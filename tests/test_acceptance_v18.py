"""v1.7.8 regression gate for generic offline task submission."""
import json
from pathlib import Path

import pytest

from agent_core.models import ImageTaskCard
from agent_core.workflow_runner import WorkflowRunner
from configs.runtime_policy import RuntimePolicy
from interaction.confirmation_builder import specification_from_task
from skills.errors import ResourceError
from storage.project_store import ProjectStore
from storage.prompt_store import CorruptPromptLogError, PromptStore


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
    invocation = result["skill_invocations"]
    assert invocation["category_library"]["source"] == "广告品类库"
    assert invocation["category_library"]["category_id"] == "generic_visual_delivery"
    assert len(invocation["style_library"]["selections"]) == 5
    for selection in invocation["style_library"]["selections"]:
        assert selection["reference_asset"]["uri"].startswith("artifact://artifact_")
        assert selection["artistic_interpretation"]
        assert set(selection["analysis"]) == {
            "composition", "material", "lighting", "narrative", "graphic_language", "color",
        }


def test_five_way_offline_generation_keeps_prompt_log_valid(tmp_path: Path) -> None:
    """The five candidate workers share one atomic prompt audit log."""
    store = ProjectStore(tmp_path, "five-way")
    store.create(RuntimePolicy(offline_mode=True).snapshot())
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    task = _generic_task()
    specification = specification_from_task(task)

    result = runner._candidates({
        "task_card": task.model_dump(mode="json"),
        "task_specification": specification.model_dump(mode="json"),
        "task_revision": {"revision_hash": "approved-revision"},
        "task_approval": {"revision_hash": "approved-revision", "actor": "reviewer"},
    }, {})

    lines = (store.root / "runtime/prompts.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert len(result["candidates"]) == 5
    assert len(records) == 10
    assert sum(record["status"] == "started" for record in records) == 5
    assert sum(record["status"] == "completed" for record in records) == 5
    assert len({record["prompt_id"] for record in records}) == 10


def test_corrupt_prompt_log_is_reported_and_never_extended(tmp_path: Path) -> None:
    path = tmp_path / "prompts.jsonl"
    damaged = '{"prompt_id":"truncated"'
    path.write_text(damaged, encoding="utf-8")
    store = PromptStore(path)
    record = {
        "messages": [], "template_id": "test", "template_version": "1",
        "template_hash": "hash", "variables": {}, "input_refs": [], "model": "offline",
        "parameters": {}, "config_hash": "config", "state": "test", "trace_id": "trace",
    }

    with pytest.raises(CorruptPromptLogError, match="第 1 行"):
        store.begin(record)
    assert path.read_text(encoding="utf-8") == damaged


def _prompt_record(trace_id: str) -> dict:
    return {
        "messages": [], "template_id": "test", "template_version": "1",
        "template_hash": "hash", "variables": {}, "input_refs": [], "model": "offline",
        "parameters": {}, "config_hash": "config", "state": "test", "trace_id": trace_id,
    }


@pytest.mark.parametrize("corruption", ["record_hash", "format_version"])
@pytest.mark.parametrize("operation", ["begin", "complete", "fail"])
def test_integrity_corruption_rejects_every_prompt_write_without_changing_bytes(
    tmp_path: Path, corruption: str, operation: str,
) -> None:
    path = tmp_path / "prompts.jsonl"
    store = PromptStore(path)
    damaged_id = store.begin(_prompt_record("damaged"))
    target_id = store.begin(_prompt_record("target"))
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    damaged = next(record for record in records if record["prompt_id"] == damaged_id)
    if corruption == "record_hash":
        damaged["record_hash"] = "0" * 64
    else:
        damaged["format_version"] = 999
    original = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records) + "\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(CorruptPromptLogError, match="第 1 行"):
        if operation == "begin":
            store.begin(_prompt_record("new"))
        elif operation == "complete":
            store.complete(target_id, output_raw={"ok": True})
        else:
            store.fail(target_id, {"message": "failed"})

    assert path.read_bytes() == original.encode("utf-8")


def test_resource_error_can_receive_python_traceback() -> None:
    error = ResourceError("RESOURCE_NO_MATCH", "library.json", "trace_test")
    try:
        raise error
    except ResourceError as caught:
        assert caught.__traceback__ is not None
