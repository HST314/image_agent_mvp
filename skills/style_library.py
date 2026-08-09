"""Validated, image-isolated style library used by the production workflow."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from PIL import Image
import portalocker
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class StyleLibraryError(ValueError):
    """Stable failure raised before any paid render call."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StyleRecord(StrictModel):
    style_id: str = Field(pattern=r"^[A-Z][A-Z0-9]*-[A-Z0-9]+(?:-[A-Z0-9]+)*$")
    image: str
    title: str = Field(min_length=1)
    describe: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(pattern=r"^image/(png|jpeg|gif|webp)$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    source: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)
    task_fit: list[str] = Field(default_factory=list)
    active_extraction: str | None = None


class LibraryManifest(StrictModel):
    schema_version: str = "1.0"
    library_id: str
    version: str
    style_count: int = Field(ge=0)


class StyleExtraction(StrictModel):
    schema_version: str = "1.0"
    extraction_key: str
    style_id: str
    image_sha256: str
    model_id: str
    prompt_version: str
    status: str = Field(pattern=r"^(success|error)$")
    composition: str
    material: str
    lighting: str
    narrative: str
    graphic_language: str
    color: str
    prompt_supplement: str
    raw_response_redacted: Any | None = None


@dataclass(frozen=True)
class SelectedStyle:
    style: StyleRecord
    extraction: StyleExtraction
    reason: str
    task_fit: str
    mechanism: str
    risk: str


def _inside(root: Path, candidate: Path) -> Path:
    root, candidate = root.resolve(), candidate.resolve()
    if root != candidate and root not in candidate.parents:
        raise StyleLibraryError("STYLE_PATH_TRAVERSAL")
    return candidate


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StyleLibrary:
    """The sole runtime entrypoint; legacy documents are never scanned."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        try:
            self.manifest = LibraryManifest.model_validate_json((self.root / "library.json").read_text("utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            raise StyleLibraryError("STYLE_LIBRARY_INVALID") from exc

    def records(self) -> list[StyleRecord]:
        index = self.root / "index.jsonl"
        try:
            rows = [StyleRecord.model_validate_json(line) for line in index.read_text("utf-8").splitlines() if line.strip()]
        except (OSError, ValidationError, ValueError) as exc:
            raise StyleLibraryError("STYLE_INDEX_INVALID") from exc
        if len(rows) != self.manifest.style_count or len({r.style_id for r in rows}) != len(rows):
            raise StyleLibraryError("STYLE_INDEX_COUNT_OR_ID_INVALID")
        seen_hashes: set[str] = set()
        for row in rows:
            path = _inside(self.root, self.root / row.image)
            if not path.is_file() or _sha(path) != row.sha256:
                raise StyleLibraryError("STYLE_IMAGE_MISSING_OR_HASH_MISMATCH")
            try:
                with Image.open(path) as image:
                    image.verify()
            except Exception as exc:
                raise StyleLibraryError("STYLE_IMAGE_DECODE_FAILED") from exc
            if row.sha256 in seen_hashes:
                raise StyleLibraryError("STYLE_IMAGE_DUPLICATE")
            seen_hashes.add(row.sha256)
        return rows

    def extraction(self, style: StyleRecord) -> StyleExtraction:
        if not style.active_extraction:
            raise StyleLibraryError("STYLE_EXTRACTION_MISSING")
        path = _inside(self.root, self.root / "extractions" / style.style_id / f"{style.active_extraction}.json")
        try:
            result = StyleExtraction.model_validate_json(path.read_text("utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            raise StyleLibraryError("STYLE_EXTRACTION_INVALID") from exc
        if result.style_id != style.style_id or result.image_sha256 != style.sha256 or result.status != "success":
            raise StyleLibraryError("STYLE_EXTRACTION_STALE")
        return result


class StyleExtractor:
    """Versioned VLM extraction cache with exactly one schema-repair attempt."""

    FIELDS = ("composition", "material", "lighting", "narrative", "graphic_language", "color", "prompt_supplement")

    def __init__(self, root: str | Path, inspect: Callable[[str, str], Any], *, model_id: str, prompt_version: str = "style-v1") -> None:
        self.root, self.inspect, self.model_id, self.prompt_version = Path(root).resolve(), inspect, model_id, prompt_version

    def extract(self, style: StyleRecord) -> StyleExtraction:
        key = hashlib.sha256(f"{style.sha256}|1.0|{self.model_id}|{self.prompt_version}".encode()).hexdigest()
        target = _inside(self.root, self.root / "extractions" / style.style_id / f"{key}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(target) + ".lock", timeout=60):
            if target.exists():
                cached = StyleExtraction.model_validate_json(target.read_text("utf-8"))
                if cached.status == "success":
                    return cached
            image = str(_inside(self.root, self.root / style.image))
            prompt = "只观察视觉机制并返回 JSON；不得识别或复述具体主体、文字、标识或独特表达。字段：" + ",".join(self.FIELDS)
            raw = self.inspect(image, prompt)
            for attempt in range(2):
                try:
                    payload = json.loads(raw) if isinstance(raw, str) else raw
                    result = StyleExtraction(extraction_key=key, style_id=style.style_id, image_sha256=style.sha256,
                        model_id=self.model_id, prompt_version=self.prompt_version, status="success", **payload)
                    temporary = target.with_suffix(".tmp")
                    temporary.write_text(result.model_dump_json(indent=2), "utf-8")
                    os.replace(temporary, target)
                    return result
                except (ValidationError, TypeError, ValueError) as exc:
                    if attempt == 0:
                        raw = self.inspect(image, f"上次输出未通过 Schema：{type(exc).__name__}。仅返回合法 JSON。字段：{','.join(self.FIELDS)}")
                        continue
                    redacted = re.sub(r"(?i)(api[_-]?key|token|authorization)[^,}\n]*", r"\1:[REDACTED]", str(raw))
                    raise StyleLibraryError(f"STYLE_EXTRACTION_RECOVERABLE:{redacted[:500]}") from exc
        raise AssertionError("unreachable")


def select_five(records: Iterable[StyleRecord], extraction_for: Callable[[StyleRecord], StyleExtraction], task_text: str) -> list[SelectedStyle]:
    """Deterministically retrieve five unique styles with distinct lead mechanisms."""
    words = {w.lower() for w in re.findall(r"[\w\u4e00-\u9fff]+", task_text)}
    dimensions = ("composition", "material", "lighting", "narrative", "graphic_language")
    scored = sorted(records, key=lambda r: (-len(words & {x.lower() for x in [*r.tags, *r.task_fit]}), r.style_id))
    selected: list[SelectedStyle] = []
    used_values: set[str] = set()
    for record in scored:
        extraction = extraction_for(record)
        dimension = dimensions[len(selected)] if len(selected) < 5 else dimensions[-1]
        value = str(getattr(extraction, dimension)).strip().lower()
        if not value or value in used_values:
            continue
        used_values.add(value)
        selected.append(SelectedStyle(record, extraction, f"与任务词和适用标签匹配：{record.describe}",
                                      "、".join(record.task_fit) or "通用视觉任务", f"{dimension}: {value}", "需避免复制参考图独特表达"))
        if len(selected) == 5:
            return selected
    raise StyleLibraryError("STYLE_LIBRARY_INSUFFICIENT_DISTINCT_STYLES")


def safe_render_supplement(selected: SelectedStyle) -> str:
    """Text-only boundary. No source image field is accepted or emitted."""
    e = selected.extraction
    return (f"style_id={selected.style.style_id}; extraction_key={e.extraction_key}\n"
            f"构图机制：{e.composition}\n材质：{e.material}\n光影：{e.lighting}\n叙事：{e.narrative}\n"
            f"图形语言：{e.graphic_language}\n色彩：{e.color}\n艺术补充：{e.prompt_supplement}\n"
            "安全约束：仅借鉴抽象机制；禁止复制参考图主体、具体构图、文字、标识或独特表达。")
