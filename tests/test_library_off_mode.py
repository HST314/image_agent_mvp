"""「不使用数据库」（release=off）模式：品类库/风格库跳过注入但阶段保留并自动通过。"""
from pathlib import Path

import pytest

from agent_core.models import ImageTaskCard
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from configs.runtime_policy import RuntimePolicy, SkillInvocationPolicyConfig
from interaction.confirmation_builder import specification_from_task
from storage.project_store import ProjectStore

CONFIG = Path(__file__).parents[1] / "configs/model_config.yaml"


def _task_payload() -> dict:
    return {
        "task_id": "off-mode", "project_id": "off-mode",
        "source_refs": [{"ref_id": "brief", "ref_type": "brief", "excerpt": "企业文化墙"}],
        "deliverable_goal": "企业文化墙", "usage_context": "办公室墙面",
        "known_facts": {"主题": "企业价值观"}, "unknowns": {}, "asset_inputs": [],
        "status": "draft",
    }


def _off_policy(**overrides) -> RuntimePolicy:
    return RuntimePolicy(
        offline_mode=True,
        category_constraint=SkillInvocationPolicyConfig(release="off"),
        style_direction=SkillInvocationPolicyConfig(release="off"),
        **overrides,
    )


def _run_to_taskbook(store: ProjectStore) -> tuple[WorkflowRunner, dict]:
    runner = WorkflowRunner(store, CONFIG, offline_mode=True)
    return runner, runner.run({"task_card": _task_payload()}, RunnerOptions())


def test_category_off_skips_library_and_never_waits(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "projects", "off-category")
    store.create(_off_policy().snapshot())
    runner, result = _run_to_taskbook(store)

    # 品类阶段自动通过：不留等待、不注入品类库未知项。
    assert result["state"] == "confirmation_build"
    assert result["phase"] == "waiting_human_approval"
    current = result["category_constraint_current"]
    assert current["disabled"] is True and current["skill"] is None
    assert current["decision"] == "library_disabled"
    assert result["category_constraint_approval"] == {
        "version_id": current["version_id"], "actor": "system:off",
    }
    task = ImageTaskCard.model_validate(result["task_card"])
    assert not any(field.startswith("library_required_input_") for field in task.unknowns)
    # 澄清阶段保留但不由品类库驱动：brief 无未知项时不产生提问。
    assert not result.get("clarification_transcript")
    events = [event["type"] for event in store.history()]
    assert "category_constraint_matched" in events


def test_category_off_clarify_still_asks_brief_unknowns(tmp_path: Path) -> None:
    """A1：澄清阶段不动——brief 自带的未知项仍会被提问，只是不再注入品类库 8 条。"""
    store = ProjectStore(tmp_path / "projects", "off-category-clarify")
    store.create(_off_policy().snapshot())
    payload = _task_payload()
    payload["unknowns"] = {"output_spec": "待确认"}
    runner = WorkflowRunner(store, CONFIG, offline_mode=True)
    result = runner.run({"task_card": payload}, RunnerOptions())
    assert result["state"] == "intake_clarify"
    assert result["phase"] == "waiting_clarification"
    fields = {q["field"] for q in result["question_card"]["questions"]}
    assert "output_spec" in fields
    assert not any(field.startswith("library_required_input_") for field in fields)


def _approved_off_snapshot(concurrency: int) -> dict:
    task = ImageTaskCard.model_validate(_task_payload())
    spec = specification_from_task(task)
    return {
        "state": "confirmation_build", "domain_state": "task_approval",
        "task_card": task.model_dump(mode="json"),
        "task_specification": spec.model_dump(mode="json"),
        "task_revision": {"revision_hash": "approved-revision"},
        "task_approval": {"revision_hash": "approved-revision", "actor": "reviewer"},
        "category_constraint_current": {
            "version_id": "category-constraint-v1", "version": 1,
            "category_id": None, "category_name": None, "score": 0,
            "decision": "library_disabled", "disabled": True, "skill": None,
            "constraint_hash": "off",
        },
        "category_constraint_approval": {"version_id": "category-constraint-v1", "actor": "system:off"},
    }


@pytest.mark.parametrize("concurrency", [5, 3])
def test_style_off_renders_candidate_concurrency_free_directions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrency: int,
) -> None:
    """B：生图个数由 candidate_concurrency 控制，提示词不注入风格库内容。"""
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(tmp_path / "projects", f"off-style-{concurrency}")
    store.create(_off_policy(candidate_concurrency=concurrency).snapshot())
    runner = WorkflowRunner(store, CONFIG, offline_mode=True)

    result = runner.run(_approved_off_snapshot(concurrency), RunnerOptions(),
                        only_state="initial_candidate_generation")

    # C1：自动通过，无人工放行等待。
    assert result["phase"] == "candidate_generation_completed"
    assert len(result["candidates"]) == concurrency
    assert len(result["render_plans"]) == concurrency
    assert {plan["style_id"] for plan in result["render_plans"]} == {
        f"free-{index + 1}" for index in range(concurrency)
    }
    assert result["skill_invocations"]["style_library"]["disabled"] is True
    assert result["skill_invocations"]["style_library"]["selections"] == []
    assert result["skill_invocations"]["category_library"] == {"source": "广告品类库", "disabled": True}
    assert result["skill_invocation_current"]["decision"] == "library_disabled"
    for plan in result["render_plans"]:
        assert "未使用艺术风格库" in plan["prompt_text"]
        assert "未使用广告品类库" in plan["prompt_text"]
        assert "风格参考" not in plan["prompt_text"]
    # 主图选择接受 candidate_concurrency 张候选。
    selected = runner.run(result, RunnerOptions(selected_id="candidate-1", actor="reviewer"),
                          only_state="master_candidate_selection")
    assert selected["phase"] == "master_selected"


def test_style_off_full_flow_from_brief_to_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """两步设置均为 off：从 brief 出发经澄清/任务书，任务书确认后直接进入生图。"""
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(tmp_path / "projects", "off-full-flow")
    store.create(_off_policy().snapshot())
    runner, waiting = _run_to_taskbook(store)
    assert waiting["phase"] == "waiting_human_approval"

    approved = runner.run(waiting, RunnerOptions(task_approved=True, actor="reviewer"))
    assert approved["phase"] == "waiting_master_selection"
    assert len(approved["candidates"]) == 5
    assert approved["skill_invocations"]["style_library"]["disabled"] is True
    # 中间不停留任何人工放行点。
    phases = [snap.get("phase") for snap in (approved,)]
    assert "waiting_skill_approval" not in phases
    assert "waiting_category_approval" not in phases


def test_library_modes_stay_five_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """使用风格库（auto）时仍固定 5 张候选，不受 off 逻辑影响。"""
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(tmp_path / "projects", "library-auto")
    store.create(RuntimePolicy(offline_mode=True).snapshot())
    runner = WorkflowRunner(store, CONFIG, offline_mode=True)
    snapshot = _approved_off_snapshot(5)
    snapshot["category_constraint_current"] = {}
    snapshot["category_constraint_approval"] = None
    result = runner.run(snapshot, RunnerOptions(), only_state="initial_candidate_generation")
    assert result["phase"] == "candidate_generation_completed"
    assert len(result["candidates"]) == 5
    assert "disabled" not in result["skill_invocations"]["style_library"]
