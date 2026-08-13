"""Regression gates for the complete, editable taskbook experience."""

from pathlib import Path

from agent_core.models import ImageTaskCard
from agent_core.workflow_runner import WorkflowRunner
from configs.runtime_policy import RuntimePolicy
from interaction.confirmation_builder import (
    specification_from_task,
    specification_to_markdown,
    specification_value,
    update_specification_from_markdown,
)
from storage.project_store import ProjectStore


ROOT = Path(__file__).resolve().parents[1]


def _task() -> ImageTaskCard:
    return ImageTaskCard.model_validate({
        "task_id": "task-taskbook",
        "project_id": "taskbook-regression",
        "source_refs": [{
            "ref_id": "brief-taskbook",
            "ref_type": "brief",
            "excerpt": "为新品发布制作一张完整视觉海报",
            "source_hash": None,
        }],
        "deliverable_goal": "交付一张可用于新品发布的竖版海报",
        "usage_context": "社交媒体发布与线下评审",
        "category_ref": {"category_id": "generic_visual_delivery", "version": "1.0"},
        "known_facts": {
            "audience": "年轻消费人群",
            "tone": "清晰、精致、有庆典感",
            "output_spec": "1080 × 1440 PNG",
            "content_boundaries": "不得出现未经确认的价格",
        },
        "unknowns": {"brand": "品牌标识位置待确认"},
        "asset_inputs": [],
        "status": "draft",
    })


def test_generated_taskbook_contains_original_goal_and_full_human_context() -> None:
    markdown = specification_to_markdown(specification_from_task(_task()))

    assert "## 任务目标与使用场景" in markdown
    assert "交付目标：交付一张可用于新品发布的竖版海报" in markdown
    assert "使用场景：社交媒体发布与线下评审" in markdown
    assert "目标受众：年轻消费人群" in markdown
    assert "输出规格：1080 × 1440 PNG" in markdown
    assert "内容边界：不得出现未经确认的价格" in markdown
    assert "需求来源：为新品发布制作一张完整视觉海报" in markdown
    assert "暂定处理（请核对）" in markdown


def test_saved_markdown_is_exact_source_of_truth_and_updates_structured_goal() -> None:
    original = specification_from_task(_task())
    edited = (
        "# 我的创作任务书\n\n"
        "这段人工说明必须原样保留。\n\n"
        "## 任务目标与使用场景\n\n"
        "- 交付目标：改为横版新品主视觉\n"
        "- 使用场景：官网首屏\n\n"
        "## 人工补充\n\n"
        "不要重新套用系统模板。  "
    )

    updated = update_specification_from_markdown(original, edited)

    assert specification_to_markdown(updated) == edited
    assert updated.source_markdown == edited
    assert updated.content_hash != original.content_hash
    assert specification_value(updated, "deliverable_goal") == "改为横版新品主视觉"
    assert specification_value(updated, "usage_context") == "官网首屏"


def test_save_then_approve_never_regenerates_over_user_markdown(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "taskbook-regression")
    store.create(RuntimePolicy(offline_mode=True).snapshot())
    runner = WorkflowRunner(store, ROOT / "configs/model_config.yaml", offline_mode=True)
    task_card = _task().model_dump(mode="json")

    initial = runner._confirmation({"task_card": task_card}, {})
    edited = "# 用户版本\n\n自由段落也必须保留。\n\n- 交付目标：用户最后保存的版本"
    saved = runner._confirmation({"task_card": task_card, **initial}, {"edited_markdown": edited})
    approved = runner._confirmation(
        {"task_card": task_card, **saved},
        {"task_approved": True, "actor": "reviewer"},
    )

    assert saved["task_markdown"] == edited
    assert approved["task_markdown"] == edited
    assert approved["task_revision"]["revision_hash"] == saved["task_revision"]["revision_hash"]
    assert approved["task_approval"]["revision_hash"] == saved["task_revision"]["revision_hash"]


def test_taskbook_frontend_uses_full_document_canvas_and_save_feedback() -> None:
    css = (ROOT / "frontend/static/css/main.css").read_text(encoding="utf-8")
    js = (ROOT / "frontend/static/js/taskbook.js").read_text(encoding="utf-8")

    taskbook_rule = css.split(".taskbook__document{", 1)[1].split("}", 1)[0]
    assert "max-width:none" in taskbook_rule
    assert "min-height:clamp(440px,58vh,760px)" in taskbook_rule
    assert "taskbook__editor" in css
    assert "正在保存…" in js
