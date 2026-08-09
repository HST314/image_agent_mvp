"""Map versioned prompts into render API payloads."""

from __future__ import annotations

from typing import Any


def build_render_payload(
    model: str,
    prompt: str,
    size: str,
    metadata: dict[str, str],
    response_format: str = "url",
    watermark: bool = False,
    reference_images: list[str] | None = None,
) -> dict[str, Any]:
    """Build a generic text-to-image payload."""

    return {
        "model": model,
        "prompt": prompt,
        "size": size,
        "response_format": response_format,
        "extra_body": {
            **({"image": reference_images[0] if len(reference_images) == 1 else reference_images} if reference_images else {}),
            "watermark": watermark,
        },
        "metadata": metadata,
    }
