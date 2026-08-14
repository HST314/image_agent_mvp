"""Regression coverage for the test14/test15 workflow audit."""
from pathlib import Path

import pytest

from agent_core.models import ImageTaskCard, QuestionCard
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from calibrator.calibration_loop import build_rework_context
from configs.runtime_policy import RuntimePolicy
from model_router.executor import ModelCallError, ModelExecutor
from storage.project_store import ProjectStore, atomic_json
from agent_core.models import ReferenceImage


CONFIG = Path(__file__).parents[1] / "configs/model_config.yaml"


def cultural_wall_task() -> dict:
    return {
        "task_id": "wall-task", "project_id": "wall-project",
        "source_refs": [{"ref_id": "brief", "ref_type": "text", "excerpt": "党建文化墙设计"}],
        "deliverable_goal": "设计一组党建文化墙", "usage_context": "室内展陈",
        "known_facts": {}, "unknowns": {}, "asset_inputs": [], "status": "draft",
    }


def runner(tmp_path: Path, *, offline: bool = True) -> tuple[WorkflowRunner, ProjectStore]:
    store = ProjectStore(tmp_path, "wall-project")
    store.create(RuntimePolicy(offline_mode=offline).snapshot())
    return WorkflowRunner(store, CONFIG, offline_mode=offline), store


def test_category_constraints_are_frozen_before_clarification(tmp_path: Path) -> None:
    workflow, _ = runner(tmp_path)
    constrained = workflow.run({"task_card": cultural_wall_task()}, RunnerOptions(),
                               only_state="category_constraint")
    assert constrained["category_constraint_current"]["category_name"] == "文化墙"
    blockers = workflow._blocking_unknowns(ImageTaskCard.model_validate(constrained["task_card"]))
    assert len(blockers) >= 5
    assert constrained["category_constraint_approval"]["actor"] == "system:auto"


def test_zero_model_questions_cannot_waive_category_blockers(tmp_path: Path) -> None:
    workflow, _ = runner(tmp_path, offline=False)
    constrained = workflow.run({"task_card": cultural_wall_task()}, RunnerOptions(),
                               only_state="category_constraint")
    workflow.gateway.call = lambda *_args, **_kwargs: QuestionCard(task_id="wall-task", questions=[])
    clarified = workflow.run(constrained, RunnerOptions(), only_state="intake_clarify")
    assert clarified["phase"] == "waiting_clarification"
    assert clarified["question_card"]["questions"]
    assert all(item["blocking"] for item in clarified["question_card"]["questions"])


def test_taskbook_markdown_pending_items_conflict_with_empty_structure(tmp_path: Path) -> None:
    workflow, _ = runner(tmp_path, offline=False)

    class Doc:
        confirmed_facts = []
        default_handling_for_unknowns = []
        markdown_body = "# 创作任务书\n\n## 待决事项\n\n- 成品尺寸待确认\n"

    workflow.gateway.call = lambda *_args, **_kwargs: Doc()
    with pytest.raises(ValueError, match="正文仍包含"):
        workflow.run({"state": "intake_clarify", "phase": "ready_to_draft",
                      "task_card": {**cultural_wall_task(), "unknowns": {}},
                      "clarification_transcript": []}, RunnerOptions(),
                     only_state="confirmation_build")


def test_rerun_clarification_restores_original_task_boundary(tmp_path: Path) -> None:
    workflow, store = runner(tmp_path)
    original = cultural_wall_task()
    atomic_json(store.root / "intake_task.json", original)
    answered = {**original, "known_facts": {"library_required_input_1": "3m x 2m"}}
    checkpoint = store.checkpoint("intake_clarify", {
        "state": "intake_clarify", "phase": "ready_to_draft", "task_card": answered,
        "clarification_transcript": [{"old": True}], "question_card": {"questions": []},
    })
    store.branch_from(checkpoint, name="rerun-clarify", mode="rerun_stage")
    rewound = store.resume()
    assert rewound["phase"] == "ready_for_clarification"
    assert rewound["task_card"] == original
    assert "clarification_transcript" not in rewound


def test_rerun_master_selection_keeps_five_candidates(tmp_path: Path) -> None:
    _, store = runner(tmp_path)
    candidates = [{"id": f"candidate-{index}"} for index in range(1, 6)]
    checkpoint = store.checkpoint("master_candidate_selection", {
        "state": "master_candidate_selection", "phase": "master_selected",
        "task_card": cultural_wall_task(), "candidates": candidates,
        "master_asset": candidates[0], "selected_master": {"candidate_id": "candidate-1"},
        "inspection": {"passed": False},
    })
    store.branch_from(checkpoint, name="rerun-master", mode="rerun_stage")
    rewound = store.resume()
    assert rewound["phase"] == "waiting_master_selection"
    assert rewound["candidates"] == candidates
    assert "master_asset" not in rewound and "inspection" not in rewound


def test_content_moderation_is_classified_before_http_400() -> None:
    class ProviderError(Exception):
        status_code = 400
        code = "InputTextSensitiveContentDetected"

    assert ModelExecutor.classify(ProviderError("bad request")) == ("content_moderation", False)


def test_model_call_error_runtime_exception_fields_are_writable() -> None:
    error = ModelCallError("x", False, "content_moderation", "req", "trace")
    error.__traceback__ = None
    error.__cause__ = ValueError("cause")
    error.__context__ = RuntimeError("context")
    assert isinstance(error.__cause__, ValueError)


def test_rework_prompt_excludes_runtime_asset_dictionary() -> None:
    ref = ReferenceImage(uri="artifact://artifact_123", role="current", source="asset",
                         sha256="a" * 64, order=0, reason="当前图")
    assembled = build_rework_context(
        specification="已确认：保持品牌色", constraints=["不增加标识"],
        feedback="提高标题清晰度", reference=ref,
    )
    assert "artifact_id" not in assembled["text"]
    assert "sha256" not in assembled["text"]
    assert assembled["references"][0]["uri"] == "artifact://artifact_123"
