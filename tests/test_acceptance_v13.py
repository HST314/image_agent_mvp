import base64
import threading
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
def test_provider_boundary_never_receives_artifact_uri(tmp_path: Path, consumer: str) -> None:
    store, asset = _asset_store(tmp_path)
    provider_value = ProviderImageAdapter(store).resolve(asset["uri"])
    assert provider_value.startswith("data:image/png;base64,")
    assert "artifact://" not in provider_value, consumer


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
