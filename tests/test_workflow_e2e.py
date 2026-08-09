import json
import threading
import time
from pathlib import Path

from agent_core.batch import CandidateBatchGenerator
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from calibrator.calibration_loop import ManualAction
from render_clients.payload_mapper import build_render_payload
from storage.project_store import ProjectStore
from workspace_cli import main
import pytest
from agent_core.workflow import SelfCheckPolicy, InvalidTransitionError
from calibrator.calibration_loop import CalibrationLoop
from storage.assets import normalize_image_asset
from storage.project_store import ProjectLockError

def _try_project_lock(root: str, queue) -> None:
    try:
        with ProjectStore(root, "locked").lock():
            queue.put("acquired")
    except ProjectLockError:
        queue.put("blocked")


def task_payload():
    return {"task_id":"t", "project_id":"p", "source_refs":[{"ref_id":"s","ref_type":"text"}],
            "deliverable_goal":"海报", "usage_context":"手机", "known_facts":{"主体":"产品"}, "unknowns":{}}


def test_cli_new_resume_and_registry(tmp_path: Path, capsys):
    task = tmp_path / "task.json"; task.write_text(json.dumps(task_payload()), encoding="utf-8")
    root = tmp_path / "projects"
    assert main(["--projects-root", str(root), "new", "p", "--task", str(task), "--offline"]) == 0
    first_stdout = capsys.readouterr().out
    assert "请选择一张作为当前主图" in first_stdout and "候选方向 5" in first_stdout
    assert "candidate_index" not in first_stdout and "sha256" not in first_stdout
    store = ProjectStore(root, "p")
    assert store.manifest()["current_checkpoint"]["state"] == "master_candidate_selection"
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    assert set(runner.handlers) == set(runner.ORDER)
    assert main(["--projects-root", str(root), "resume", "p", "--offline", "--selected-id", "candidate-1"]) == 0
    resumed_stdout = capsys.readouterr().out
    assert "第 1 轮画面质检" in resumed_stdout and "修改建议" in resumed_stdout
    assert any(e["type"] == "inspection_completed" for e in store.history())


def test_retry_calls_real_failed_handler_on_new_branch(tmp_path: Path):
    store = ProjectStore(tmp_path, "p"); store.create(); store.checkpoint("confirmation_build", {"state":"confirmation_build"})
    store.fail_step("initial_candidate_generation", {"message":"provider"})
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    called = []
    runner.handlers["initial_candidate_generation"] = lambda data, options: called.append(data["state"]) or {"candidates":[]}
    store.retry(lambda state, snapshot: runner.run(snapshot, RunnerOptions(), only_state=state))
    assert called == ["confirmation_build"]
    assert store.manifest()["current_branch"].startswith("retry-")


def test_manual_resume_reuses_inspection(tmp_path: Path):
    store = ProjectStore(tmp_path, "p"); store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    data = {"state":"master_candidate_selection", "master_asset":{"uri":"u","sha256":"h"},
            "task_specification":{"task_id":"t","version":1,"facts":[],"parent_hash":None,"content_hash":"s"},
            "self_check_policy":{"termination":"fix","release":"manual","fixed_rounds":1}}
    first = runner.run(data, RunnerOptions(), only_state="self_check_iteration")
    before = len([e for e in store.history() if e["type"] == "inspection_completed"])
    runner.run(first, RunnerOptions(manual_action=ManualAction(action="skip")), only_state="self_check_iteration")
    after = len([e for e in store.history() if e["type"] == "inspection_completed"])
    assert before == after == 1


def test_i2i_payload_exact_extra_body_image():
    payload = build_render_payload("seedream", "改图", "2K", {}, watermark=True, reference_images=["previous-url"])
    assert payload["extra_body"] == {"image":"previous-url", "watermark":True}


def test_batch_is_concurrent_and_keeps_partial_success(tmp_path: Path):
    store = ProjectStore(tmp_path, "p"); store.create(); active = 0; peak = 0; guard = threading.Lock()
    def render(index):
        nonlocal active, peak
        with guard: active += 1; peak = max(peak, active)
        time.sleep(.03)
        with guard: active -= 1
        if index == 3: raise RuntimeError("bad")
        return {"uri":str(index), "sha256":str(index), "candidate_index":index}
    result = CandidateBatchGenerator(store, render, attempts=1, max_workers=3).generate("input")
    assert peak >= 2 and len(result["succeeded"]) == 4 and [x["index"] for x in result["failed"]] == [3]

@pytest.mark.parametrize("termination,release", [(a,b) for a in ("fix","solo") for b in ("auto","manual")])
def test_two_round_i2i_all_policies(tmp_path: Path, termination: str, release: str):
    store = ProjectStore(tmp_path, f"two-{termination}-{release}"); store.create()
    payloads = []
    def rework(assembled):
        refs = [r["uri"] for r in assembled["references"]]
        payloads.append(build_render_payload("seedream", assembled["text"], "2K", {}, reference_images=refs))
        return normalize_image_asset({"uri":f"https://images.example/{len(payloads)}.png", "provider":"ark", "model":"seedream"})
    policy = SelfCheckPolicy(termination, release, fixed_rounds=2, max_rounds=2)
    approve = (lambda _: ManualAction(action="execute")) if release == "manual" else None
    result = CalibrationLoop(store, policy, inspector=lambda *_:{"passed":False,"decision":"continue","rework_prompt_delta":"只修改主体清晰度","confidence":.8}, reworker=rework).run(
        current_asset=normalize_image_asset({"uri":"https://images.example/base.png","provider":"ark","model":"seedream"}),
        stable_specification="稳定任务事实", constraints=[], approve=approve)
    assert len(payloads) == 1
    assert result["waiting"] and not result["termination_satisfied"]
    assert result["asset"]["sha256"] == result["latest_checked_asset_hash"]
    completed = [e for e in store.history() if e["type"] == "rework_completed"]
    assert len(completed) == 1 and all(e.get("idempotency_key") for e in completed)

@pytest.mark.parametrize("termination,release", [(a,b) for a in ("fix","solo") for b in ("auto","manual")])
def test_completed_asset_is_latest_checked_in_all_policy_combinations(tmp_path: Path, termination: str, release: str):
    store = ProjectStore(tmp_path, f"pass-{termination}-{release}"); store.create()
    asset = normalize_image_asset({"uri":"https://images.example/approved.png", "provider":"ark", "model":"seedream"})
    approve = (lambda _: ManualAction(action="execute")) if release == "manual" else None
    result = CalibrationLoop(store, SelfCheckPolicy(termination, release, fixed_rounds=1, max_rounds=1),
        inspector=lambda *_:{"passed":True,"decision":"pass","rework_prompt_delta":"","confidence":.99},
        reworker=lambda _: pytest.fail("passing/final inspection must not create an unchecked asset")).run(
        current_asset=asset, stable_specification="s", constraints=[], approve=approve)
    assert result["termination_satisfied"] and not result["waiting"]
    assert result["asset"]["sha256"] == result["latest_checked_asset_hash"]

def test_self_check_to_final_approval_accepts_only_audited_checked_asset(tmp_path: Path):
    store = ProjectStore(tmp_path, "final-e2e"); store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    asset = normalize_image_asset({"uri":"https://images.example/final.png", "provider":"ark", "model":"seedream"})
    base = {"state":"master_candidate_selection", "master_asset":asset,
            "task_specification":{"task_id":"t","version":1,"facts":[],"parent_hash":None,"content_hash":"s"},
            "self_check_policy":{"termination":"solo","release":"auto","max_rounds":2}}
    runner._inspect = lambda *_:{"passed":True,"decision":"pass","rework_prompt_delta":"","confidence":.99}
    checked = runner.run(base, RunnerOptions(), only_state="self_check_iteration")
    assert checked["asset"]["sha256"] == checked["latest_checked_asset_hash"]
    human_done = runner.run(checked, RunnerOptions(), only_state="human_prompt_iteration")
    delivered = runner.run(human_done, RunnerOptions(final_approved=True), only_state="final_approval")
    assert delivered["completed"] and delivered["final_asset"]["sha256"] == asset["sha256"]
    for mutation in (
        {"asset": {**asset, "sha256":"unchecked"}},
        {"latest_checked_asset_hash":"old-check"},
        {"calibration_status":"waiting_human_decision", "termination_satisfied":False,
         "termination_reason":"solo_round_limit"},
    ):
        invalid = {**human_done, **mutation, "state":"human_prompt_iteration"}
        with pytest.raises(ValueError):
            runner.run(invalid, RunnerOptions(final_approved=True), only_state="final_approval")

def test_runner_streams_round_to_user_and_checkpoint_clarification(tmp_path: Path):
    store = ProjectStore(tmp_path, "stream"); store.create(); output = []
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True, output=output.append)
    data = {"master_asset":normalize_image_asset({"uri":"mock://base","mock":True,"provider":"offline","model":"fake"}),
            "task_specification":{"task_id":"t","version":1,"facts":[],"parent_hash":None,"content_hash":"s"},
            "self_check_policy":{"termination":"fix","release":"auto","fixed_rounds":2}}
    runner.run(data, RunnerOptions(), only_state="self_check_iteration")
    text = "\n".join(output)
    assert "第 1 轮画面质检" in text and "第 2 轮画面质检" in text and "修改建议" in text

def test_clarification_budget_and_fingerprints_survive_resume(tmp_path: Path):
    store = ProjectStore(tmp_path, "clarify"); store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    unknowns = {
        "output_spec":{"impact":"影响构图","blocking":True,"has_safe_default":False},
        "asset_rules":{"impact":"影响素材合规","blocking":True,"has_safe_default":False},
        "content_boundaries":{"impact":"影响内容安全","blocking":True,"has_safe_default":False},
    }
    first = runner.run({"task_card":{**task_payload(), "unknowns":unknowns}}, RunnerOptions(), only_state="intake_clarify")
    restored = store.resume()
    assert restored["clarification_asked_count"] == 3 and restored["clarification_remaining_budget"] == 7
    assert len(restored["previous_fingerprints"]) == 3
    second = runner.run(restored, RunnerOptions(clarification_answers={"output_spec":"9:16"}), only_state="intake_clarify")
    assert second["clarification_asked_count"] == 3 and second["clarification_remaining_budget"] == 7

def test_blocked_and_manual_end_cannot_be_delivered(tmp_path: Path):
    store = ProjectStore(tmp_path, "blocked"); store.create()
    loop = CalibrationLoop(store, SelfCheckPolicy("solo", "auto"),
        inspector=lambda *_:{"passed":False,"decision":"blocked","rework_prompt_delta":"","confidence":.8}, reworker=lambda x:x)
    result = loop.run(current_asset={"uri":"https://x/a","sha256":"a"}, stable_specification="s", constraints=[])
    assert result["waiting"] and not result["termination_satisfied"]
    ended = loop.run(current_asset={"uri":"https://x/a","sha256":"a"}, stable_specification="s", constraints=[],
                     approve=lambda _: ManualAction(action="end"))
    assert ended["calibration_status"] == "terminated_without_delivery" and not ended["termination_satisfied"]
    assert any(e["type"] == "calibration_terminated_without_delivery" for e in store.history())

def test_human_rework_invalidates_old_inspection_and_forces_recheck(tmp_path: Path):
    store = ProjectStore(tmp_path, "rework"); store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    old = normalize_image_asset({"uri":"https://x/old","provider":"ark","model":"m"})
    data = {"state":"self_check_iteration", "asset":old, "current_asset":old,
            "calibration_status":"completed", "termination_satisfied":True, "termination_reason":"pass",
            "latest_checked_asset_hash":old["sha256"], "selected_policy":{"termination":"solo","release":"auto"}}
    changed = runner.run(data, RunnerOptions(human_prompt="改颜色"), only_state="human_prompt_iteration")
    assert changed["waiting"] and changed["phase"] == "waiting_reinspection"
    assert not changed["termination_satisfied"] and changed["latest_checked_asset_hash"] is None

def test_runner_rejects_illegal_transition(tmp_path: Path):
    store = ProjectStore(tmp_path, "illegal"); store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    with pytest.raises(InvalidTransitionError):
        runner.run({"state":"intake_clarify"}, RunnerOptions(), only_state="final_approval")

def test_two_processes_only_one_gets_project_lock(tmp_path: Path):
    import multiprocessing as mp
    store = ProjectStore(tmp_path, "locked"); store.create()
    queue = mp.Queue()
    with store.lock():
        process = mp.Process(target=_try_project_lock, args=(str(tmp_path), queue))
        process.start(); process.join(5)
        assert process.exitcode == 0 and queue.get(timeout=1) == "blocked"
