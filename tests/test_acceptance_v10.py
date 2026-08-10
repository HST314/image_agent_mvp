from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main_front
from agent_core.workflow_runner import WorkflowRunner
from configs.runtime_policy import RuntimePolicy
from storage.project_store import ProjectStore


def _png(color: str, size: tuple[int, int] = (4, 4)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(
        WorkflowRunner,
        "_inspect",
        lambda *_: {"passed": True, "decision": "pass", "rework_prompt_delta": "", "confidence": 0.99},
    )
    monkeypatch.setattr(
        WorkflowRunner,
        "_image_call",
        lambda runner, *_args, **_kwargs: runner.store.artifacts.save_bytes(
            _png("yellow"), metadata={"kind": "human_tune_child"}
        ),
    )
    return TestClient(main_front.app, raise_server_exceptions=False)


def _seed(project_id: str, *, color: str = "white") -> tuple[ProjectStore, dict]:
    store = ProjectStore(main_front.PROJECTS_ROOT, project_id)
    store.create(RuntimePolicy(offline_mode=True).snapshot())
    asset = store.artifacts.save_bytes(_png(color), metadata={"kind": "seed"})
    snapshot = {
        "state": "self_check_iteration",
        "domain_state": "quality_rework",
        "phase": "waiting_human_approval",
        "waiting": True,
        "round": 1,
        "asset": asset,
        "current_asset": asset,
        "best_asset": asset,
        "task_specification": {"task_id": "t", "version": 1, "facts": [], "parent_hash": None, "content_hash": "s"},
        "task_revision": {"revision_hash": "revision-1"},
        "task_approval": {"revision_hash": "revision-1", "actor": "acceptance-user"},
        "selected_policy": {"termination": "solo", "release": "auto", "max_rounds": 1},
        "termination_reason": "solo_round_limit",
        "termination_satisfied": False,
    }
    store.checkpoint("self_check_iteration", snapshot)
    return store, asset


def _advance_to_delivery(api: TestClient, project_id: str, **payload) -> dict:
    response = api.post(
        f"/api/projects/{project_id}/advance",
        json={"final_approved": True, **payload},
    )
    assert response.status_code == 200, response.text
    return response.json()["snapshot"]


def test_round_limit_cost_confirmation_routes_back_to_real_quality_api(api: TestClient) -> None:
    store, original = _seed("paid-round")
    disposition = api.post(
        "/api/projects/paid-round/quality-disposition",
        json={"action": "add_rounds_with_cost_confirmation", "additional_rounds": 1, "cost_confirmed": True},
    )
    assert disposition.status_code == 200, disposition.text
    waiting = api.get("/api/projects/paid-round").json()
    assert waiting["snapshot"]["phase"] == "additional_rounds_approved"
    assert waiting["capabilities"] == ["resume_quality_inspection"]
    assert WorkflowRunner(store, main_front.MODEL_CONFIG, offline_mode=True).next_state(store.resume()) == "self_check_iteration"

    delivered = _advance_to_delivery(api, "paid-round")
    assert delivered["completed"]
    assert delivered["final_asset"]["sha256"] == original["sha256"]
    assert delivered["frozen_delivery"]["asset_sha256"] == original["sha256"]


def test_best_asset_human_tune_stays_in_human_review_and_freezes_child(api: TestClient) -> None:
    store, original = _seed("human-tune", color="red")
    response = api.post(
        "/api/projects/human-tune/quality-disposition",
        json={"action": "human_tune_best"},
    )
    assert response.status_code == 200, response.text
    waiting = api.get("/api/projects/human-tune").json()
    assert waiting["capabilities"] == ["submit_human_tune"]
    assert WorkflowRunner(store, main_front.MODEL_CONFIG, offline_mode=True).next_state(store.resume()) == "human_prompt_iteration"

    tuned = api.post(
        "/api/projects/human-tune/advance",
        json={"human_prompt": "仅调整主体颜色"},
    )
    assert tuned.status_code == 200, tuned.text
    child = tuned.json()["snapshot"]["asset"]
    assert tuned.json()["snapshot"]["phase"] == "waiting_human_tune"
    assert tuned.json()["capabilities"] == ["submit_human_tune"]
    assert child["sha256"] != original["sha256"]

    delivered = _advance_to_delivery(api, "human-tune", manual_action="accept_current")
    assert delivered["final_asset"]["sha256"] == child["sha256"]
    assert delivered["frozen_delivery"]["asset_sha256"] == child["sha256"]
    assert store.artifacts.resolve(original["artifact_id"])[1]["sha256"] == original["sha256"]


def test_annotation_api_atomically_checkpoints_child_then_stays_in_human_review(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, original = _seed("annotation", color="blue")

    def create_child(runner: WorkflowRunner, _state: str, _prompt: str, _refs: list[str], **_kwargs) -> dict:
        return runner.store.artifacts.save_bytes(_png("green"), metadata={"kind": "annotation_child"})

    monkeypatch.setattr(WorkflowRunner, "_image_call", create_child)
    assert api.post(
        "/api/projects/annotation/quality-disposition", json={"action": "human_tune_best"}
    ).status_code == 200
    response = api.post(
        "/api/projects/annotation/annotations",
        json={
            "artifact_id": original["artifact_id"],
            "marks": [{"kind": "rectangle", "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}],
            "prompt": "按圈画区域微调",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    child = result["asset"]
    snapshot = store.resume()
    assert result["requires_reinspection"] is False and result["checkpoint_id"]
    assert snapshot["phase"] == "waiting_human_tune"
    assert snapshot["asset"]["sha256"] == child["sha256"]
    assert snapshot["annotation_parent_asset"]["sha256"] == original["sha256"]
    assert snapshot["annotation_guide_asset"]["sha256"] == result["guide_asset"]["sha256"]
    assert store.artifacts.resolve(original["artifact_id"])[0].read_bytes() == _png("blue")

    delivered = _advance_to_delivery(api, "annotation", manual_action="accept_current")
    assert delivered["latest_checked_asset_hash"] == child["sha256"]
    assert delivered["final_asset"]["sha256"] == child["sha256"]
    assert delivered["frozen_delivery"]["asset_sha256"] == child["sha256"]


def test_human_tune_supports_multiple_rounds_without_automatic_inspection(api: TestClient) -> None:
    store, original = _seed("human-multi", color="red")
    assert api.post(
        "/api/projects/human-multi/quality-disposition", json={"action": "human_tune_best"}
    ).status_code == 200

    first = api.post("/api/projects/human-multi/advance", json={"human_prompt": "第一轮微调"})
    assert first.status_code == 200, first.text
    second = api.post("/api/projects/human-multi/advance", json={"human_prompt": "第二轮微调"})
    assert second.status_code == 200, second.text
    second_snapshot = second.json()["snapshot"]

    assert second_snapshot["phase"] == "waiting_human_tune"
    assert second_snapshot["human_tune_mode"] is True
    assert second_snapshot["asset"]["sha256"] != original["sha256"]
    recent = store.history()
    assert len([event for event in recent if event["type"] == "calibration_invalidated"]) == 2
    assert not any(event["type"] == "inspection_started" for event in recent)
