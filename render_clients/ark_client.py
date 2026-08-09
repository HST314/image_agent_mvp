"""Ark text-to-image client; simulation is intentionally external to production."""

from __future__ import annotations

import os
from typing import Any

from render_clients.image_render_client import ImageRenderClient


DEFAULT_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_ARK_IMAGE_MODEL = "doubao-seedream-5-0-260128"


class ArkImageRenderClient(ImageRenderClient):
    """Render images through Ark with mandatory credentials."""

    def __init__(
        self,
        base_url: str = DEFAULT_ARK_BASE_URL,
        api_key: str | None = None,
        model: str = DEFAULT_ARK_IMAGE_MODEL,
    ) -> None:
        super().__init__(
            base_url=os.getenv("ARK_BASE_URL", base_url),
            api_key=api_key or os.getenv("ARK_API_KEY"),
            model=model,
        )

    def render(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Render one image and normalize the result to a URL payload."""

        request_payload = {**payload, "model": payload.get("model") or self.model}
        if not self.api_key:
            raise RuntimeError("未配置 Ark 凭证；只有显式离线测试客户端可以生成模拟资产。")
        return self._remote_response(request_payload)

    def _remote_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Call Ark through the OpenAI SDK-compatible images endpoint."""

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai SDK is required when ARK_API_KEY is configured.") from exc

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        response = client.images.generate(
            model=str(payload["model"]),
            prompt=str(payload["prompt"]),
            size=str(payload.get("size", "2K")),
            response_format=str(payload.get("response_format", "url")),
            extra_body=payload.get("extra_body", {"watermark": False}),
        )
        first = response.data[0]
        return {
            "url": first.url,
            "provider": "ark",
            "model": payload["model"],
            "mock": False,
            "raw": response.model_dump(mode="json"),
        }
