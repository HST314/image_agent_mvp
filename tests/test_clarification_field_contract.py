import json
from pathlib import Path

from agent_core.models import (
    AppliesWhen,
    CategorySkill,
    ImageTaskCard,
    PromptInjection,
    QuestionCard,
    QuestionItem,
    QuestionOption,
    RequiredQuestion,
    SkillStatus,
    SourceRef,
)
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from interaction.question_generator import generate_question_card
from storage.project_store import ProjectStore


def _task(unknowns, known_facts=None) -> ImageTaskCard:
    return ImageTaskCard(
        task_id="field-contract",
        project_id="field-contract-project",
        source_refs=[SourceRef(ref_id="brief", ref_type="text")],
        deliverable_goal="文化墙",
        usage_context="室内",
        known_facts=known_facts or {},
        unknowns=unknowns,
    )


def _unknown(label: str, *, safe: bool = False) -> dict:
    return {
        "label": label,
        "question": label,
        "blocking": not safe,
        "has_safe_default": safe,
        "impact": "影响制作与交付",
        "default_handling": "采用允许的保守默认值" if safe else None,
        "options": [
            {"label": "现在补充（请注明）", "description": "填写明确答案"},
            {"label": "人工确认（请注明）", "description": "填写人工确认值"},
        ],
    }


class _Model:
    def __init__(self, field: str, fingerprint: str = "generated_fp_1") -> None:
        self.field = field
        self.fingerprint = fingerprint

    def complete(self, _prompt: str) -> str:
        return json.dumps({"questions": [{
            "field": self.field,
            "question": "请提供成品尺寸与展开尺寸。",
            "options": ["现在补充", "稍后人工确认"],
            "recommended_option_id": "A",
            "impact": "影响制作",
            "missing": True,
            "has_safe_default": False,
            "blocking": True,
            "semantic_fingerprint": self.fingerprint,
        }]}, ensure_ascii=False)


def test_model_display_alias_is_normalized_and_fingerprint_is_field_owned() -> None:
    task = _task({"library_required_input_1": _unknown("成品尺寸与展开尺寸")})
    card = generate_question_card(task, _Model("成品尺寸与展开尺寸"))

    assert [question.field for question in card.questions] == ["library_required_input_1"]
    assert card.questions[0].semantic_fingerprint != "generated_fp_1"
    repeated = generate_question_card(
        task,
        _Model("library_required_input_1", "another_generated_fp"),
        previous_fingerprints={card.questions[0].semantic_fingerprint},
    )
    assert repeated.questions == []


def test_invalid_model_field_is_discarded_and_local_question_fills_gap() -> None:
    task = _task({"library_required_input_1": _unknown("成品尺寸与展开尺寸")})
    card = generate_question_card(task, _Model("untrusted_model_field"))

    assert [question.field for question in card.questions] == ["library_required_input_1"]


def test_category_answer_alias_migrates_to_internal_id_and_clears_unknown(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "field-contract-project")
    store.create()
    runner = WorkflowRunner(store, Path("tests/fixtures/model_config.yaml"), offline_mode=True)
    skill = CategorySkill(
        category_id="wall",
        version="1",
        display_name="文化墙",
        applies_when=AppliesWhen(),
        required_questions=[RequiredQuestion(
            field="library_required_input_1",
            question="成品尺寸与展开尺寸",
            blocks_generation=True,
        )],
        prompt_injection=PromptInjection(),
        status=SkillStatus.APPROVED,
    )
    task = _task(
        {"library_required_input_1": _unknown("成品尺寸与展开尺寸")},
        known_facts={"成品尺寸与展开尺寸": "5m × 8m"},
    )

    reconciled = runner._apply_category_unknowns(task, skill)

    assert reconciled.known_facts["library_required_input_1"] == "5m × 8m"
    assert "library_required_input_1" not in reconciled.unknowns


def test_answer_record_reconciles_legacy_display_field_to_internal_id(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "field-contract-project")
    store.create()
    runner = WorkflowRunner(store, Path("tests/fixtures/model_config.yaml"), offline_mode=True)
    task = _task({"library_required_input_1": _unknown("成品尺寸与展开尺寸")})
    card = QuestionCard(task_id=task.task_id, questions=[QuestionItem(
        question_id="legacy-question",
        field="成品尺寸与展开尺寸",
        question="请提供成品尺寸与展开尺寸。",
        options=[
            QuestionOption(option_id="A", label="填写", description="填写答案", requires_free_text=True),
            QuestionOption(option_id="B", label="暂停", description="稍后处理"),
        ],
        recommended_option_id="A",
        impact="影响制作",
        blocking=True,
    )])
    payload = {
        "question_card_id": card.question_card_id,
        "answers": [{
            "question_id": "legacy-question",
            "selected_option_id": "A",
            "free_text": "5m × 8m",
            "skipped": False,
        }],
    }

    _, resolved = runner._answer_record(task, card, payload)

    assert resolved == {"library_required_input_1": "5m × 8m"}


def test_five_unique_blockers_advance_after_exactly_five_answers(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "field-contract-project")
    store.create()
    runner = WorkflowRunner(store, Path("tests/fixtures/model_config.yaml"), offline_mode=True)
    unknowns = {
        f"library_required_input_{index}": _unknown(label)
        for index, label in enumerate([
            "成品尺寸与展开尺寸",
            "数量及是否分批",
            "使用环境与寿命",
            "交期与交付地点",
            "安装包装运输范围",
        ], start=1)
    }
    state = {"task_card": _task(unknowns).model_dump(mode="json")}

    for expected_count in (3, 5):
        state = runner.run(state, RunnerOptions(), only_state="intake_clarify")
        card = state["question_card"]
        assert state["clarification_asked_count"] == expected_count
        answers = {
            "question_card_id": card["question_card_id"],
            "answers": [{
                "question_id": question["question_id"],
                "selected_option_id": question["options"][0]["option_id"],
                "free_text": f"答案-{question['field']}",
                "skipped": False,
            } for question in card["questions"]],
        }
        state = runner.run(
            state,
            RunnerOptions(clarification_answers=answers),
            only_state="intake_clarify",
        )

    assert state["phase"] == "ready_to_draft"
    assert state["clarification_asked_count"] == 5
    assert state["task_card"]["unknowns"] == {}


def test_budget_exhaustion_is_recoverable_and_safe_defaults_are_explicit(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "field-contract-project")
    store.create()
    runner = WorkflowRunner(store, Path("tests/fixtures/model_config.yaml"), offline_mode=True)
    task = _task({
        "library_required_input_1": _unknown("尺寸"),
        "optional_finish": _unknown("表面处理", safe=True),
    })
    state = {
        "state": "intake_clarify",
        "task_card": task.model_dump(mode="json"),
        "clarification_asked_count": runner.policy.clarification_total_budget,
    }

    review = runner.run(state, RunnerOptions(), only_state="intake_clarify")
    assert review["phase"] == "waiting_clarification_review"
    assert review["waiting"] is True
    assert review["clarification_blocking_fields"] == ["library_required_input_1"]
    assert "apply_safe_defaults" in review["clarification_recovery_actions"]

    updated = runner.run(
        review,
        RunnerOptions(clarification_action="apply_safe_defaults"),
        only_state="intake_clarify",
    )
    assert "optional_finish" not in updated["task_card"]["unknowns"]
    assert updated["task_card"]["known_facts"]["optional_finish"] == "采用允许的保守默认值"
    assert updated["phase"] == "waiting_clarification_review"
