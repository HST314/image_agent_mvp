"""v1.7.8 regression gate for generic offline task submission."""
from pathlib import Path

from agent_core.models import ImageTaskCard
from agent_core.workflow_runner import WorkflowRunner
from configs.runtime_policy import RuntimePolicy
from interaction.confirmation_builder import specification_from_task
from skills.errors import ResourceError
from storage.project_store import ProjectStore


def _generic_task() -> ImageTaskCard:
    return ImageTaskCard.model_validate({
        "task_id": "task_new",
        "project_id": "test2",
        "source_refs": [{"ref_id": "brief-001", "ref_type": "brief",
                         "excerpt": "请描述已确认的创作输入。", "source_hash": None}],
        "deliverable_goal": "描述需要生成的视觉内容、主体、风格和画面重点。",
        "usage_context": "内部审核与决策",
        "category_ref": {"category_id": "generic_visual_delivery", "version": "1.0"},
        "known_facts": {"audience": "内部审核人员", "tone": "清晰、精致",
                        "output_spec": "竖版手机"},
        "unknowns": {}, "asset_inputs": [], "status": "draft",
    })


def test_generic_offline_task_uses_approved_fallback_skill(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "test2")
    store.create(RuntimePolicy(offline_mode=True).snapshot())
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    task = _generic_task()
    spec = specification_from_task(task)
    revision = {"revision_hash": "approved-revision"}

    result = runner._candidates({
        "task_card": task.model_dump(mode="json"),
        "task_specification": spec.model_dump(mode="json"),
        "task_revision": revision,
        "task_approval": {"revision_hash": "approved-revision", "actor": "reviewer"},
    }, {})

    assert len(result["candidates"]) == 5
    assert {item["provenance"]["category_id"] for item in result["candidates"]} == {
        "generic_visual_delivery"
    }


def test_resource_error_can_receive_python_traceback() -> None:
    error = ResourceError("RESOURCE_NO_MATCH", "library.json", "trace_test")
    try:
        raise error
    except ResourceError as caught:
        assert caught.__traceback__ is not None
