from __future__ import annotations

import subprocess
import sys
import io
from pathlib import Path

import pytest
from PIL import Image

from configs.runtime_policy import RuntimePolicy
from storage.project_store import ProjectStore


REPO = Path(__file__).resolve().parents[1]


def _seed(root: Path, project_id: str) -> ProjectStore:
    store = ProjectStore(root, project_id)
    store.create(RuntimePolicy(offline_mode=True).snapshot())
    image = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(image, "PNG")
    asset = store.artifacts.save_bytes(image.getvalue(), metadata={"kind": "seed"})
    snapshot = {
        "state": "self_check_iteration",
        "phase": "waiting_human_approval",
        "waiting": True,
        "round": 4,
        "asset": asset,
        "current_asset": asset,
        "best_asset": asset,
        "inspection": {"passed": False, "decision": "continue", "rework_prompt_delta": "微调", "confidence": 0.8},
        "task_specification": {"task_id": "t", "version": 1, "facts": [], "parent_hash": None, "content_hash": "s"},
        "self_check_policy": {"termination": "solo", "release": "auto", "fixed_rounds": 2, "max_rounds": 4},
        "selected_policy": {"termination": "solo", "release": "auto", "fixed_rounds": 2, "max_rounds": 4},
        "termination_reason": "solo_round_limit",
        "termination_satisfied": False,
        "latest_checked_asset_hash": asset["sha256"],
    }
    store.checkpoint("self_check_iteration", snapshot)
    return store


def _cli(root: Path, project_id: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "main.py", "--projects-root", str(root), "resume", project_id, "--offline", *args],
        cwd=REPO, text=True, capture_output=True, timeout=30, check=False,
    )


@pytest.mark.parametrize("action", ["accept_current", "end"])
def test_real_cli_process_consumes_terminal_disposition_without_reinspection(tmp_path: Path, action: str) -> None:
    root = tmp_path / "projects"
    store = _seed(root, action)
    before = len([event for event in store.history() if event["type"] == "inspection_completed"])
    extra = ("--approve-final",) if action == "accept_current" else ()
    result = _cli(root, action, "--manual-action", action, *extra)
    assert result.returncode == 0, result.stdout + result.stderr
    snapshot = store.resume()
    assert len([event for event in store.history() if event["type"] == "inspection_completed"]) == before
    if action == "accept_current":
        assert snapshot["completed"] and snapshot["calibration_status"] == "human_accepted"
    else:
        assert snapshot["phase"] == "terminated_without_delivery" and snapshot["waiting"]


def test_real_cli_process_adds_confirmed_rounds_and_resumes_inspection(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    store = _seed(root, "add-rounds")
    result = _cli(root, "add-rounds", "--manual-action", "add_rounds", "--additional-rounds", "1", "--confirm-cost")
    assert result.returncode == 0, result.stdout + result.stderr
    snapshot = store.resume()
    dispositions = [event for event in store.history() if event["type"] == "quality_disposition"]
    assert dispositions[-1]["action"] == "add_rounds" and dispositions[-1]["cost_confirmed"] is True
    assert snapshot["round"] == 5 and snapshot["termination_reason"] == "solo_round_limit"
    assert any(event["type"] == "inspection_completed" and event["round"] == 5 for event in store.history())


def test_real_cli_process_tunes_best_asset_and_requires_reinspection(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    store = _seed(root, "human-tune")
    original_hash = store.resume()["asset"]["sha256"]
    result = _cli(root, "human-tune", "--manual-action", "human_tune_best", "--human-prompt", "只调整主体颜色")
    assert result.returncode == 0, result.stdout + result.stderr
    snapshot = store.resume()
    assert snapshot["phase"] == "waiting_reinspection" and snapshot["waiting"]
    assert snapshot["latest_checked_asset_hash"] is None
    assert snapshot["asset"]["sha256"] != original_hash
    assert any(event["type"] == "quality_disposition" and event["action"] == "human_tune_best" for event in store.history())
