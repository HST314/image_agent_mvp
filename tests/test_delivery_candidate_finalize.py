"""Regression tests for delivery candidate finalize idempotency and lock waiting.

Covers the two delivery-page save fixes:
- the project lock waits briefly for transient contention instead of failing
  instantly with a 423;
- repeated candidate finalization short-circuits on the existing marker instead
  of re-reading and re-hashing the finalized image on every observer sweep.
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main_front
from agent_core.delivery import load_finalized_candidate_marker
from storage.project_store import ProjectLockError, ProjectStore


def _png(color: str) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture()
def api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline_frontend_runtime: None,
) -> TestClient:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    return TestClient(main_front.app, raise_server_exceptions=False)


def _seed_completed(root: Path, project_id: str) -> tuple[ProjectStore, dict, str]:
    store = ProjectStore(root, project_id)
    store.create()
    asset = store.artifacts.save_bytes(_png("white"), metadata={"kind": "seed"})
    checkpoint_id = store.checkpoint(
        "delivery_frozen",
        {
            "completed": True,
            "final_asset": asset,
            "frozen_delivery": {"asset_sha256": asset["sha256"]},
            "task_card": {"task_id": "t", "deliverable_goal": "海报"},
        },
    )
    return store, asset, checkpoint_id


def test_project_lock_waits_for_transient_contention(tmp_path: Path) -> None:
    holder = ProjectStore(tmp_path, "p")
    holder.create()
    acquired: list[float] = []

    def contender() -> None:
        with ProjectStore(tmp_path, "p").lock():
            acquired.append(time.monotonic())

    with holder.lock():
        started = time.monotonic()
        thread = threading.Thread(target=contender)
        thread.start()
        time.sleep(0.5)
    thread.join(5)
    assert not thread.is_alive()
    assert acquired and acquired[0] - started >= 0.4


def test_project_lock_times_out_on_persistent_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("storage.project_store.LOCK_ACQUIRE_TIMEOUT_SECONDS", 0.3)
    holder = ProjectStore(tmp_path, "p")
    holder.create()
    started = time.monotonic()
    with holder.lock():
        with pytest.raises(ProjectLockError):
            with ProjectStore(tmp_path, "p").lock():
                pass
    assert time.monotonic() - started >= 0.3


def test_candidates_finalize_short_circuits_on_existing_marker(api: TestClient) -> None:
    store, asset, _checkpoint_id = _seed_completed(main_front.PROJECTS_ROOT, "pd")
    first = api.post("/api/projects/pd/delivery/candidates/finalize")
    assert first.status_code == 200, first.text
    candidate = first.json()["candidates"][0]

    # Remove the source image: any path that re-reads or re-hashes the original
    # bytes must now fail, while the marker short-circuit must not notice.
    source, _record = store.artifacts.resolve(asset["artifact_id"])
    source.unlink()

    second = api.post("/api/projects/pd/delivery/candidates/finalize")
    assert second.status_code == 200, second.text
    assert second.json()["candidates"][0] == candidate
    assert (store.root / candidate["files"]["image"]).is_file()
    assert (store.root / candidate["files"]["markdown"]).is_file()


def test_user_finalize_reuses_marker_without_touching_image(api: TestClient) -> None:
    store, asset, _checkpoint_id = _seed_completed(main_front.PROJECTS_ROOT, "pd")
    first = api.post("/api/projects/pd/delivery/finalize")
    assert first.status_code == 200, first.text
    bundle_id = first.json()["bundle_id"]

    source, _record = store.artifacts.resolve(asset["artifact_id"])
    source.unlink()

    second = api.post("/api/projects/pd/delivery/finalize")
    assert second.status_code == 200, second.text
    assert second.json()["bundle_id"] == bundle_id
    assert second.json() == first.json()


def test_load_finalized_candidate_marker_requires_exact_identity(tmp_path: Path) -> None:
    store, asset, checkpoint_id = _seed_completed(tmp_path, "p")
    envelope = store.checkpoints.load(checkpoint_id)
    branch = envelope["branch"]
    assert (
        load_finalized_candidate_marker(
            store.root, "p", branch, checkpoint_id, asset["sha256"]
        )
        is None
    )

    api_root = tmp_path / "unused"
    api_root.mkdir()
    # Materialize the marker through the real finalize path.
    from agent_core.delivery import build_delivery, finalize_delivery_candidate

    source, record = store.artifacts.resolve(asset["artifact_id"])
    delivery_envelope = build_delivery(
        {"task_card": {"task_id": "t"}}, "p", asset, "trace"
    )
    marker = finalize_delivery_candidate(
        store.root,
        delivery_envelope,
        source,
        branch_id=branch,
        checkpoint_id=checkpoint_id,
        created_at="2026-08-30T00:00:00Z",
    )
    assert (
        load_finalized_candidate_marker(
            store.root, "p", branch, checkpoint_id, asset["sha256"]
        )
        == marker
    )
    # Any identity change (other asset, other checkpoint, other branch) misses.
    other_sha = "0" * 64
    assert (
        load_finalized_candidate_marker(
            store.root, "p", branch, checkpoint_id, other_sha
        )
        is None
    )
    assert (
        load_finalized_candidate_marker(
            store.root, "p", branch, "checkpoint_other", asset["sha256"]
        )
        is None
    )
    assert (
        load_finalized_candidate_marker(
            store.root, "p", "branch-other", checkpoint_id, asset["sha256"]
        )
        is None
    )
