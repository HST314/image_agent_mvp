import base64
import threading
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from agent_core.jobs import JobNotFoundError, JobRegistry
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from storage.project_store import ProjectStore
from storage.provider_assets import ArtifactCorruptError, ArtifactNotFoundError, ProviderImageAdapter


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _asset_store(tmp_path: Path) -> tuple[ProjectStore, dict]:
    store = ProjectStore(tmp_path, "project"); store.create()
    return store, store.artifacts.save_bytes(PNG)


@pytest.mark.parametrize("consumer", ["self_check_inspection", "self_check_rework", "human_prompt_rework"])
def test_provider_boundary_never_receives_artifact_uri(tmp_path: Path, consumer: str, monkeypatch) -> None:
    store, asset = _asset_store(tmp_path)
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=False)
    captured = []
    def direct_call(state, role, invoke, **kwargs):
        binding = runner.gateway.router.binding_for_state(state)
        return invoke(SimpleNamespace(binding=binding))
    monkeypatch.setattr(runner.gateway, "call", direct_call)
    if consumer == "self_check_inspection":
        class Vlm:
            def inspect(self, image, prompt):
                captured.append(image)
                return {"passed": True, "decision": "pass", "confidence": .9}
        monkeypatch.setattr(runner, "_vlm", lambda route: Vlm())
        runner._inspect(asset["uri"], "check")
    else:
        class ImageClient:
            def __init__(self, **kwargs): pass
            def render(self, payload):
                image = payload["extra_body"]["image"]
                captured.extend(image if isinstance(image, list) else [image])
                return {"content": base64.b64encode(PNG).decode(), "provider": "test"}
        monkeypatch.setattr("agent_core.workflow_runner.ArkImageRenderClient", ImageClient)
        runner._image_call(consumer, "rework", [asset["uri"]])
    assert captured and all(value.startswith("data:image/png;base64,") for value in captured)
    assert all("artifact://" not in value for value in captured)


def test_provider_boundary_reports_missing_and_corrupt_artifacts(tmp_path: Path) -> None:
    store, asset = _asset_store(tmp_path)
    adapter = ProviderImageAdapter(store)
    with pytest.raises(ArtifactNotFoundError):
        adapter.resolve("artifact://artifact_000000000000000000000000")
    path, _ = store.artifacts.resolve(asset["artifact_id"])
    path.write_bytes(PNG + b"tampered")
    with pytest.raises(ArtifactCorruptError):
        adapter.resolve(asset["uri"])


def test_master_selection_is_an_independent_persisted_fact(tmp_path: Path) -> None:
    store, asset = _asset_store(tmp_path)
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    candidates = [{**asset, "id": f"candidate-{index}"} for index in range(1, 6)]
    result = runner.run({"state":"master_candidate_selection", "candidates":candidates},
                        RunnerOptions(selected_id="candidate-4", actor="tester"),
                        only_state="master_candidate_selection")
    assert result["selected_master"] == {"candidate_id":"candidate-4", "artifact_id":asset["artifact_id"], "actor":"tester"}
    assert any(event["type"] == "master_selected" for event in store.history())


def test_job_lifecycle_callback_is_persistable_and_survives_registry_reload(tmp_path: Path) -> None:
    seen = []; done = threading.Event()
    def observe(record):
        seen.append(record["status"])
        if record["status"] == "succeeded": done.set()
    registry = JobRegistry(tmp_path / "jobs")
    job, _ = registry.submit("project", "selection-job", "advance", lambda: {"ok": True}, on_event=observe)
    assert done.wait(2)
    assert seen == ["queued", "running", "succeeded"]
    assert JobRegistry(tmp_path / "jobs").get(job["job_id"])["status"] == "succeeded"
    with pytest.raises(JobNotFoundError):
        registry.get("job_00000000000000000000000000000000")


@pytest.mark.parametrize("status", ["queued", "running"])
def test_inflight_reload_persists_job_and_project_interruption(tmp_path: Path, status: str) -> None:
    jobs = tmp_path / "jobs"; jobs.mkdir()
    job = {"job_id":"job_dead", "project_id":"project", "operation":"advance",
           "status":status, "events":[{"seq":1,"type":status,"timestamp":"earlier"}]}
    (jobs / "job_dead.json").write_text(__import__("json").dumps(job), encoding="utf-8")
    store = ProjectStore(tmp_path / "projects", "project"); store.create()
    def recover(record):
        store.events.append("job_status_changed", job_id=record["job_id"], operation=record["operation"],
                            status=record["status"], error=record["error"])
    restored = JobRegistry(jobs, on_recover=recover).get("job_dead")
    assert restored["status"] == "interrupted"
    assert restored["error"]["code"] == "PROCESS_RESTARTED"
    event = [item for item in store.history() if item["type"] == "job_status_changed"][-1]
    assert event["status"] == "interrupted" and event["error"]["code"] == "PROCESS_RESTARTED"


@pytest.mark.parametrize("error_type, code", [(ArtifactNotFoundError, "ARTIFACT_NOT_FOUND"),
                                                (ArtifactCorruptError, "ARTIFACT_CORRUPT")])
def test_async_job_preserves_domain_error_code(tmp_path: Path, error_type, code) -> None:
    registry = JobRegistry(tmp_path / "jobs")
    def fail(): raise error_type("broken")
    job, _ = registry.submit("project", "failure-key", "advance", fail)
    for _ in range(200):
        record = registry.get(job["job_id"])
        if record["status"] == "failed": break
        time.sleep(.01)
    assert record["error"]["code"] == code


def test_async_job_preserves_normalized_error_category(tmp_path: Path) -> None:
    """异常携带的规范化 category（如 ModelCallError 的 timeout_unknown）随 job
    记录透出，前端依 *_unknown 约定判定结果未知并保留幂等键；无 category 的
    异常不新增该字段（向后兼容既有 {code, message} 契约）。"""
    registry = JobRegistry(tmp_path / "jobs")

    class ClassifiedError(RuntimeError):
        category = "timeout_unknown"

    def fail_classified(): raise ClassifiedError("x")
    def fail_plain(): raise ValueError("TASK_APPROVAL_REQUIRED")

    job_unknown, _ = registry.submit("project", "failure-key-unknown", "advance", fail_classified)
    job_known, _ = registry.submit("project2", "failure-key-known", "advance", fail_plain)
    for job_id in (job_unknown["job_id"], job_known["job_id"]):
        for _ in range(200):
            if registry.get(job_id)["status"] == "failed": break
            time.sleep(.01)
    unknown_error = registry.get(job_unknown["job_id"])["error"]
    assert unknown_error["code"] == "ClassifiedError" and unknown_error["category"] == "timeout_unknown"
    known_error = registry.get(job_known["job_id"])["error"]
    assert known_error["code"] == "ValueError" and "category" not in known_error


def test_queued_job_exceeding_sla_becomes_retry_safe_without_execution(tmp_path: Path) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    second_executed = threading.Event()
    registry = JobRegistry(
        tmp_path / "jobs",
        workers=1,
        queue_sla_seconds=0.05,
        heartbeat_interval_seconds=0.01,
    )

    def occupy_worker():
        first_started.set()
        assert release_first.wait(2)
        return {"ok": True}

    first, _ = registry.submit("project-1", "first-job", "first", occupy_worker)
    assert first_started.wait(1)
    second, _ = registry.submit(
        "project-2", "second-job", "second", lambda: second_executed.set()
    )
    assert second["submitted_at"] == second["created_at"]

    deadline = time.monotonic() + 1
    record = registry.get(second["job_id"])
    while record["status"] == "queued" and time.monotonic() < deadline:
        time.sleep(0.01)
        record = registry.get(second["job_id"])

    assert record["status"] == "stalled"
    assert record["error"] == {
        "code": "QUEUE_START_TIMEOUT",
        "message": "任务排队超时且尚未开始，可安全重试。",
        "retry_safe": True,
    }
    assert "started_at" not in record
    assert record["events"][-1]["type"] == "stalled"

    release_first.set()
    deadline = time.monotonic() + 1
    while registry.get(first["job_id"])["status"] != "succeeded" and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.02)
    assert second_executed.is_set() is False
    assert registry.get(second["job_id"])["status"] == "stalled"


def test_worker_terminalizes_expired_queue_item_without_an_observer(tmp_path: Path) -> None:
    """The execute boundary enforces the SLA even when nobody polls the job."""
    first_started = threading.Event()
    release_first = threading.Event()
    second_executed = threading.Event()
    second_terminalized = threading.Event()
    registry = JobRegistry(
        tmp_path / "jobs",
        workers=1,
        queue_sla_seconds=0.05,
        heartbeat_interval_seconds=0.01,
    )

    def occupy_worker():
        first_started.set()
        assert release_first.wait(2)

    registry.submit("project-1", "first-job", "first", occupy_worker)
    assert first_started.wait(1)
    second, _ = registry.submit(
        "project-2",
        "second-job",
        "second",
        lambda: second_executed.set(),
        on_event=lambda record: second_terminalized.set() if record["status"] == "stalled" else None,
    )

    # Deliberately do not call get(), metrics(), submit(), or
    # active_for_project() while the second job exceeds its queue SLA.
    time.sleep(0.09)
    release_first.set()

    assert second_terminalized.wait(1)
    assert second_executed.is_set() is False
    record = registry.get(second["job_id"])
    assert record["status"] == "stalled"
    assert "started_at" not in record
    assert [event["type"] for event in record["events"]] == ["queued", "stalled"]


def test_running_job_heartbeat_and_executor_metrics_are_observable(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    registry = JobRegistry(
        tmp_path / "jobs",
        workers=1,
        queue_sla_seconds=1,
        heartbeat_interval_seconds=0.01,
    )

    def execute():
        started.set()
        assert release.wait(2)

    job, _ = registry.submit("project", "heartbeat-job", "heartbeat", execute)
    assert started.wait(1)
    first = registry.get(job["job_id"])
    assert first["submitted_at"] and first["started_at"] and first["heartbeat_at"]
    time.sleep(0.03)
    current = registry.get(job["job_id"])
    assert current["heartbeat_at"] >= first["heartbeat_at"]
    metrics = registry.metrics()
    assert metrics["max_workers"] == 1
    assert metrics["active_workers"] == 1
    assert metrics["queue_depth"] == 0
    assert metrics["oldest_queued_seconds"] is None
    assert metrics["oldest_heartbeat_seconds"] is not None
    assert metrics["oldest_heartbeat_seconds"] < 0.2
    release.set()
