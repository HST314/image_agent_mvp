from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

import main_front
from agent_core.workflow import SelfCheckPolicy
from calibrator.calibration_loop import CalibrationLoop, ManualAction
from configs.runtime_policy import RuntimePolicy
from storage.project_store import ProjectStore


def _asset(store: ProjectStore, color: str = "white") -> dict:
    image = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(image, "PNG")
    return store.artifacts.save_bytes(image.getvalue(), metadata={"kind": "test"})


def _inspection(score: float = 70, confidence: float = .9) -> dict:
    return {
        "passed": False, "decision": "continue", "rework_prompt_delta": "微调",
        "overall_score": score, "dimension_scores": {"构图": score}, "confidence": confidence,
    }


def test_intermediate_quality_checkpoint_is_complete_and_resumable(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "canonical"); store.create()
    asset = _asset(store)
    context = {"state": "master_candidate_selection", "task_specification": {"content_hash": "spec"}, "master_asset": asset}
    result = CalibrationLoop(
        store, SelfCheckPolicy("solo", "manual", max_rounds=3),
        inspector=lambda *_: _inspection(), reworker=lambda _: asset,
    ).run(current_asset=asset, stable_specification="s", constraints=[], snapshot_context=context)
    restored = store.resume()
    assert result["round"] == 1
    assert restored["state"] == "self_check_iteration"
    assert restored["task_specification"] == context["task_specification"]
    assert restored["inspection_asset"]["sha256"] == asset["sha256"]


def test_one_manual_decision_advances_exactly_one_round(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "one-release"); store.create()
    original = _asset(store)
    reworked = _asset(store, "black")
    inspections: list[str] = []
    reworks: list[dict] = []
    result = CalibrationLoop(
        store, SelfCheckPolicy("solo", "manual", max_rounds=4),
        inspector=lambda uri, _spec: inspections.append(uri) or _inspection(),
        reworker=lambda assembled: reworks.append(assembled) or reworked,
    ).run(
        current_asset=original, stable_specification="s", constraints=[],
        approve=lambda _: ManualAction(action="execute"), snapshot_context={"master_asset": original},
    )
    assert result["phase"] == "waiting_human_approval" and result["round"] == 2
    assert len(inspections) == 2 and len(reworks) == 1


def test_best_asset_uses_quality_score_not_confidence(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "score"); store.create()
    better = _asset(store, "white")
    current = _asset(store, "black")
    store.events.append("inspection_completed", round=1, asset=better,
                        result=_inspection(score=92, confidence=.55), idempotency_key="prior")
    result = CalibrationLoop(
        store, SelfCheckPolicy("solo", "auto", max_rounds=2),
        inspector=lambda *_: _inspection(score=30, confidence=.99), reworker=lambda _: current,
    ).run(current_asset=current, stable_specification="s", constraints=[], start_round=2)
    assert result["best_asset"]["sha256"] == better["sha256"]


def test_normal_manual_gate_can_enter_tune_and_clears_limit_fields(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", root)
    store = ProjectStore(root, "ordinary-tune"); store.create(RuntimePolicy(offline_mode=True).snapshot())
    asset = _asset(store)
    store.checkpoint("self_check_iteration", {
        "state": "self_check_iteration", "phase": "waiting_human_approval", "round": 5,
        "asset": asset, "current_asset": asset, "inspection_asset": asset,
        "inspection": _inspection(), "termination_reason": "manual_release_required",
        "available_actions": ["abandon"], "best_asset": asset,
    })
    response = TestClient(main_front.app).post(
        "/api/projects/ordinary-tune/quality-disposition", json={"action": "human_tune_best"},
    )
    assert response.status_code == 200, response.text
    restored = store.resume()
    assert restored["phase"] == "waiting_human_tune"
    assert restored["available_actions"] == [] and restored["best_asset"] is None
    assert restored["termination_reason"] == "human_tune_in_progress"
