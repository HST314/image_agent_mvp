"""Resolve an approved task specification into one paid-render size."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from agent_core.models import TaskSpecification
from render_clients.payload_mapper import SEEDREAM_MIN_PIXELS, validate_render_size


_SIZE_LABELS = {
    "output_spec", "output_format_details", "size",
    "输出规格", "交付规格", "交付要求", "尺寸规格",
}
_DIMENSION = re.compile(r"(?<!\d)(\d{2,5})\s*[xX×＊*]\s*(\d{2,5})(?!\d)")
_RATIO = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{1,2})(?!\d)")


@dataclass(frozen=True)
class RenderSizeDecision:
    size: str
    source: str
    requested_spec: str | None = None


def _ratio(width: int, height: int) -> tuple[int, int]:
    divisor = math.gcd(width, height)
    return width // divisor, height // divisor


def _near(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return math.isclose(left[0] / left[1], right[0] / right[1], rel_tol=0.01)


def _round_up(value: float, step: int = 64) -> int:
    return int(math.ceil(value / step) * step)


def _minimum_area(model: str) -> int:
    return SEEDREAM_MIN_PIXELS if model.startswith("doubao-seedream-") else 0


def _scale_explicit(width: int, height: int, model: str) -> tuple[int, int]:
    target_area = _minimum_area(model)
    if width == height:
        target_area = max(target_area, 2048 * 2048)
    if not target_area or width * height >= target_area:
        return width, height
    scale = math.sqrt(target_area / (width * height))
    return _round_up(width * scale), _round_up(height * scale)


def _size_for_ratio(ratio: tuple[int, int], model: str, default_size: str) -> tuple[int, int]:
    default_match = _DIMENSION.fullmatch(default_size.strip())
    default_area = (int(default_match.group(1)) * int(default_match.group(2))) if default_match else 0
    target_area = max(default_area, _minimum_area(model), 2048 * 2048 if ratio == (1, 1) else 0)
    if not target_area:
        target_area = 2048 * 2048
    unit = math.sqrt(target_area / (ratio[0] * ratio[1]))
    return _round_up(ratio[0] * unit), _round_up(ratio[1] * unit)


def _keyword_ratios(text: str) -> list[tuple[int, int]]:
    ratios: list[tuple[int, int]] = []
    if re.search(r"正方形|方形(?:图片|画布|尺寸|构图)|方图", text):
        ratios.append((1, 1))
    if "竖版手机" in text or "手机竖版" in text:
        ratios.append((9, 16))
    elif "竖版" in text:
        ratios.append((3, 4))
    if "横版" in text:
        ratios.append((16, 9))
    return ratios


def resolve_render_size(spec: TaskSpecification, model: str, default_size: str) -> RenderSizeDecision:
    """Resolve dimensions and reject contradictory approved facts before any paid call."""
    requirements = [
        fact.value.strip()
        for fact in spec.facts
        if fact.label in _SIZE_LABELS and fact.status != "blocking" and fact.value.strip()
    ]
    explicit: list[tuple[int, int]] = []
    requested_ratios: list[tuple[int, int]] = []
    for text in requirements:
        explicit.extend((int(match.group(1)), int(match.group(2))) for match in _DIMENSION.finditer(text))
        requested_ratios.extend(_ratio(int(match.group(1)), int(match.group(2))) for match in _RATIO.finditer(text))
        requested_ratios.extend(_keyword_ratios(text))

    explicit_unique = list(dict.fromkeys(explicit))
    ratio_unique = list(dict.fromkeys(requested_ratios))
    if len(explicit_unique) > 1:
        raise ValueError("输出规格包含多个不同尺寸，请在生图前确认唯一尺寸。")
    if ratio_unique and any(not _near(ratio_unique[0], item) for item in ratio_unique[1:]):
        raise ValueError("输出规格包含互相冲突的宽高比，请在生图前确认唯一规格。")
    if explicit_unique and ratio_unique and not _near(_ratio(*explicit_unique[0]), ratio_unique[0]):
        raise ValueError("输出规格中的像素尺寸与宽高比冲突，请在生图前确认唯一规格。")

    requested_spec = "；".join(requirements) or None
    if explicit_unique:
        width, height = _scale_explicit(*explicit_unique[0], model)
        resolved = f"{width}x{height}"
        validate_render_size(model, resolved)
        return RenderSizeDecision(resolved, "task_exact_size", requested_spec)
    if ratio_unique:
        width, height = _size_for_ratio(ratio_unique[0], model, default_size)
        resolved = f"{width}x{height}"
        validate_render_size(model, resolved)
        return RenderSizeDecision(resolved, "task_aspect_ratio", requested_spec)

    validate_render_size(model, default_size)
    return RenderSizeDecision(default_size, "runtime_default", requested_spec)
