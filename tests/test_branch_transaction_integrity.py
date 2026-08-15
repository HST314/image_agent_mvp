from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main_front
from storage.project_store import CorruptProjectError, ProjectStore, atomic_json


def _store(tmp_path: Path) -> tuple[ProjectStore, str]:
    store = ProjectStore(tmp_path, "branch_integrity")
    store.create({"offline_mode": True})
    source = store.checkpoint("confirmation_build", {"state": "confirmation_build", "phase": "waiting"})
    return store, source


def test_branch_verification_failure_rolls_back_every_control_file(tmp_path: Path):
    store, source = _store(tmp_path)
    previous_manifest = store.manifest()

    def fail_view() -> None:
        raise FileNotFoundError("private/projects/new/checkpoint.json")

    with pytest.raises(FileNotFoundError):
        store.branch_from(source, name="校验失败分支", verify=fail_view)

    assert store.manifest() == previous_manifest
    assert "校验失败分支" not in json.loads((store.root / "branches.json").read_text())["branches"]
    assert all(item["branch"] != "校验失败分支" for item in store.checkpoints.list())
    assert not (store.root / "checkpoints" / "校验失败分支").exists()
    assert not (store.root / "transactions/pending.json").exists()


def test_page_projection_failure_after_api_commit_does_not_rollback_branch(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    store, source = _store(tmp_path)
    real_view = main_front._project_view
    attempts = 0

    def transient_view(project_store, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FileNotFoundError("transient projection read")
        return real_view(project_store, *args, **kwargs)

    monkeypatch.setattr(main_front, "_project_view", transient_view)
    client = TestClient(main_front.app, raise_server_exceptions=False)
    response = client.post(
        f"/api/projects/{store.project_id}/branches",
        json={"checkpoint": source, "name": "投影失败分支", "mode": "rerun_stage"},
    )
    assert response.status_code == 409
    assert store.manifest()["current_branch"] == "投影失败分支"
    assert "投影失败分支" in json.loads((store.root / "branches.json").read_text())["branches"]
    assert not (store.root / "transactions/pending.json").exists()

    reconciled = client.get(f"/api/projects/{store.project_id}")
    assert reconciled.status_code == 200
    assert reconciled.json()["manifest"]["current_branch"] == "投影失败分支"


def test_read_paths_never_recover_or_delete_a_writer_transaction(tmp_path: Path):
    store, source = _store(tmp_path)
    previous_manifest = store.manifest()
    source_envelope = store.checkpoints.load(source)
    branch = "恢复校验分支"
    prepared = store.checkpoints.prepare(branch, 1, source_envelope["state"], source_envelope["data"])
    checkpoint_id, relative, checksum = store.checkpoints.save(
        branch, 1, source_envelope["state"], source_envelope["data"], prepared=prepared,
    )
    branches_path = store.root / "branches.json"
    branches = json.loads(branches_path.read_text())
    branches["branches"][branch] = {"parent": "main", "from_checkpoint": source}
    atomic_json(branches_path, branches)
    manifest = store.manifest()
    manifest.update(current_branch=branch, current_checkpoint={
        "checkpoint_id": checkpoint_id, "checksum": checksum, "branch": branch,
        "sequence": 1, "state": source_envelope["state"],
    })
    atomic_json(store.root / "manifest.json", manifest)
    atomic_json(store.root / "transactions/pending.json", {
        "format_version": 1, "kind": "branch", "status": "prepared", "branch": branch,
        "sequence": 1, "state": source_envelope["state"], "data": source_envelope["data"],
        "from_checkpoint": source, "checkpoint_id": checkpoint_id, "path": relative,
        "checksum": checksum, "previous_manifest": previous_manifest,
    })
    (store.root / relative).unlink()

    # Ordinary project reads serve the last complete version and leave the
    # writer's intent untouched.  They must never roll back another request.
    assert store.resume() == source_envelope["data"]
    assert store.progress_snapshots()[-1]["checkpoint_id"] == source
    assert (store.root / "transactions/pending.json").is_file()
    assert store.manifest()["current_branch"] == branch

    # Recovery is an explicit, lock-owning write operation.
    store.recover_pending_transaction()
    assert store.manifest() == previous_manifest
    assert branch not in json.loads(branches_path.read_text())["branches"]
    assert checkpoint_id not in {item["checkpoint_id"] for item in store.checkpoints.list()}


def test_project_get_during_branch_intent_serves_previous_commit_without_mutation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    store, source = _store(tmp_path)
    previous_manifest = store.manifest()
    source_envelope = store.checkpoints.load(source)
    atomic_json(store.root / "transactions/pending.json", {
        "format_version": 1,
        "kind": "branch",
        "status": "intent",
        "branch": "正在写入的分支",
        "sequence": 1,
        "state": source_envelope["state"],
        "data": source_envelope["data"],
        "from_checkpoint": source,
        "checkpoint_id": "checkpoint_pending0000000000000",
        "path": "checkpoints/正在写入的分支/000001-confirmation_build.json",
        "checksum": "pending",
        "previous_manifest": previous_manifest,
    })

    client = TestClient(main_front.app, raise_server_exceptions=False)
    response = client.get(f"/api/projects/{store.project_id}")

    assert response.status_code == 200
    assert response.json()["manifest"] == previous_manifest
    assert response.json()["snapshot"] == source_envelope["data"]
    assert (store.root / "transactions/pending.json").is_file()


def test_branch_listing_hides_pending_branch_and_checkpoint_without_mutation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    store, source = _store(tmp_path)
    previous_manifest = store.manifest()
    source_envelope = store.checkpoints.load(source)
    branch = "尚未提交分支"
    prepared = store.checkpoints.prepare(branch, 1, source_envelope["state"], source_envelope["data"])
    checkpoint_id, relative, checksum = store.checkpoints.save(
        branch, 1, source_envelope["state"], source_envelope["data"], prepared=prepared,
    )
    branches_path = store.root / "branches.json"
    branch_document = json.loads(branches_path.read_text())
    branch_document["branches"][branch] = {"parent": "main", "from_checkpoint": source}
    atomic_json(branches_path, branch_document)
    manifest = dict(previous_manifest)
    manifest.update(current_branch=branch, current_checkpoint={
        "checkpoint_id": checkpoint_id, "checksum": checksum, "branch": branch,
        "sequence": 1, "state": source_envelope["state"],
    })
    atomic_json(store.root / "manifest.json", manifest)
    atomic_json(store.root / "transactions/pending.json", {
        "format_version": 1, "kind": "branch", "status": "intent", "branch": branch,
        "sequence": 1, "state": source_envelope["state"], "data": source_envelope["data"],
        "from_checkpoint": source, "checkpoint_id": checkpoint_id, "path": relative,
        "checksum": checksum, "previous_manifest": previous_manifest,
    })
    before = {
        "manifest": (store.root / "manifest.json").read_bytes(),
        "branches": branches_path.read_bytes(),
        "index": store.checkpoints.index_path.read_bytes(),
        "pending": (store.root / "transactions/pending.json").read_bytes(),
    }

    response = TestClient(main_front.app, raise_server_exceptions=False).get(
        f"/api/projects/{store.project_id}/branches"
    )

    assert response.status_code == 200
    listing = response.json()
    assert listing["current_branch"] == previous_manifest["current_branch"] == "main"
    assert listing["current_checkpoint_id"] == source
    assert [item["name"] for item in listing["items"]] == ["main"]
    assert checkpoint_id not in {
        checkpoint["checkpoint_id"]
        for item in listing["items"] for checkpoint in item["checkpoints"]
    }
    assert before == {
        "manifest": (store.root / "manifest.json").read_bytes(),
        "branches": branches_path.read_bytes(),
        "index": store.checkpoints.index_path.read_bytes(),
        "pending": (store.root / "transactions/pending.json").read_bytes(),
    }


def test_project_corrupt_response_is_sanitized_and_traceable(caplog):
    exc = CorruptProjectError("private/projects/用户/secret/checkpoint.json")
    exc.project_context = {
        "operation": "project_view",
        "transaction_phase": "prepared",
        "source_checkpoint": "checkpoint_source",
        "target_checkpoint": "checkpoint_target",
        "lock_owned_by_current": False,
    }

    response = main_front._translate_error(exc)

    assert response.status_code == 409
    assert response.detail["code"] == "PROJECT_CORRUPT"
    assert response.detail["trace_id"].startswith("trace_")
    assert "private/projects" not in response.detail["message"]
    assert "transaction_phase" in caplog.text


def test_health_tool_repairs_mojibake_index_by_checksum_with_backup(tmp_path: Path):
    store, source = _store(tmp_path)
    branch = store.branch_from(source, name="主图选择-0814-221126")
    pointer = store.manifest()["current_checkpoint"]
    checkpoint_id = pointer["checkpoint_id"]
    index = store.checkpoints._index()
    index["items"][checkpoint_id].update(
        branch="涓诲浘閫夋嫨-0814-221126",
        path="checkpoints/涓诲浘閫夋嫨-0814-221126/000001-confirmation_build.json",
    )
    atomic_json(store.checkpoints.index_path, index)

    dry_run = store.check_health()
    assert dry_run["healthy"] is False
    assert dry_run["issues"][0]["repairable"] is True
    assert dry_run["applied"] == 0 and dry_run["backup"] is None

    repaired = store.check_health(repair=True)
    assert repaired["healthy"] is True
    assert repaired["applied"] == 1
    assert (store.root / repaired["backup"] / "index.json").is_file()
    envelope = store.checkpoints.load(checkpoint_id)
    assert envelope["branch"] == branch
