"""任务书阶段的未知项策略契约回归（test21 故障固化）。

契约要点：
- 非阻塞 ≠ 有安全默认；策略三态 blocking / safe_default / out_of_scope；
- safe_default 必须提供可执行 default_value，且不得与"保持未确认"语义共存；
- 任务书主准入只看结构化阻塞字段；正文关键词扫描只是 consistency check，
  命中后自动定向修复或回退确定性任务书，不再把工程打进不可恢复失败；
- 真实阻塞项仍拦截，但进入可恢复的 waiting_taskbook_revision 而非抛错死路。
"""
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_core.models import (
    ImageTaskCard,
    RequiredQuestion,
    SourceRef,
)
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from configs.runtime_policy import RuntimePolicy
from skills.category_library_adapter import CategoryLibraryAdapter
from storage.project_store import ProjectStore

CONFIG = Path(__file__).parents[1] / "tests/fixtures/model_config.yaml"
LIBRARY = Path(__file__).parents[1] / "skills/category_libraries/advertising_category_library_v2.json"


def _runner(tmp_path: Path, *, offline: bool = True) -> tuple[WorkflowRunner, ProjectStore]:
    store = ProjectStore(tmp_path, "wall-project")
    store.create(RuntimePolicy(offline_mode=offline).snapshot())
    return WorkflowRunner(store, CONFIG, offline_mode=offline), store


def _culture_wall_skill():
    task = ImageTaskCard(
        task_id="match", project_id="wall-project",
        source_refs=[SourceRef(ref_id="brief", ref_type="text", excerpt="党建文化墙设计")],
        deliverable_goal="设计一组党建文化墙", usage_context="室内展陈",
    )
    match = CategoryLibraryAdapter(LIBRARY).load_for_task(task)
    assert match is not None and match.skill.display_name == "文化墙"
    return match.skill


def _legacy_unknown(label: str) -> dict:
    """test21 旧检查点里的条目形态：非阻塞却被标成 has_safe_default，且处理语义矛盾。"""
    return {
        "blocking": False,
        "has_safe_default": True,
        "default_handling": "缺失时保持未确认，不得在生成阶段自行补全。",
        "evidence": "广告品类库：文化墙",
        "impact": "该品类的制作、交付或验收依赖此信息。",
        "label": label,
        "question": label,
        "options": [
            {"label": "现在补充（请注明）", "description": f"提供“{label}”的可执行内容"},
            {"label": "采用明确默认（请注明）", "description": "写明经人工确认的保守默认值"},
        ],
    }


def _test21_task() -> dict:
    """镜像 test21 检查点 4：5 个阻塞项已回答销账，3 个旧式非阻塞项保留。"""
    return {
        "task_id": "wall-task", "project_id": "wall-project",
        "source_refs": [{"ref_id": "brief", "ref_type": "text", "excerpt": "党建文化墙设计"}],
        "deliverable_goal": "设计一组党建文化墙", "usage_context": "室内展陈",
        "known_facts": {
            "audience": "单位员工与来访领导",
            "tone": "庄重、现代",
            "output_spec": "墙面效果图",
            "library_required_input_1": "5m*8m",
            "library_required_input_2": "1 面，不分批",
            "library_required_input_3": "室内大厅，长期使用",
            "library_required_input_5": "现在，线上",
            "library_required_input_6": "仅设计，不含安装",
        },
        "unknowns": {
            "library_required_input_4": _legacy_unknown("观看距离或使用方式"),
            "library_required_input_7": _legacy_unknown("客户预算口径"),
            "library_required_input_8": _legacy_unknown("是否开具增值税专用发票"),
        },
        "asset_inputs": [], "status": "draft",
    }


def _test21_snapshot() -> dict:
    skill = _culture_wall_skill()
    return {
        "state": "intake_clarify", "phase": "ready_to_draft",
        "task_card": _test21_task(),
        "clarification_transcript": [],
        "clarification_asked_count": 5,
        "category_constraint_current": {
            "version_id": "category-constraint-v1",
            "category_id": skill.category_id,
            "skill": skill.model_dump(mode="json"),
        },
    }


def test_safe_default_requires_executable_default_value() -> None:
    with pytest.raises(ValidationError):
        RequiredQuestion(field="f", question="q", blocks_generation=False,
                         handling_strategy="safe_default")


def test_safe_default_rejects_contradictory_handling() -> None:
    with pytest.raises(ValidationError):
        RequiredQuestion(field="f", question="q", blocks_generation=False,
                         handling_strategy="safe_default", default_value="保守默认",
                         default_handling="缺失时保持未确认，不得在生成阶段自行补全。")


def test_blocking_strategy_must_be_consistent() -> None:
    with pytest.raises(ValidationError):
        RequiredQuestion(field="f", question="q", blocks_generation=True,
                         handling_strategy="out_of_scope")
    with pytest.raises(ValidationError):
        RequiredQuestion(field="f", question="q", blocks_generation=False,
                         handling_strategy="blocking")


def test_non_blocking_defaults_to_out_of_scope_not_safe_default() -> None:
    question = RequiredQuestion(field="f", question="q", blocks_generation=False)
    assert question.resolved_strategy() == "out_of_scope"


def test_library_strategies_for_culture_wall() -> None:
    skill = _culture_wall_skill()
    strategies = {q.question: q.resolved_strategy() for q in skill.required_questions}
    assert strategies["观看距离或使用方式"] == "safe_default"
    assert strategies["客户预算口径"] == "out_of_scope"
    assert strategies["是否开具增值税专用发票"] == "out_of_scope"
    assert strategies["成品尺寸与展开尺寸"] == "blocking"


def test_test21_snapshot_reaches_waiting_human_approval(tmp_path: Path) -> None:
    """5 阻塞已答 + 3 非阻塞保留 → 成功生成任务书，旧条目完成策略迁移。"""
    workflow, _ = _runner(tmp_path)
    result = workflow.run(_test21_snapshot(), RunnerOptions(), only_state="confirmation_build")

    assert result["phase"] == "waiting_human_approval"
    assert result["waiting"] is True
    spec = result["task_specification"]
    blocking = [fact for fact in spec["facts"] if fact["status"] == "blocking"]
    assert blocking == []
    tentative = {fact["label"]: fact["value"] for fact in spec["facts"] if fact["status"] == "tentative"}
    assert "按大厅常规观看距离" in tentative["library_required_input_4"]
    assert "本轮交付不包含" in tentative["library_required_input_7"]
    assert "本轮交付不包含" in tentative["library_required_input_8"]
    assert not workflow._markdown_has_unresolved_items(result["task_markdown"])

    unknowns = result["task_card"]["unknowns"]
    assert unknowns["library_required_input_4"]["handling_strategy"] == "safe_default"
    assert unknowns["library_required_input_4"]["default_value"]
    assert unknowns["library_required_input_7"]["handling_strategy"] == "out_of_scope"
    assert unknowns["library_required_input_8"]["handling_strategy"] == "out_of_scope"


def test_legacy_frozen_skill_policies_refreshed_from_current_library(tmp_path: Path) -> None:
    """检查点里冻结的旧版技能（无策略字段）按当前品类库回填后再迁移未知项。"""
    workflow, _ = _runner(tmp_path)
    snapshot = _test21_snapshot()
    frozen = snapshot["category_constraint_current"]["skill"]
    for question in frozen["required_questions"]:
        question.pop("handling_strategy", None)
        question.pop("default_value", None)
        if not question["blocks_generation"]:
            question["default_handling"] = "缺失时保持未确认，不得在生成阶段自行补全。"
    result = workflow.run(snapshot, RunnerOptions(), only_state="confirmation_build")

    assert result["phase"] == "waiting_human_approval"
    unknowns = result["task_card"]["unknowns"]
    assert unknowns["library_required_input_4"]["handling_strategy"] == "safe_default"
    assert "按大厅常规观看距离" in unknowns["library_required_input_4"]["default_value"]
    assert unknowns["library_required_input_7"]["handling_strategy"] == "out_of_scope"
    assert unknowns["library_required_input_8"]["handling_strategy"] == "out_of_scope"


def test_real_blocker_enters_recoverable_revision_instead_of_raising(tmp_path: Path) -> None:
    workflow, _ = _runner(tmp_path)
    snapshot = _test21_snapshot()
    # 品类库之外的真阻塞项（无已知事实可销账），必须拦截但可恢复。
    snapshot["task_card"]["unknowns"]["legal_review_clause"] = {
        "blocking": True, "has_safe_default": False,
        "label": "法务审核附加条款", "question": "法务审核附加条款",
        "impact": "未经法务确认的附加条款不得进入生成假设。",
        "options": [
            {"label": "现在补充（请注明）", "description": "提供法务确认的条款内容"},
            {"label": "保持阻塞并暂停", "description": "稍后处理"},
        ],
    }
    result = workflow.run(snapshot, RunnerOptions(), only_state="confirmation_build")

    assert result["phase"] == "waiting_taskbook_revision"
    assert result["waiting"] is True
    assert "legal_review_clause" in result["taskbook_revision_fields"]
    assert "answer_taskbook_revision" in result["taskbook_recovery_actions"]
    assert "regenerate_taskbook" in result["taskbook_recovery_actions"]
    assert result["question_card"]["questions"], "修订态应就地产出补充问题卡"

    card = result["question_card"]
    answers = {
        "question_card_id": card["question_card_id"],
        "answers": [{
            "question_id": question["question_id"],
            "selected_option_id": question["options"][0]["option_id"],
            "free_text": "法务已确认无附加条款",
            "skipped": False,
        } for question in card["questions"]],
    }
    resolved = workflow.run(result, RunnerOptions(clarification_answers=answers),
                            only_state="confirmation_build")
    assert resolved["phase"] == "waiting_human_approval"
    assert resolved["taskbook_revision_fields"] is None
    assert "legal_review_clause" not in resolved["task_card"]["unknowns"]
    assert resolved["task_card"]["known_facts"]["legal_review_clause"] == "法务已确认无附加条款"


def test_revision_apply_scope_boundaries(tmp_path: Path) -> None:
    """模型把非阻塞项误标 blocking 时，可用边界动作按明确默认/范围边界闭环。"""
    workflow, _ = _runner(tmp_path, offline=False)

    class _Unknown:
        def __init__(self, field):
            self.field = field

            class _Risk:
                value = "blocking"
            self.risk_level = _Risk()
            self.handling = "暂停下游生成，直到人工补充明确内容。"

    class Doc:
        confirmed_facts = []
        markdown_body = "# 创作任务书\n\n按已确认信息执行。\n"
        default_handling_for_unknowns = [
            _Unknown("观看距离或使用方式"), _Unknown("客户预算口径"),
            _Unknown("是否开具增值税专用发票"),
        ]

    class CleanDoc:
        """边界落地后模型不再误标，任务书可以闭环。"""
        confirmed_facts = []
        default_handling_for_unknowns = []
        markdown_body = "# 创作任务书\n\n按已确认信息执行。\n"

    calls = {"count": 0}

    def call(*_args, **_kwargs):
        calls["count"] += 1
        return Doc() if calls["count"] == 1 else CleanDoc()

    workflow.gateway.call = call
    result = workflow.run(_test21_snapshot(), RunnerOptions(), only_state="confirmation_build")

    assert result["phase"] == "waiting_taskbook_revision"
    assert set(result["taskbook_scope_boundary_fields"]) == {
        "library_required_input_4", "library_required_input_7", "library_required_input_8",
    }
    assert "apply_taskbook_scope_boundaries" in result["taskbook_recovery_actions"]

    applied = workflow.run(result, RunnerOptions(taskbook_action="apply_scope_boundaries"),
                           only_state="confirmation_build")
    assert applied["phase"] == "waiting_human_approval"
    facts = applied["task_card"]["known_facts"]
    assert "按大厅常规观看距离" in facts["library_required_input_4"]
    assert "本轮交付不包含" in facts["library_required_input_7"]
    assert "本轮交付不包含" in facts["library_required_input_8"]
    assert not applied["task_card"]["unknowns"]


def test_edited_markdown_with_open_wording_returns_to_revision_with_draft(tmp_path: Path) -> None:
    """人工编辑引入未闭环表述时保留草稿并给出修订动作，不再报错死路。"""
    workflow, _ = _runner(tmp_path)
    first = workflow.run(_test21_snapshot(), RunnerOptions(), only_state="confirmation_build")
    assert first["phase"] == "waiting_human_approval"

    edited = first["task_markdown"] + "\n- 字体大小待确认\n"
    result = workflow.run(first, RunnerOptions(edited_markdown=edited),
                          only_state="confirmation_build")
    assert result["phase"] == "waiting_taskbook_revision"
    assert result["taskbook_revision_draft"] == edited
    assert "edit_taskbook" in result["taskbook_recovery_actions"]
    assert "regenerate_taskbook" in result["taskbook_recovery_actions"]


def test_model_doc_with_open_prose_is_repaired_once(tmp_path: Path) -> None:
    """模型正文含待决措辞：一次定向修复成功即继续，无需人工介入。"""
    workflow, store = _runner(tmp_path, offline=False)

    class Doc:
        confirmed_facts = []
        default_handling_for_unknowns = []
        markdown_body = "# 创作任务书\n\n- 观看距离待确认\n"

    def call(_state, _role, _invoke, **kwargs):
        if kwargs.get("template_id") == "confirmation_build_repair":
            return "# 创作任务书\n\n- 观看距离按大厅常规基线执行\n"
        return Doc()

    workflow.gateway.call = call
    result = workflow.run(_test21_snapshot(), RunnerOptions(), only_state="confirmation_build")

    assert result["phase"] == "waiting_human_approval"
    assert "待确认" not in result["task_markdown"]
    events = (store.root / "events" / "events.jsonl").read_text(encoding="utf-8")
    assert "taskbook_auto_repaired" in events and "model_repair" in events
