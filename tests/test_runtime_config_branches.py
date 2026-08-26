"""Configuration revision branch, Runner, and HTTP boundary regressions."""

from __future__ import annotations

import json
import time
from pathlib import Path

import configs.runtime_settings_api as runtime_settings_api
import main_front
import pytest
from agent_core.jobs import JobRegistry
from agent_core.workflow_runner import WorkflowRunner
from configs.runtime_revision import publish_revision
from fastapi.testclient import TestClient
from storage.project_store import ProjectStore, atomic_json


def _bundle(
    project_id: str,
    revision_id: str,
    *,
    policy,
    model_document: dict,
    parent: str | None,
    branch_id: str | None = None,
    checkpoint_id: str | None = None,
    effective_from_state: str = "initial",
):
    return main_front._build_runtime_revision(
        project_id=project_id,
        revision_id=revision_id,
        parent_revision_id=parent,
        task_config_revision_id="task-config-r000001",
        overrides={} if parent is None else {"candidate_concurrency": policy.candidate_concurrency},
        policy=policy,
        model_document=model_document,
        actor_type="system",
        actor_id="runtime_test",
        apply_mode="before_start" if parent is None else "safe_checkpoint_branch",
        branch_id=branch_id,
        checkpoint_id=checkpoint_id,
        effective_from_state=effective_from_state,
    )


def _waiting_checkpoint(store: ProjectStore) -> str:
    with store.lock():
        return store.checkpoint(
            "intake_clarify",
            {
                "state": "intake_clarify",
                "phase": "waiting_clarification",
                "waiting": True,
                "task_card": {
                    "task_id": f"task-{store.project_id}",
                    "project_id": store.project_id,
                    "source_refs": [{"ref_id": "brief", "ref_type": "brief"}],
                    "deliverable_goal": "新品海报",
                    "usage_context": "内部审核",
                    "category_ref": {"category_id": "generic", "version": "1"},
                    "known_facts": {"audience": "审核人员"},
                    "unknowns": {"output_spec": "待确认"},
                    "asset_inputs": [],
                    "status": "draft",
                },
            },
        )


def _wait_job(client: TestClient, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = client.get(f"/api/jobs/{job_id}").json()
        if record["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return record
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in time")


def test_rerun_branch_jobs_keep_branch_effective_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    config_root = tmp_path / "instance-runtime-config"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", projects)
    monkeypatch.setattr(main_front, "CONFIG_ROOT", config_root)
    monkeypatch.setattr(main_front, "MANAGED_MODE", True)
    monkeypatch.setattr(main_front, "MANAGED_PROJECT_ID", "rerun-binding-project")
    monkeypatch.setattr(main_front, "JOBS", JobRegistry(tmp_path / "jobs"))
    base = main_front._base_runtime()
    initial = _bundle(
        "rerun-binding-project",
        "cfg-inst-r000001",
        policy=base.policy,
        model_document=base.model_document,
        parent=None,
    )
    publish_revision(config_root, initial[0], initial[1], initial[2])
    atomic_json(config_root / "state.json", {"current_revision_id": "cfg-inst-r000001"})
    store = ProjectStore(projects, "rerun-binding-project")
    store.create(base.policy.snapshot(), config_binding=initial[3])
    source = _waiting_checkpoint(store)
    client = TestClient(main_front.app, raise_server_exceptions=False)
    real_clarify = WorkflowRunner._clarify

    def clarify_without_provider(
        runner: WorkflowRunner, data: dict, options: dict
    ) -> dict:
        runner.offline_mode = True
        try:
            return real_clarify(runner, data, options)
        finally:
            runner.offline_mode = False

    monkeypatch.setattr(WorkflowRunner, "_clarify", clarify_without_provider)

    created = client.post(
        "/api/projects/rerun-binding-project/branches",
        json={
            "checkpoint": source,
            "name": "rerun-clarification",
            "mode": "rerun_stage",
        },
    )

    assert created.status_code == 200, created.text
    assert initial[0]["effective_from_state"] == "initial"
    binding = store.active_config_binding()
    assert binding["runtime_config_revision_id"] == "cfg-inst-r000001"
    assert binding["effective_from_state"] == "intake_clarify"

    first = client.post(
        "/api/projects/rerun-binding-project/jobs",
        json={"idempotency_key": "rerun-auto-start"},
    )
    assert first.status_code == 202, first.text
    assert _wait_job(client, first.json()["job_id"])["status"] == "succeeded"
    snapshot = store.resume()
    assert snapshot is not None
    assert snapshot["phase"] == "waiting_clarification"
    assert snapshot["question_card"]["questions"]

    second = client.post(
        "/api/projects/rerun-binding-project/jobs",
        json={"idempotency_key": "rerun-user-click"},
    )
    assert second.status_code == 202, second.text
    assert _wait_job(client, second.json()["job_id"])["status"] == "succeeded"
    assert store.active_config_binding()["effective_from_state"] == "intake_clarify"


def test_standalone_settings_apply_before_first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", projects)
    monkeypatch.setattr(main_front, "CONFIG_ROOT", None)
    monkeypatch.setattr(main_front, "MANAGED_MODE", False)
    base = main_front._base_runtime()
    store = ProjectStore(projects, "unstarted-project")
    initial = _bundle(
        store.project_id,
        "cfg-inst-r000001",
        policy=base.policy,
        model_document=base.model_document,
        parent=None,
    )
    store.create(base.policy.snapshot(), config_binding=initial[3])
    publish_revision(store.root / "runtime-config", initial[0], initial[1], initial[2])
    runtime_file_before = Path(main_front.RUNTIME_POLICY_PATH).read_bytes()
    model_file_before = Path(main_front.MODEL_CONFIG).read_bytes()
    client = TestClient(main_front.app, raise_server_exceptions=False)
    request = {
        "base_revision_id": "cfg-inst-r000001",
        "overrides": {
            "category_constraint": {"release": "manual"},
            "style_direction": {"release": "off"},
            "candidate_concurrency": 2,
            "watermark": True,
        },
        "actor": "designer-before-start",
        "confirmed": True,
        "idempotency_key": "settings-before-start-0001",
    }

    applied = client.post(
        f"/api/projects/{store.project_id}/runtime-settings", json=request
    )

    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["status"] == "APPLIED_BEFORE_START"
    assert body["branch_id"] == "main"
    assert body["checkpoint_id"] is None
    assert body["from_checkpoint"] is None
    assert body["runtime_config_revision_id"] == "cfg-inst-r000002"
    assert store.read_manifest()["current_checkpoint"] is None
    assert main_front._project_runtime(store).revision_id == "cfg-inst-r000002"
    assert main_front._project_runtime(store).policy.candidate_concurrency == 2
    assert main_front._project_runtime(store).policy.category_constraint.release == "manual"
    assert main_front._project_runtime(store).policy.style_direction.release == "off"
    assert Path(main_front.RUNTIME_POLICY_PATH).read_bytes() == runtime_file_before
    assert Path(main_front.MODEL_CONFIG).read_bytes() == model_file_before

    replay = client.post(
        f"/api/projects/{store.project_id}/runtime-settings", json=request
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "APPLIED_BEFORE_START"
    assert replay.json()["runtime_config_revision_id"] == "cfg-inst-r000002"
    assert len(
        [
            event
            for event in store.history()
            if event.get("type") == "config_revision_applied"
        ]
    ) == 1


def test_standalone_settings_create_revision_branch_and_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", projects)
    monkeypatch.setattr(main_front, "CONFIG_ROOT", None)
    monkeypatch.setattr(main_front, "MANAGED_MODE", False)
    base = main_front._base_runtime()
    store = ProjectStore(projects, "standalone-project")
    initial = _bundle(
        store.project_id,
        "cfg-inst-r000001",
        policy=base.policy,
        model_document=base.model_document,
        parent=None,
    )
    store.create(base.policy.snapshot(), config_binding=initial[3])
    publish_revision(store.root / "runtime-config", initial[0], initial[1], initial[2])
    source = _waiting_checkpoint(store)
    runtime_file_before = Path(main_front.RUNTIME_POLICY_PATH).read_bytes()
    model_file_before = Path(main_front.MODEL_CONFIG).read_bytes()
    client = TestClient(main_front.app, raise_server_exceptions=False)

    settings = client.get(f"/api/projects/{store.project_id}/runtime-settings")
    assert settings.status_code == 200, settings.text
    serialized = json.dumps(settings.json())
    assert all(
        private not in serialized
        for private in ("image_api_base_url", "style_library_root", "offline_mode")
    )

    request = {
        "base_revision_id": "cfg-inst-r000001",
        "overrides": {"candidate_concurrency": 3, "watermark": True},
        "actor": "designer-1",
        "confirmed": True,
        "idempotency_key": "settings-command-0001",
    }
    applied = client.post(
        f"/api/projects/{store.project_id}/runtime-settings", json=request
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["status"] == "APPLIED_ON_BRANCH"
    assert body["from_checkpoint"] == source
    assert body["runtime_config_revision_id"] == "cfg-inst-r000002"
    assert body["settings"]["values"]["candidate_concurrency"]["effective"] == 3
    assert main_front._project_runtime(store).revision_id == "cfg-inst-r000002"
    assert main_front._project_runtime(store).policy.watermark is True
    assert Path(main_front.RUNTIME_POLICY_PATH).read_bytes() == runtime_file_before
    assert Path(main_front.MODEL_CONFIG).read_bytes() == model_file_before

    runtime_status = client.get(f"/api/projects/{store.project_id}/runtime-status")
    assert runtime_status.status_code == 200, runtime_status.text
    status_body = runtime_status.json()
    assert status_body["process_health"] == "ok"
    assert status_body["active_job"] is None
    assert status_body["configuration"]["revision_id"] == "cfg-inst-r000002"
    assert status_body["configuration"]["branch_id"] == body["branch_id"]
    assert len(status_body["configuration"]["config_hash"]) == 64
    assert status_body["recent_exceptions"] == []

    replay = client.post(
        f"/api/projects/{store.project_id}/runtime-settings", json=request
    )
    assert replay.status_code == 200
    assert replay.json()["branch_id"] == body["branch_id"]
    with store.lock():
        advanced = store.checkpoint(
            "intake_clarify",
            {
                "state": "intake_clarify",
                "phase": "waiting_clarification",
                "waiting": True,
                "task_card": {"task_id": store.project_id},
            },
        )
    late_replay = client.post(
        f"/api/projects/{store.project_id}/runtime-settings", json=request
    )
    assert late_replay.status_code == 200
    assert late_replay.json()["checkpoint_id"] == body["checkpoint_id"]
    conflict = client.post(
        f"/api/projects/{store.project_id}/runtime-settings",
        json={**request, "overrides": {"candidate_concurrency": 2}},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    unsafe = client.post(
        f"/api/projects/{store.project_id}/runtime-settings",
        json={**request, "idempotency_key": "settings-command-0002", "overrides": {"image_api_base_url": "https://example.invalid"}},
    )
    assert unsafe.status_code == 422

    with store.lock():
        store.switch_branch(
            source,
            verify=lambda binding: main_front._runtime_for_binding(store, binding),
        )
    assert main_front._project_runtime(store).revision_id == "cfg-inst-r000001"
    with store.lock():
        store.switch_branch(
            advanced,
            verify=lambda binding: main_front._runtime_for_binding(store, binding),
        )
    assert main_front._project_runtime(store).revision_id == "cfg-inst-r000002"

    def fail_publish(*_args, **_kwargs):
        raise OSError("simulated revision publication failure")

    monkeypatch.setattr(runtime_settings_api, "publish_revision", fail_publish)
    failed = client.post(
        f"/api/projects/{store.project_id}/runtime-settings",
        json={
            **request,
            "base_revision_id": "cfg-inst-r000002",
            "idempotency_key": "settings-command-rollback",
            "overrides": {"candidate_concurrency": 4},
        },
    )
    assert failed.status_code == 503
    assert store.pending_transaction() is None
    assert store.read_manifest()["current_branch"] == body["branch_id"]
    assert store.find_config_apply("settings-command-rollback") is None
