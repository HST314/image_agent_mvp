"""Generate and persist the minimal, machine-readable delivery bundle."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image

from agent_core.contracts import DesignDeliveryEnvelopeV1
from storage.project_store import long_path

def build_delivery(snapshot: dict[str, Any], project_id: str, asset: dict[str, Any], trace_ref: str) -> DesignDeliveryEnvelopeV1:
    task=(snapshot.get("task_card") or {})
    selections=snapshot.get("style_selections") or []
    chosen=next((x for x in selections if x.get("style_id")==asset.get("style_id")), selections[0] if selections else {})
    note={"concept":chosen.get("mechanism","以已确认任务书和最终质检结果为准。"),
          "selection_reason":chosen.get("reason","经候选筛选、质检与人工确认。"),
          "task_fit":chosen.get("task_fit",task.get("deliverable_goal","")),
          "final_asset":{"artifact_id":asset["artifact_id"],"sha256":asset["sha256"]},"trace_ref":trace_ref}
    markdown=(f"# 最终设计说明\n\n## 设计理念\n{note['concept']}\n\n## 选择理由\n{note['selection_reason']}\n\n"
              f"## 任务适配\n{note['task_fit']}\n\n最终资产：`{asset['artifact_id']}`\n\n追溯引用：`{trace_ref}`\n")
    return DesignDeliveryEnvelopeV1(task_id=str(task.get("task_id","unknown")), project_id=project_id,
        final_image={k:asset[k] for k in ("artifact_id","uri","sha256")}, design_note_markdown=markdown,
        design_note=note, trace_ref=trace_ref)

def persist_delivery(root: Path, envelope: DesignDeliveryEnvelopeV1) -> dict[str,str]:
    directory=root/"delivery"; directory.mkdir(parents=True,exist_ok=True)
    md=directory/"design-note.md"; js=directory/"delivery.json"
    md.write_text(envelope.design_note_markdown,encoding="utf-8")
    js.write_text(envelope.model_dump_json(indent=2),encoding="utf-8")
    return {"markdown":str(md.relative_to(root)),"json":str(js.relative_to(root))}


def finalize_delivery(root: Path, envelope: DesignDeliveryEnvelopeV1, source_image: Path) -> dict[str, str]:
    """Persist the user-facing delivery bundle, including the immutable image.

    The image filename contains its content hash, so retrying finalize is idempotent and
    a later branch can never silently overwrite an earlier branch's delivered image.
    """
    files = persist_delivery(root, envelope)
    suffix = source_image.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        raise ValueError("最终图片格式不受支持。")
    directory = root / "delivery"
    destination = directory / f"final-image-{envelope.final_image.sha256[:12]}{suffix}"
    if not destination.exists():
        temporary = directory / f".{destination.name}.{uuid4().hex}.tmp"
        try:
            shutil.copyfile(source_image, long_path(temporary))
            os.replace(long_path(temporary), long_path(destination))
        finally:
            long_path(temporary).unlink(missing_ok=True)
    files["image"] = str(destination.relative_to(root))
    return files


def bundle_id_for(
    project_id: str,
    branch_id: str,
    checkpoint_id: str,
    image_sha256: str,
) -> str:
    """Derive the stable identity of one immutable branch delivery candidate."""

    identity = json.dumps(
        [project_id, branch_id, checkpoint_id, image_sha256],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"bundle_{hashlib.sha256(identity).hexdigest()[:32]}"


def _write_immutable(path: Path, content: bytes) -> None:
    """Create one candidate file once, accepting only byte-identical retries."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise ValueError("DELIVERY_CANDIDATE_CONFLICT")
        return
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with long_path(temporary).open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(long_path(temporary), long_path(path))
    finally:
        long_path(temporary).unlink(missing_ok=True)


def finalize_delivery_candidate(
    root: Path,
    envelope: DesignDeliveryEnvelopeV1,
    source_image: Path,
    *,
    branch_id: str,
    checkpoint_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Materialize an immutable image + Markdown candidate for one frozen branch.

    The marker is written last. A crash before that point leaves no discoverable
    candidate, and the next call deterministically completes the same bundle.
    """

    suffix = source_image.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("最终图片格式不受支持。")
    bundle_id = bundle_id_for(
        envelope.project_id,
        branch_id,
        checkpoint_id,
        envelope.final_image.sha256,
    )
    directory = root / "delivery"
    image_name = f"{bundle_id}-image{suffix}"
    note_name = f"{bundle_id}-design-note.md"
    envelope_name = f"{bundle_id}-delivery.json"
    marker_name = f"{bundle_id}-candidate.json"
    image_bytes = source_image.read_bytes()
    note_bytes = envelope.design_note_markdown.encode("utf-8")
    envelope_bytes = envelope.model_dump_json(indent=2).encode("utf-8")
    _write_immutable(directory / image_name, image_bytes)
    _write_immutable(directory / note_name, note_bytes)
    _write_immutable(directory / envelope_name, envelope_bytes)
    with Image.open(source_image) as opened:
        width, height = opened.size
        image_mime = Image.MIME.get(opened.format or "", "application/octet-stream")
    marker_path = directory / marker_name
    previous = None
    if marker_path.exists():
        previous = json.loads(marker_path.read_text(encoding="utf-8"))
        if isinstance(previous.get("created_at"), str):
            created_at = previous["created_at"]
    marker = {
        "schema_version": "1.0",
        "finalized": True,
        "bundle_id": bundle_id,
        "branch_id": branch_id,
        "checkpoint_id": checkpoint_id,
        "asset_sha256": envelope.final_image.sha256,
        "files": {
            "image": f"delivery/{image_name}",
            "markdown": f"delivery/{note_name}",
            "json": f"delivery/{envelope_name}",
        },
        "image": {
            "mime_type": image_mime,
            "size_bytes": len(image_bytes),
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
            "width": width,
            "height": height,
        },
        "design_note": {
            "mime_type": "text/markdown",
            "size_bytes": len(note_bytes),
            "sha256": hashlib.sha256(note_bytes).hexdigest(),
        },
        "created_at": created_at,
        "finalized_at": created_at,
    }
    if previous is not None:
        if previous != marker:
            raise ValueError("DELIVERY_CANDIDATE_CONFLICT")
        return previous
    _write_immutable(
        marker_path,
        json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        ),
    )
    return marker
