"""Materialize the packaged, deterministic first-edition style library."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image


STYLES = (
    ("OPC-GRID", "秩序网格", "严谨的信息网格", (38, 70, 120), "modular grid", "matte paper", "even", "systematic", "geometric", "navy"),
    ("OPC-FOCAL", "聚焦层次", "强中心与景深", (180, 58, 48), "central focus", "gloss", "spotlight", "heroic", "bold shapes", "warm red"),
    ("OPC-LAYERS", "空间叠层", "前中后景叠层", (42, 130, 104), "layered depth", "translucent", "rim light", "spatial", "overlap", "emerald"),
    ("OPC-EDITORIAL", "编辑流线", "非对称编辑节奏", (120, 62, 148), "asymmetric flow", "ink", "soft", "editorial", "typographic rhythm", "violet"),
    ("OPC-MINIMAL", "极简信号", "高留白与单一信号", (220, 150, 38), "negative space", "smooth", "ambient", "minimal", "single signal", "amber"),
)


def ensure_builtin_style_library(root: Path) -> Path:
    """Create only package-owned fixtures; never scan or import legacy assets."""
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, (style_id, title, describe, color, *features) in enumerate(STYLES):
        image_path = root / "images" / f"{style_id}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        if not image_path.exists():
            image = Image.new("RGB", (16, 16), color)
            for point in range(index + 1):
                image.putpixel((point, point), (255, 255, 255))
            image.save(image_path, format="PNG")
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        extraction_key = "builtin-v1"
        extraction = {"schema_version":"1.0", "extraction_key":extraction_key, "style_id":style_id,
                      "image_sha256":digest, "model_id":"builtin-reviewed-v1", "prompt_version":"style-v1", "status":"success",
                      "composition":features[0], "material":features[1], "lighting":features[2], "narrative":features[3],
                      "graphic_language":features[4], "color":features[5], "prompt_supplement":describe}
        target = root / "extractions" / style_id / f"{extraction_key}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(extraction, ensure_ascii=False), encoding="utf-8")
        rows.append({"style_id":style_id, "image":f"images/{style_id}.png", "title":title, "describe":describe,
                     "sha256":digest, "media_type":"image/png", "width":16, "height":16,
                     "tags":["通用", title], "task_fit":["海报", "移动端"], "active_extraction":extraction_key})
    (root / "library.json").write_text(json.dumps({"schema_version":"1.0", "library_id":"opc-first-edition", "version":"1.0.0", "style_count":5}, ensure_ascii=False), encoding="utf-8")
    (root / "index.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return root
