"""Persist provider image responses before they cross a workflow boundary."""
from __future__ import annotations

import base64
import urllib.request
from typing import Any

from storage.project_store import ArtifactStore


def persist_image_response(store: ArtifactStore, response: dict[str, Any], *, timeout: float = 30,
                           metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    content = response.get("bytes") or response.get("content")
    if isinstance(content, str):
        content = base64.b64decode(content, validate=True)
    if content is None:
        url = response.get("url") or response.get("uri")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("供应商响应没有可持久化的图片内容。")
        request = urllib.request.Request(url, headers={"User-Agent": "image-agent/1"})
        with urllib.request.urlopen(request, timeout=timeout) as remote:
            status = getattr(remote, "status", 200)
            mime = remote.headers.get_content_type()
            if status != 200 or mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise ValueError(f"图片下载响应无效（status={status}, mime={mime}）。")
            content = remote.read(25 * 1024 * 1024 + 1)
    return store.save_bytes(content, metadata={**(metadata or {}), "provider": response.get("provider"), "model": response.get("model")})
