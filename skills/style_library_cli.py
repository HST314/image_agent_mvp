"""Build and validate the lightweight style-library index."""
from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path

from PIL import Image

from skills.style_library import LibraryManifest, StyleLibrary, StyleRecord, _sha


def build_index(root: str | Path) -> list[StyleRecord]:
    root = Path(root).resolve()
    records: list[StyleRecord] = []
    seen: dict[str, str] = {}
    for metadata in sorted((root / "styles").glob("*/style.json")):
        source = json.loads(metadata.read_text("utf-8"))
        image_path = (metadata.parent / source.pop("image")).resolve()
        if metadata.parent.resolve() not in image_path.parents:
            raise ValueError("STYLE_PATH_TRAVERSAL")
        digest = _sha(image_path)
        if digest in seen:
            raise ValueError(f"duplicate image: {seen[digest]} and {source.get('style_id')}")
        seen[digest] = str(source.get("style_id"))
        with Image.open(image_path) as image:
            image.verify()
            width, height = image.size
        relative = image_path.relative_to(root).as_posix()
        media = mimetypes.guess_type(image_path.name)[0] or ""
        records.append(StyleRecord(image=relative, sha256=digest, media_type=media, width=width, height=height, **source))
    (root / "index.jsonl").write_text("".join(r.model_dump_json() + "\n" for r in records), "utf-8")
    manifest_path = root / "library.json"
    current = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else {"library_id": "style-library", "version": "1"}
    manifest = LibraryManifest(**{**current, "style_count": len(records)})
    manifest_path.write_text(manifest.model_dump_json(indent=2), "utf-8")
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args(argv)
    if args.build:
        build_index(args.root)
    StyleLibrary(args.root).records()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
