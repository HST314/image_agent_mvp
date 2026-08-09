"""Generate and persist the minimal, machine-readable delivery bundle."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from agent_core.contracts import DesignDeliveryEnvelopeV1

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
