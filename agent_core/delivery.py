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

DELIVERY_NOTE_SYSTEM_PROMPT = """你是资深视觉设计总监。你的文字是无视觉能力的 PPT 模型理解最终图片的唯一依据。
根据提供的任务书、最终渲染提示词、风格选择及理由、质检结论，撰写准确、具体、可直接用于配图叙述的 Markdown 设计说明。

使用自然、可读的 Markdown 组织内容，可以自由决定标题和段落，不强制固定字段或固定结构。
要求：以连贯段落为主，不要把输入字段机械堆砌；尽量说清主体、空间层级、视觉动势、材质、光影、主辅色及其关系；只描述输入能够支持的事实，不臆测图片中不可确认的文字、品牌或工艺参数；不要插入图片链接、资产 ID、哈希或追溯信息；不要输出 JSON 或用代码块包裹全文，只输出 Markdown 正文。

以下五个压缩示例仅用于学习表达密度和结构，不得照抄其中的航天题材事实：

【示例一：金珐琅星空浮雕徽章】奢华航天美学与传统珐琅工艺结合。圆形金边徽章以银色火箭为垂直中轴，深蓝星空、对称云纹与环绕卫星链建立中心聚焦和向上动势；深海蓝、香槟金、亮银和暖金形成庄重而璀璨的层次。云纹、圆环和星链分别传达腾飞、圆满与规模化部署；工艺表现强调金属浮雕、珐琅点色和拉丝底衬。

【示例二：银质极简科技徽章】极简科技感与冷峻金属质感结合。磨砂银圆形底衬承托深蓝火箭，金色尾焰沿纵轴向下延伸，几何卫星与细线构成克制的网络背景；银灰、深蓝、亮金和墨蓝形成理性有序的冷暖对比。设计以精密金属、通信网络和向上推进表达工业严谨与技术创新，工艺侧重哑光电镀、珐琅填色及局部金箔效果。

【示例三：双景叙事纪念徽章】历史叙事与双重视觉并置。左侧银币式浮雕徽章和右侧竖版发射实景构成从设计理想到工程现实的对应关系，统一光源把两部分连接成完整故事；银白、炭黑、炽金与暗紫蓝营造庄重档案感。浮雕的铭刻感强化纪念属性，齿纹边缘、渐变火焰和哑光印刷共同建立收藏品质。

【示例四：香槟金轨道阵列徽章】殿堂级秩序美学与轨道几何结合。中央金色徽章作为视觉锚点，等距卫星阵列和闭合连线向外扩展，背景网格进一步强化精确坐标感；香槟金、深蓝、银白与灰蓝呈现高贵、理性且高度标准化的气质。环形轨道象征规模化组网，拉丝金属、低温珐琅和哑光镀层支撑精密工艺表达。

【示例五：虚实融合文化徽章】工业质感、文化符号与科幻信息层叠合。前景拉丝银徽章、中景半透明信息面板和远景真实发射场景形成清晰纵深，虚拟符号与工程实体相互呼应；宝石蓝、金色、墨蓝和亮白制造丰富的冷暖及明暗对比。文字刻印与金属拉丝传达文化和工业底蕴，珐琅渐变与发光描边连接传统工艺和数字界面。
"""

def delivery_note_prompt(snapshot: dict[str, Any], asset: dict[str, Any]) -> str:
    """Build the text-only delivery-note request from frozen workflow evidence."""

    selections = snapshot.get("style_selections") or []
    chosen = next(
        (item for item in selections if item.get("style_id") == asset.get("style_id")),
        selections[0] if selections else {},
    )
    render_plans = snapshot.get("render_plans") or []
    plan = next(
        (item for item in render_plans if item.get("style_id") == asset.get("style_id")),
        render_plans[0] if render_plans else {},
    )
    evidence = {
        "task_card": snapshot.get("task_card") or {},
        "final_render_prompt": plan.get("prompt_text") or snapshot.get("human_prompt") or "",
        "style_selection": chosen,
        "quality_result": snapshot.get("inspection") or {},
    }
    return (
        f"{DELIVERY_NOTE_SYSTEM_PROMPT}\n\n"
        "请根据以下最终冻结证据撰写设计说明：\n"
        f"{json.dumps(evidence, ensure_ascii=False, sort_keys=True)}"
    )


def build_delivery(
    snapshot: dict[str, Any],
    project_id: str,
    asset: dict[str, Any],
    trace_ref: str,
    *,
    generated_markdown: str | None = None,
) -> DesignDeliveryEnvelopeV1:
    task=(snapshot.get("task_card") or {})
    selections=snapshot.get("style_selections") or []
    chosen=next((x for x in selections if x.get("style_id")==asset.get("style_id")), selections[0] if selections else {})
    note={"concept":chosen.get("mechanism","以已确认任务书和最终质检结果为准。"),
          "selection_reason":chosen.get("reason","经候选筛选、质检与人工确认。"),
          "task_fit":chosen.get("task_fit",task.get("deliverable_goal","")),
          "final_asset":{"artifact_id":asset["artifact_id"],"sha256":asset["sha256"]},"trace_ref":trace_ref}
    fallback=(f"# 最终设计说明\n\n## 设计理念\n{note['concept']}\n\n## 选择理由\n{note['selection_reason']}\n\n"
              f"## 任务适配\n{note['task_fit']}\n\n最终资产：`{asset['artifact_id']}`\n\n追溯引用：`{trace_ref}`\n")
    candidate = (generated_markdown or "").strip()
    markdown = candidate or fallback
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
