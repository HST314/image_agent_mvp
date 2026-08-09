"""Production-path evidence for the v7 remediation."""
from pathlib import Path

import pytest

from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from storage.project_store import ProjectStore


def task_payload():
    return {"task_id":"v7", "project_id":"clean", "source_refs":[{"ref_id":"brief","ref_type":"text"}],
            "deliverable_goal":"海报", "usage_context":"手机", "known_facts":{"主体":"产品"}, "unknowns":{}}


def test_clean_external_workspace_real_runner_to_frozen_delivery(tmp_path: Path, monkeypatch):
    clean = tmp_path / "clean-install"; clean.mkdir(); monkeypatch.chdir(clean)
    store = ProjectStore(tmp_path / "projects", "p"); store.create()
    runner = WorkflowRunner(store, Path(__file__).parents[1] / "configs/model_config.yaml", offline_mode=True)
    state = runner.run({"task_card": task_payload()}, RunnerOptions(), only_state="intake_clarify")
    state = runner.run(state, RunnerOptions(task_approved=True, actor="owner"), only_state="confirmation_build")
    state = runner.run(state, RunnerOptions(), only_state="initial_candidate_generation")
    assert len(state["candidates"]) == 5 and len({x["style_id"] for x in state["candidates"]}) == 5
    state = runner.run(state, RunnerOptions(selected_id=state["candidates"][0]["id"]), only_state="master_candidate_selection")
    # The provider is stubbed, but inspection, checkpoints and delivery gates are
    # the production handlers. Mark the persisted fixture as provider-accepted.
    state["master_asset"]["mock"] = False
    runner._inspect = lambda *_: {"passed":True, "decision":"pass", "deviations":[],
                                  "rework_prompt_delta":"", "confidence":.99}
    state = runner.run(state, RunnerOptions(), only_state="self_check_iteration")
    state = runner.run(state, RunnerOptions(), only_state="human_prompt_iteration")
    asset = state["asset"]
    state["quality_version"] = "visual-check-v2"
    delivered = runner.run(state, RunnerOptions(final_approved=True, actor="owner"), only_state="final_approval")
    assert delivered["domain_state"] == "delivery_frozen"
    assert delivered["frozen_delivery"]["asset_sha256"] == asset["sha256"]
    assert delivered["frozen_delivery"]["revision_hash"] == delivered["task_revision"]["revision_hash"]
    assert store.resume()["frozen_delivery"] == delivered["frozen_delivery"]
    assert any(e["type"] == "delivery_frozen" for e in store.history())


def test_public_initial_render_gateway_rejects_reference_leaks(tmp_path: Path):
    store = ProjectStore(tmp_path, "p"); store.create()
    runner = WorkflowRunner(store, Path(__file__).parents[1] / "configs/model_config.yaml", offline_mode=True)
    with pytest.raises(ValueError, match="STYLE_REFERENCE_LEAK"):
        runner._image_call("initial_candidate_generation", "safe", ["file:///style.png"])


def test_production_error_event_uses_non_retryable_matrix(tmp_path: Path):
    store = ProjectStore(tmp_path, "p"); store.create()
    runner = WorkflowRunner(store, Path(__file__).parents[1] / "configs/model_config.yaml", offline_mode=True)
    with pytest.raises(Exception):
        runner.run({"task_card":task_payload(), "task_specification":{"bad":True}}, RunnerOptions(), only_state="confirmation_build")
    error = store.manifest()["failed_step"]["error"]
    assert error["category"] in {"invalid_input", "structured_output"}
    assert error["retryable"] is False
