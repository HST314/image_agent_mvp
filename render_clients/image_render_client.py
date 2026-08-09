"""Text-to-image client boundary."""

from __future__ import annotations

from typing import Any


class ImageRenderClient:
    """Base render client that accepts already-composed payloads."""

    def __init__(self, base_url: str, api_key: str | None, model: str, *, timeout: float = 180,
                 max_retries: int = 0, idempotency_key: str | None = None) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.idempotency_key = idempotency_key

    def render(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Render one image payload and return provider-normalized data."""

        raise NotImplementedError
