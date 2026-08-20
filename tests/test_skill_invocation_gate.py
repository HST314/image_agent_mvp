"""Independent skill-invocation approval gate and auditable regeneration."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main_front
from agent_core.batch import CandidateBatchError
from agent_core.models import ImageTaskCard
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from configs.runtime_policy import RuntimePolicy, SkillInvocationPolicyConfig
from interaction.confirmation_builder import specification_from_task
from model_router.executor import ModelCallError
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
    original_image_call = runner._image_call
    observed_boundaries = []

    def render_after_persisted_boundary(state, prompt, references, *, index=None, size=None):
        persisted = store.resume()
        assert persisted is not None
        observed_boundaries.append((persisted["phase"], persisted["render_size"], size))
        return original_image_call(state, prompt, references, index=index, size=size)

    monkeypatch.setattr(runner, "_image_call", render_after_persisted_boundary)

    result = runner.run(_approved_snapshot(), RunnerOptions(), only_state="initial_candidate_generation")
    assert result["phase"] == "candidate_generation_completed"
    assert result["domain_state"] == "five_render"
    assert len(result["candidates"]) == 5
    assert result["skill_invocation_approval"]["actor"] == "system:auto"
    assert observed_boundaries == [
        ("skill_approved_pending_render", "2560x1440", "2560x1440")
    ] * 5


def test_branch_retry_uses_only_candidates_from_the_approved_skill_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v2 branch must not hit v1 candidate cache entries for the same task."""
    monkeypatch.chdir(tmp_path)
    policy = RuntimePolicy(
        offline_mode=True,
        skill_invocation=SkillInvocationPolicyConfig(release="manual"),
    )
    store = ProjectStore(tmp_path / "projects", "skill-version-branch")
    store.create(policy.snapshot())
    runner = WorkflowRunner(store, Path(__file__).parents[1] / "configs/model_config.yaml", offline_mode=True)

    waiting_v1 = runner.run(_approved_snapshot(), RunnerOptions(), only_state="initial_candidate_generation")
    v1_gate_checkpoint = store.manifest()["current_checkpoint"]["checkpoint_id"]
    rendered_v1 = runner.run(
        waiting_v1,
        RunnerOptions(skill_action="approve", actor="reviewer-v1"),
        only_state="initial_candidate_generation",
    )

    store.branch_from(v1_gate_checkpoint, name="skill-v2-branch")
    branched_v1 = store.resume()
    assert branched_v1 is not None
    waiting_v2 = runner.run(
        branched_v1,
        RunnerOptions(skill_action="retry", actor="reviewer-v2"),
        only_state="initial_candidate_generation",
    )
    rendered_v2 = runner.run(
        waiting_v2,
        RunnerOptions(skill_action="approve", actor="reviewer-v2"),
        only_state="initial_candidate_generation",
    )

    v1_styles = {item["style_id"] for item in rendered_v1["candidates"]}
    v2_plan_styles = {item["style_id"] for item in waiting_v2["render_plans"]}
    v2_candidate_styles = {item["style_id"] for item in rendered_v2["candidates"]}
    assert v1_styles.isdisjoint(v2_plan_styles)
    assert v2_candidate_styles == v2_plan_styles
    assert {item["prompt_version_id"] for item in rendered_v2["candidates"]} == {
        item["prompt_version_id"] for item in waiting_v2["render_plans"]
    }
    scopes = [event.get("cache_scope") for event in store.history()
              if event.get("type") == "candidate_succeeded"]
    assert {scope["skill_version_id"] for scope in scopes if scope} == {
        "skill-invocation-v1", "skill-invocation-v2",
    }


def test_retryable_five_render_failure_recovers_via_api_without_reinvoking_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approved skill provenance survives a transient render failure and /retry."""
    monkeypatch.chdir(tmp_path)
    projects = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", projects)
    policy = RuntimePolicy(
        offline_mode=True,
        skill_invocation=SkillInvocationPolicyConfig(release="manual"),
    )
    store = ProjectStore(projects, "skill-render-recovery")
    store.create(policy.snapshot())
    runner = WorkflowRunner(store, Path(__file__).parents[1] / "configs/model_config.yaml", offline_mode=True)
    waiting = runner.run(_approved_snapshot(), RunnerOptions(), only_state="initial_candidate_generation")

    original_image_call = runner._image_call

    def fail_one(state: str, prompt: str, references: list[str], index: int = 0, size: str | None = None):
        if index == 2:
            raise ModelCallError(
                "provider timed out after submission", True, "timeout_unknown", "req-timeout", "trace-timeout",
            )
        return original_image_call(state, prompt, references, index=index, size=size)

    monkeypatch.setattr(runner, "_image_call", fail_one)
    with pytest.raises(CandidateBatchError):
        runner.run(
            waiting,
            RunnerOptions(skill_action="approve", actor="recovery-reviewer"),
            only_state="initial_candidate_generation",
        )

    approved_boundary = store.resume()
    assert approved_boundary is not None
    assert approved_boundary["phase"] == "skill_approved_pending_render"
    assert approved_boundary["skill_invocation_approval"] == {
        "version_id": "skill-invocation-v1", "actor": "recovery-reviewer",
    }
    failure = store.manifest()["failed_step"]["error"]
    assert failure["category"] == "timeout_unknown"
    assert failure["retryable"] is True
    assert failure["recovery_actions"] == ["retry_after_confirmation", "abandon"]
    failed_view = main_front._project_view(store)
    assert "retry" in failed_view["capabilities"]

    response = TestClient(main_front.app, raise_server_exceptions=False).post(
        "/api/projects/skill-render-recovery/retry", json={},
    )
    assert response.status_code == 200, response.text
    recovered = response.json()
    assert recovered["manifest"]["failed_step"] is None
    assert recovered["snapshot"]["phase"] == "candidate_generation_completed"
    assert recovered["snapshot"]["skill_invocation_approval"] == {
        "version_id": "skill-invocation-v1", "actor": "recovery-reviewer",
    }
    assert len(recovered["snapshot"]["skill_invocation_history"]) == 1
    assert len(recovered["snapshot"]["candidates"]) == 5
