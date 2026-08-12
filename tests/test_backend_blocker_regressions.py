"""Fault-injection regressions for the final backend release blockers."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

import main_front
from agent_core.jobs import JobRegistry


def test_late_cancel_preserves_committed_result_and_truthful_terminal_state(tmp_path: Path) -> None:
    """A request arriving after execution starts cannot erase committed work."""
    registry = JobRegistry(tmp_path, workers=1)
    entered = threading.Event()
    release = threading.Event()
    side_effects: list[str] = []

    def execute() -> dict[str, bool]:
        entered.set()
        assert release.wait(2)
        side_effects.append("committed")
        return {"published": True}

    job, _ = registry.submit("p", "late-cancel-key", "advance", execute)
    assert entered.wait(2)
    cancelling = registry.cancel(job["job_id"])
    assert cancelling["status"] == "cancelling"
    release.set()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = registry.get(job["job_id"])
        if record["status"] == "succeeded":
            break
        time.sleep(0.01)

    assert side_effects == ["committed"]
    assert record["status"] == "succeeded"
    assert record["result"] == {"published": True}
    assert record["cancellation_requested"] is True
    assert record["events"][-1]["type"] == "succeeded"
    assert record["events"][-1]["cancellation_requested"] is True


def test_chunked_body_cannot_bypass_512_kib_limit() -> None:
    """An unknown-length streaming body is rejected before JSON validation."""
    client = TestClient(main_front.app, raise_server_exceptions=False)
    chunk = b"x" * (128 * 1024)

    def oversized_body():
        for _ in range(5):
            yield chunk

    response = client.post(
        "/api/projects",
        content=oversized_body(),
        headers={"content-type": "application/json", "transfer-encoding": "chunked"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "请求内容超过 512 KiB 限制。"
