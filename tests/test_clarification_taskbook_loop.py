from pathlib import Path

import pytest

from agent_core.models import QuestionCard, QuestionItem, QuestionOption
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from storage.project_store import ProjectStore


def _task(*, unknowns=None):
    return {
        "task_id": "loop-task",
        "project_id": "loop-project",
        "source_refs": [{"ref_id": "source-1", "ref_type": "text"}],
        "deliverable_goal": "发布海报",
        "usage_context": "手机端",
        "known_facts": {},
        "unknowns": unknowns or {},
    }


def _card(requires_free_text=True):
    return QuestionCard(task_id="loop-task", questions=[QuestionItem(
        question_id="q1", field="mission_name", question="任务名称是什么？",
        options=[
            QuestionOption(option_id="A", label="默认名称", description="使用默认名称"),
            QuestionOption(option_id="D", label="其他（请注明）", description="使用自定义名称",
                           requires_free_text=requires_free_text),
        ], recommended_option_id="A", impact="影响任务表达", blocking=True,
        semantic_fingerprint="mission-name-v1",
    )])


def _runner(tmp_path, *, offline=True):
    store = ProjectStore(tmp_path, "loop-project")
    store.create()
    return WorkflowRunner(store, Path("tests/fixtures/model_config.yaml"), offline_mode=offline)


def test_other_option_requires_concrete_free_text(tmp_path):
    runner = _runner(tmp_path)
    state = {"state": "intake_clarify", "phase": "waiting_clarification",
             "task_card": _task(), "question_card": _card().model_dump(mode="json")}

    with pytest.raises(ValueError, match="必须填写具体内容"):
        runner.run(state, RunnerOptions(clarification_answers={"question_card_id": state["question_card"]["question_card_id"], "answers": [{
            "question_id": "q1", "selected_option_id": "D", "free_text": ""
        }]}), only_state="intake_clarify")


def test_structured_answer_must_match_current_question_card(tmp_path):
    runner = _runner(tmp_path)
    state = {"state": "intake_clarify", "phase": "waiting_clarification",
             "task_card": _task(), "question_card": _card().model_dump(mode="json")}

    with pytest.raises(ValueError, match="问题卡已失效"):
        runner.run(state, RunnerOptions(clarification_answers={"question_card_id": "stale-card", "answers": [{
            "question_id": "q1", "selected_option_id": "A", "free_text": None,
        }]}), only_state="intake_clarify")


def test_answer_is_persisted_structurally_and_reanalysed(tmp_path):
    runner = _runner(tmp_path, offline=False)
    calls = []

    def call(state, role, invoke, **kwargs):
        calls.append((state, kwargs["variables"]))
        return QuestionCard(task_id="loop-task", questions=[])

    runner.gateway.call = call
    state = {"state": "intake_clarify", "phase": "waiting_clarification",
             "task_card": _task(), "question_card": _card().model_dump(mode="json"),
             "clarification_asked_count": 1, "previous_fingerprints": ["mission-name-v1"]}
    result = runner.run(state, RunnerOptions(clarification_answers={"question_card_id": state["question_card"]["question_card_id"], "answers": [{
        "question_id": "q1", "selected_option_id": "D", "free_text": "春日发布会"
    }]}), only_state="intake_clarify")

    assert calls[0][0] == "intake_clarify"
    assert calls[0][1]["clarification_transcript"][0]["answer_record"]["answers"][0]["free_text"] == "春日发布会"
    assert result["task_card"]["known_facts"]["mission_name"] == "春日发布会"
    assert result["phase"] == "ready_to_draft"
    assert result["task_specification"] is None and result["task_approval"] is None


def test_budget_exhaustion_with_blocker_waits_for_recoverable_review(tmp_path):
    runner = _runner(tmp_path)
    blocker = {"blocking": True, "has_safe_default": False, "impact": "影响输出"}
    state = {"state": "intake_clarify", "task_card": _task(unknowns={"name": blocker}),
             "clarification_asked_count": runner.policy.clarification_total_budget}
    result = runner.run(state, RunnerOptions(), only_state="intake_clarify")
    assert result["phase"] == "waiting_clarification_review"
    assert result["waiting"] is True
    assert result["clarification_blocking_fields"] == ["name"]
    assert "increase_budget" in result["clarification_recovery_actions"]


def test_confirmation_build_is_a_reasoning_model_boundary(tmp_path):
    runner = _runner(tmp_path, offline=False)
    calls = []

    class Doc:
        confirmed_facts = []
        default_handling_for_unknowns = []
        markdown_body = "# 模型重写的任务书\n\n这是综合理解后的执行说明。\n"

    def call(state, role, invoke, **kwargs):
        calls.append((state, kwargs))
        return Doc()

    runner.gateway.call = call
    result = runner.run({"state": "intake_clarify", "phase": "ready_to_draft",
                         "task_card": _task(), "clarification_transcript": []},
                        RunnerOptions(), only_state="confirmation_build")

    assert calls[0][0] == "confirmation_build"
    assert calls[0][1]["template_version"] == "3"
    assert result["task_markdown"].startswith("# 模型重写的任务书")
    assert result["phase"] == "waiting_human_approval"


def test_taskbook_approval_preserves_reasoning_model_markdown(tmp_path):
    runner = _runner(tmp_path, offline=False)
    authored = "# 模型重写的任务书\n\n这是综合理解后的执行说明。\n"

    class Doc:
        confirmed_facts = []
        default_handling_for_unknowns = []
        markdown_body = authored

    runner.gateway.call = lambda *_args, **_kwargs: Doc()
    first = runner.run({"state": "intake_clarify", "phase": "ready_to_draft",
                        "task_card": _task(), "clarification_transcript": []},
                       RunnerOptions(), only_state="confirmation_build")
    approved = runner.run(first, RunnerOptions(task_approved=True, actor="reviewer"),
                          only_state="confirmation_build")

    assert approved["task_markdown"] == authored
    assert approved["task_revision"]["revision_hash"] == first["task_revision"]["revision_hash"]
    assert approved["task_approval"]["revision_hash"] == first["task_revision"]["revision_hash"]
