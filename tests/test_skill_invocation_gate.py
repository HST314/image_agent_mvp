"""Independent skill-invocation approval gate and auditable regeneration."""
from pathlib import Path

from agent_core.models import ImageTaskCard
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from configs.runtime_policy import RuntimePolicy, SkillInvocationPolicyConfig
from interaction.confirmation_builder import specification_from_task
from storage.project_store import ProjectStore


def _approved_snapshot() -> dict:
    task = ImageTaskCard.model_validate({
        "task_id": "skill-gate", "project_id": "skill-gate",
        "source_refs": [{"ref_id": "brief", "ref_type": "brief", "excerpt": "通用视觉海报"}],
        "deliverable_goal": "通用视觉海报", "usage_context": "内部审核",
        "category_ref": {"category_id": "generic_visual_delivery", "version": "1.0"},
        "known_facts": {"audience": "审核人员"}, "unknowns": {}, "asset_inputs": [],
        "status": "draft",
    })
    spec = specification_from_task(task)
    return {
        "state": "confirmation_build", "domain_state": "task_approval",
        "task_card": task.model_dump(mode="json"),
        "task_specification": spec.model_dump(mode="json"),
        "task_revision": {"revision_hash": "approved-revision"},
        "task_approval": {"revision_hash": "approved-revision", "actor": "reviewer"},
    }


def test_manual_skill_gate_retries_with_avoidance_and_renders_only_after_approval(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    policy = RuntimePolicy(
        offline_mode=True,
        skill_invocation=SkillInvocationPolicyConfig(release="manual"),
    )
    store = ProjectStore(tmp_path / "projects", "skill-gate")
    store.create(policy.snapshot())
    runner = WorkflowRunner(store, Path(__file__).parents[1] / "configs/model_config.yaml", offline_mode=True)

    first = runner.run(_approved_snapshot(), RunnerOptions(), only_state="initial_candidate_generation")
    assert first["phase"] == "waiting_skill_approval"
    assert first["domain_state"] == "skill_approval"
    assert "candidates" not in first
    assert len(first["render_plans"]) == 5
    assert len(first["skill_invocation_history"]) == 1
    assert not (store.root / "runtime/prompts.jsonl").exists()

    retried = runner.run(
        first,
        RunnerOptions(skill_action="retry", actor="reviewer"),
        only_state="initial_candidate_generation",
    )
    assert retried["phase"] == "waiting_skill_approval"
    assert retried["domain_state"] == "skill_approval"
    assert len(retried["skill_invocation_history"]) == 2
    assert retried["skill_invocation_history"][0]["decision"] == "rejected"
    context = retried["skill_invocation_current"]["avoidance_context"]
    assert context["previous_version_id"] == "skill-invocation-v1"
    old_styles = {item["style_id"] for item in first["style_selections"]}
    new_styles = {item["style_id"] for item in retried["style_selections"]}
    assert old_styles.isdisjoint(new_styles)
    assert context["excluded_style_ids"] == [item["style_id"] for item in first["style_selections"]]
    assert not (store.root / "runtime/prompts.jsonl").exists()

    approved = runner.run(
        retried,
        RunnerOptions(skill_action="approve", actor="reviewer"),
        only_state="initial_candidate_generation",
    )
    assert approved["phase"] == "candidate_generation_completed"
    assert approved["domain_state"] == "five_render"
    assert approved["skill_invocation_approval"] == {
        "version_id": "skill-invocation-v2", "actor": "reviewer",
    }
    assert len(approved["candidates"]) == 5
    assert approved["skill_invocation_history"][-1]["decision"] == "approved"
    records = (store.root / "runtime/prompts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(records) == 10

    event_types = [event["type"] for event in store.history()]
    assert "skill_invocation_retried" in event_types
    assert "skill_invocation_approved" in event_types


def test_auto_skill_gate_keeps_continuous_five_render_flow(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(tmp_path / "projects", "skill-auto")
    store.create(RuntimePolicy(offline_mode=True).snapshot())
    runner = WorkflowRunner(store, Path(__file__).parents[1] / "configs/model_config.yaml", offline_mode=True)

    result = runner.run(_approved_snapshot(), RunnerOptions(), only_state="initial_candidate_generation")
    assert result["phase"] == "candidate_generation_completed"
    assert result["domain_state"] == "five_render"
    assert len(result["candidates"]) == 5
    assert result["skill_invocation_approval"]["actor"] == "system:auto"
