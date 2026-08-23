"""提问偏好（question_preference）：积极追问接纳任务卡外新字段并写入任务书链路；
只问关键问题模式保持「仅未知项」旧契约。提问只发生在澄清阶段，不动后方流程。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.models import ImageTaskCard, SourceRef
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from configs.runtime_policy import RuntimePolicy
from interaction.confirmation_builder import specification_from_task
from interaction.question_generator import generate_question_card
from storage.project_store import ProjectStore

CONFIG = Path(__file__).parents[1] / "tests/fixtures/model_config.yaml"


def _task(unknowns=None, known_facts=None) -> ImageTaskCard:
    return ImageTaskCard(
        task_id="pref-task", project_id="pref-project",
        source_refs=[SourceRef(ref_id="brief", ref_type="text")],
        deliverable_goal="海报", usage_context="线上投放",
        known_facts=known_facts or {}, unknowns=unknowns or {},
    )


class _ProactiveModel:
    """模拟积极追问的澄清模型：提出一个任务卡未知项之外的新字段。"""

    assert_role_prompt = True

    def complete(self, prompt: str) -> str:
        if self.assert_role_prompt:
            assert "自动化平面设计" in prompt, "积极模式必须注入平面设计 Agent 角色设定"
        return json.dumps({"questions": [{
            "field": "画面文案",
            "question": "画面中需要呈现哪些文案？",
            "options": [
                {"option_id": "A", "label": "提供文案（请注明）", "description": "填写具体文案", "requires_free_text": True},
                {"option_id": "B", "label": "由 Agent 发挥", "description": "按任务书整体语境合理处理"},
            ],
            "recommended_option_id": "A",
            "impact": "决定画面文字内容。",
            "evidence": "简报未提供画面文案。",
            "missing": True, "has_safe_default": False, "blocking": True,
        }]}, ensure_ascii=False)


class _ManyQuestionsModel:
    def __init__(self) -> None:
        self.prompt = ""

    def complete(self, prompt: str) -> str:
        self.prompt = prompt
        questions = [{
            "field": f"主动字段_{index}",
            "question": f"请确认第 {index} 项创作要求？",
            "options": [
                {"option_id": "A", "label": "方案 A", "description": "按建议处理"},
                {"option_id": "B", "label": "方案 B", "description": "采用另一方案"},
            ],
            "recommended_option_id": "A",
            "impact": "影响出图质量。",
            "evidence": "当前任务信息未覆盖。",
            "missing": True, "has_safe_default": False, "blocking": True,
        } for index in range(1, 9)]
        return json.dumps({"questions": questions}, ensure_ascii=False)


def test_default_preference_is_proactive() -> None:
    assert RuntimePolicy().question_preference == "proactive"


def test_proactive_mode_accepts_new_fields() -> None:
    card = generate_question_card(_task(), _ProactiveModel(), question_preference="proactive")
    assert [question.field for question in card.questions] == ["画面文案"]


def test_blocking_only_mode_discards_new_fields() -> None:
    model = _ProactiveModel()
    model.assert_role_prompt = False
    card = generate_question_card(_task(), model, question_preference="blocking_only")
    assert card.questions == []


def test_proactive_mode_never_asks_known_facts() -> None:
    task = _task(known_facts={"画面文案": "春季新品 5 折起"})
    card = generate_question_card(task, _ProactiveModel(), question_preference="proactive")
    assert card.questions == []


def test_proactive_field_registers_nonblocking_and_flows_to_taskbook(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "projects", "pref-project")
    store.create(RuntimePolicy(offline_mode=True).snapshot())
    runner = WorkflowRunner(store, CONFIG, offline_mode=True)
    task = _task()
    card = generate_question_card(task, _ProactiveModel(), question_preference="proactive")

    # 登记进未知项：非阻塞 + 安全默认，跳过/预算耗尽不阻断任务书。
    task = runner._register_proactive_unknowns(task, card)
    entry = task.unknowns["画面文案"]
    assert entry["blocking"] is False
    assert entry["has_safe_default"] is True
    assert runner._blocking_unknowns(task) == []

    # 结构化回答 → 已知事实 → 任务书事实（后方节点经任务书/运行时提示词可见）。
    payload = {"question_card_id": card.question_card_id, "answers": [{
        "question_id": card.questions[0].question_id,
        "selected_option_id": "A", "free_text": "春季新品 5 折起", "skipped": False,
    }]}
    _, resolved = runner._answer_record(task, card, payload)
    assert resolved == {"画面文案": "春季新品 5 折起"}
    task = task.model_copy(update={
        "known_facts": {**task.known_facts, **resolved},
        "unknowns": {key: value for key, value in task.unknowns.items() if key not in resolved},
    })
    assert "画面文案" not in task.unknowns
    spec = specification_from_task(task)
    fact = next(fact for fact in spec.facts if fact.label == "画面文案")
    assert "春季新品" in fact.value


def test_clarify_runner_proactive_roundtrip(tmp_path: Path, monkeypatch) -> None:
    """在线链路：_clarify 把提问偏好传给问题生成器，新字段登记后进入等待回答态。"""
    store = ProjectStore(tmp_path / "projects", "pref-online")
    store.create(RuntimePolicy(offline_mode=False).snapshot())
    runner = WorkflowRunner(store, CONFIG, offline_mode=False)
    assert runner.policy.question_preference == "proactive"
    monkeypatch.setattr(WorkflowRunner, "_text", lambda self, route: _ProactiveModel())
    monkeypatch.setattr(
        runner.gateway, "call",
        lambda state, role, invoke, **kwargs: invoke(object()),
    )

    result = runner.run({"task_card": _task().model_dump(mode="json")}, RunnerOptions(), only_state="intake_clarify")

    assert result["phase"] == "waiting_clarification"
    assert [q["field"] for q in result["question_card"]["questions"]] == ["画面文案"]
    entry = result["task_card"]["unknowns"]["画面文案"]
    assert entry["blocking"] is False and entry["has_safe_default"] is True

    # 回答后无新信息可问（模型仍返回同一字段，但已进入已知事实）→ 直接进入任务书。
    card = result["question_card"]
    answers = {"question_card_id": card["question_card_id"], "answers": [{
        "question_id": card["questions"][0]["question_id"],
        "selected_option_id": "A", "free_text": "春季新品 5 折起", "skipped": False,
    }]}
    followup = runner.run(result, RunnerOptions(clarification_answers=answers), only_state="intake_clarify")
    assert followup["phase"] == "ready_to_draft"
    assert followup["task_card"]["known_facts"]["画面文案"] == "春季新品 5 折起"
    assert followup["task_card"]["unknowns"] == {}


@pytest.mark.parametrize(
    ("max_auto_questions", "total_budget", "expected"),
    [(5, 7, 5), (5, 4, 4)],
)
def test_online_clarify_uses_runtime_question_limits(
    tmp_path: Path, monkeypatch, max_auto_questions: int,
    total_budget: int, expected: int,
) -> None:
    store = ProjectStore(tmp_path / "projects", f"limits-{total_budget}")
    store.create(RuntimePolicy(
        offline_mode=False,
        max_auto_questions=max_auto_questions,
        clarification_total_budget=total_budget,
    ).snapshot())
    runner = WorkflowRunner(store, CONFIG, offline_mode=False)
    model = _ManyQuestionsModel()
    monkeypatch.setattr(WorkflowRunner, "_text", lambda self, route: model)
    monkeypatch.setattr(
        runner.gateway, "call",
        lambda state, role, invoke, **kwargs: invoke(object()),
    )

    result = runner.run(
        {"task_card": _task().model_dump(mode="json")},
        RunnerOptions(), only_state="intake_clarify",
    )

    assert len(result["question_card"]["questions"]) == expected
    assert f"最多 {expected} 问" in model.prompt
