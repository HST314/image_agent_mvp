"""Managed runtime revision application regressions."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main_front
from configs.runtime_revision import publish_revision
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
        overrides=(
            {} if parent is None else {"candidate_concurrency": policy.candidate_concurrency}
        ),
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
                "task_card": {"task_id": store.project_id},
            },
        )


def test_managed_apply_requires_registered_revision_and_replays_one_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    config_root = tmp_path / "instance-runtime-config"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", projects)
    monkeypatch.setattr(main_front, "CONFIG_ROOT", config_root)
    monkeypatch.setattr(main_front, "MANAGED_MODE", True)
    monkeypatch.setattr(main_front, "MANAGED_PROJECT_ID", "managed-project")
    monkeypatch.setattr(
        main_front, "MANAGED_ADAPTER_KEY", "managed-adapter-key-for-tests-12345"
    )
    real_ip_address = main_front.ipaddress.ip_address
    monkeypatch.setattr(
        main_front.ipaddress,
        "ip_address",
        lambda _value: real_ip_address("127.0.0.1"),
    )
    base = main_front._base_runtime()
    initial = _bundle(
        "managed-project",
        "cfg-inst-r000001",
        policy=base.policy,
        model_document=base.model_document,
        parent=None,
    )
    publish_revision(config_root, initial[0], initial[1], initial[2])
    atomic_json(config_root / "state.json", {"current_revision_id": "cfg-inst-r000001"})
    store = ProjectStore(projects, "managed-project")
    store.create(base.policy.snapshot(), config_binding=initial[3])
    source = _waiting_checkpoint(store)

    revised_policy = base.policy.model_copy(update={"candidate_concurrency": 2})
    revision = _bundle(
        "managed-project",
        "cfg-inst-r000002",
        policy=revised_policy,
        model_document=base.model_document,
        parent="cfg-inst-r000001",
        branch_id="config-placeholder",
        checkpoint_id=source,
        effective_from_state="confirmation_build",
    )
    revision[0].update(apply_status="CONFIRMED", branch_id=None, checkpoint_id=None)
    publish_revision(config_root, revision[0], revision[1], revision[2])
    client = TestClient(main_front.app, raise_server_exceptions=False)
    headers = {
        main_front.MANAGED_ADAPTER_HEADER: "managed-adapter-key-for-tests-12345"
    }
    request = {
        "runtime_config_revision_id": "cfg-inst-r000002",
        "from_checkpoint": source,
        "expected_config_hash": revision[0]["config_hash"],
        "effective_from_state": "confirmation_build",
        "idempotency_key": "managed-apply-0001",
    }
    active_for_project = main_front.JOBS.active_for_project
    monkeypatch.setattr(
        main_front.JOBS,
        "active_for_project",
        lambda _project_id: {"status": "running"},
    )
    blocked = client.post(
        "/api/managed/projects/managed-project/config-revisions/apply",
        headers=headers,
        json=request,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "SAFE_CHECKPOINT_UNAVAILABLE"
    assert store.find_config_apply("managed-apply-0001") is None
    monkeypatch.setattr(main_front.JOBS, "active_for_project", active_for_project)

    applied = client.post(
        "/api/managed/projects/managed-project/config-revisions/apply",
        headers=headers,
        json=request,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["runtime_config_revision_id"] == "cfg-inst-r000002"
    assert main_front._project_runtime(store).policy.candidate_concurrency == 2

    replay = client.post(
        "/api/managed/projects/managed-project/config-revisions/apply",
        headers=headers,
        json=request,
    )
    assert replay.status_code == 200
    assert replay.json()["branch_id"] == applied.json()["branch_id"]
    assert len(
        [
            item
            for item in store.branches()["items"]
            if item.get("runtime_config_revision_id") == "cfg-inst-r000002"
        ]
    ) == 1

    rejected = client.post(
        "/api/managed/projects/managed-project/config-revisions/apply",
        headers={main_front.MANAGED_ADAPTER_HEADER: "wrong-key"},
        json=request,
    )
    assert rejected.status_code == 403

    stale = _bundle(
        "managed-project",
        "cfg-inst-r000003",
        policy=base.policy.model_copy(update={"candidate_concurrency": 4}),
        model_document=base.model_document,
        parent="cfg-inst-r000001",
        branch_id="config-placeholder",
        checkpoint_id=applied.json()["checkpoint_id"],
        effective_from_state="self_check_inspection",
    )
    stale[0].update(apply_status="CONFIRMED", branch_id=None, checkpoint_id=None)
    publish_revision(config_root, stale[0], stale[1], stale[2])
    stale_response = client.post(
        "/api/managed/projects/managed-project/config-revisions/apply",
        headers=headers,
        json={
            "runtime_config_revision_id": "cfg-inst-r000003",
            "from_checkpoint": applied.json()["checkpoint_id"],
            "expected_config_hash": stale[0]["config_hash"],
            "effective_from_state": "self_check_inspection",
            "idempotency_key": "managed-apply-stale-parent",
        },
    )
    assert stale_response.status_code == 409
    assert stale_response.json()["detail"]["code"] == "SETTINGS_REVISION_CONFLICT"
    assert store.find_config_apply("managed-apply-stale-parent") is None
