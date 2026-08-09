"""Canonical image asset contract shared by every generation path."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def normalize_image_asset(response: dict[str, Any], *, provider: str | None = None,
                          model: str | None = None) -> dict[str, Any]:
    """Return a stable, complete asset; reject unusable provider responses."""
    raw = dict(response)
    uri = raw.get("uri") or raw.get("url")
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("生图服务未返回可保存的图片地址。")
    content = raw.get("content") or raw.get("bytes")
    reference_hash = raw.get("reference_hash")
    if content is not None:
        material = content if isinstance(content, bytes) else str(content).encode("utf-8")
        reference_hash = hashlib.sha256(material).hexdigest()
    elif not reference_hash:
        # A persistent URI is the provider's content reference. Hash the exact
        # reference rather than the mutable response envelope.
        reference_hash = hashlib.sha256(uri.strip().encode("utf-8")).hexdigest()
    sha256 = str(raw.get("sha256") or reference_hash)
    if len(sha256) != 64:
        sha256 = hashlib.sha256(sha256.encode("utf-8")).hexdigest()
    normalized = {
        **raw,
        "uri": uri.strip(),
        "reference_hash": str(reference_hash),
        "sha256": sha256,
        "provider": str(raw.get("provider") or provider or "unknown"),
        "model": str(raw.get("model") or model or "unknown"),
        "mock": bool(raw.get("mock", False)),
    }
    normalized.pop("url", None)
    normalized.pop("bytes", None)
    if content is not None and isinstance(content, bytes):
        normalized["content"] = content.hex()
    # Ensure the asset can always cross a checkpoint boundary.
    json.dumps(normalized, ensure_ascii=False)
    return normalized
