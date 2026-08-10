"""Map versioned prompts into render API payloads."""

from __future__ import annotations

from typing import Any

SEEDREAM_MIN_PIXELS = 3_686_400


def validate_render_size(model: str, size: str) -> None:
    """Reject known-invalid provider dimensions before a paid render request."""
    if not model.startswith("doubao-seedream-") or size in {"1K", "2K", "4K"}:
        return
    try:
        width_text, height_text = size.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"图片尺寸不合法：{size}，应使用 宽x高 或 1K/2K/4K。") from exc
    if width * height < SEEDREAM_MIN_PIXELS:
        raise ValueError(
            f"图片尺寸不合法：{size} 仅 {width * height} 像素；模型 {model} "
            f"要求至少 {SEEDREAM_MIN_PIXELS} 像素（例如 2560x1440）。"
        )


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
    validate_render_size(model, size)

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
