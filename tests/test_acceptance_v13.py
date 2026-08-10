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
