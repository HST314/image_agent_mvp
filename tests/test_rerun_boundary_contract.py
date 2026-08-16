"""重跑分支头边界（ready_for_* 相位）的前后端契约回归。

重跑建分支（project_store._rewind_stage）把分支头落在某节点的输入边界上：
下游产物已按设计清空。边界上的"推进"必须重跑本节点（next_state 指向本
状态），且能力清单必须给出本节点的重启动作——否则前端没有任何合法入口，
工程卡死在只有 branch 能力的占位界面（test23 线上事故）。
"""
from __future__ import annotations

from pathlib import Path

from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from configs.runtime_policy import RuntimePolicy
from main_front import _capabilities
from storage.project_store import ProjectStore

CONFIG = Path(__file__).parents[1] / "configs/model_config.yaml"


def _task() -> dict:
    return {
        "task_id": "t", "project_id": "p",
        "source_refs": [{"ref_id": "s", "ref_type": "text"}],
        "deliverable_goal": "海报", "usage_context": "手机",
        "category_ref": {"category_id": "generic_visual_delivery", "version": "1.0"},
        "known_facts": {"主体": "产品"}, "unknowns": {},
    }


def _runner(tmp_path: Path, name: str = "p") -> tuple[WorkflowRunner, ProjectStore]:
    store = ProjectStore(tmp_path, name)
    store.create(RuntimePolicy(offline_mode=True).snapshot())
    return WorkflowRunner(store, CONFIG, offline_mode=True), store


# state → (构造源检查点所需额外数据, 边界相位, next_state 期望, 重启能力期望)
BOUNDARY_CASES = {
    "category_constraint": ({}, "ready_for_category_match", "category_constraint", "start_category_match"),
    "intake_clarify": ({}, "ready_for_clarification", "intake_clarify", "start_clarification"),
    "confirmation_build": ({}, "ready_for_taskbook", "confirmation_build", "build_taskbook"),
    "initial_candidate_generation": ({}, "ready_for_style_direction", "initial_candidate_generation", "prepare_style_direction"),
    "self_check_iteration": ({"asset": {"uri": "u", "sha256": "h"}}, "ready_for_quality_inspection", "self_check_iteration", "start_quality_inspection"),
    "final_approval": ({}, "ready_for_final_approval", "final_approval", "open_final_approval"),
}


def test_every_rerun_boundary_reruns_own_stage_and_exposes_restart_capability(tmp_path: Path) -> None:
    for state, (extra, phase, expected_target, capability) in BOUNDARY_CASES.items():
        runner, store = _runner(tmp_path, f"p_{state}")
        source = store.checkpoint(state, {"state": state, "phase": "completed", "task_card": _task(), **extra})
        store.branch_from(source, name=f"重跑-{state}", mode="rerun_stage")

        snapshot = store.resume()
        assert snapshot["phase"] == phase, state
        assert snapshot["state"] == state, state
        # 边界上的推进必须重跑本节点，而不是跨入下一节点
        assert runner.next_state(snapshot) == expected_target, state
        # 能力清单必须给出本节点重启动作，不能退化为只有 branch
        capabilities = _capabilities(store.manifest(), snapshot)
        assert capability in capabilities, state
        assert "branch" in capabilities, state


def test_master_selection_rerun_keeps_waiting_phase_and_candidates(tmp_path: Path) -> None:
    """主图选择重跑保留五张候选图与等待相位：推进留在本节点等待人工选择。"""
    runner, store = _runner(tmp_path)
    candidates = [{"id": f"candidate-{index}"} for index in range(1, 6)]
    source = store.checkpoint("master_candidate_selection", {
        "state": "master_candidate_selection", "phase": "master_selected",
        "task_card": _task(), "candidates": candidates,
        "master_asset": candidates[0], "selected_master": {"candidate_id": "candidate-1"},
    })
    store.branch_from(source, name="重跑主图", mode="rerun_stage")

    snapshot = store.resume()
    assert snapshot["phase"] == "waiting_master_selection"
    assert runner.next_state(snapshot) == "master_candidate_selection"
    assert "select_master" in _capabilities(store.manifest(), snapshot)


def test_style_rerun_branch_regenerates_five_candidates_end_to_end(tmp_path: Path) -> None:
    """test23 事故路径的端到端修复验证：
    艺术风格重跑分支头 → 推进 → 重新准备五风格并生成五张候选图，
    停在 waiting_master_selection（而不是零候选的主图选择死胡同）。"""
    runner, store = _runner(tmp_path)
    # 推进到任务书人工等待点（offline 全 auto 放行，无需模型）。
    first = runner.run({"task_card": _task()}, RunnerOptions())
    assert first["state"] == "confirmation_build" and first["phase"] == "waiting_human_approval"
    # 确认任务书 → 准备五风格 → 生成五候选 → 停在主图选择。
    second = runner.run(first, RunnerOptions(task_approved=True, actor="reviewer"))
    assert second["state"] == "master_candidate_selection"
    assert second["phase"] == "waiting_master_selection"
    assert len(second["candidates"]) == 5

    style_checkpoint = next(
        item["checkpoint_id"] for item in store.checkpoints.list()
        if item["branch"] == "main" and item["state"] == "initial_candidate_generation"
    )
    store.branch_from(style_checkpoint, name="艺术风格-rerun", mode="rerun_stage")
    boundary = store.resume()
    assert boundary["phase"] == "ready_for_style_direction"
    assert not boundary.get("candidates")
    assert runner.next_state(boundary) == "initial_candidate_generation"

    # 从边界推进：重跑艺术风格节点本身，最终重新生成五张候选图。
    rerun = runner.run(boundary, RunnerOptions())
    assert rerun["state"] == "master_candidate_selection"
    assert rerun["phase"] == "waiting_master_selection"
    assert len(rerun["candidates"]) == 5
