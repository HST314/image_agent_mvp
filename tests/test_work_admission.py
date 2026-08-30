from __future__ import annotations

import json

import main_front
from fastapi.testclient import TestClient


def test_managed_quiesce_gate_rejects_new_mutations(tmp_path, monkeypatch) -> None:
    control = tmp_path / "managed-control.json"
    control.write_text(json.dumps({"quiesced": True}), encoding="utf-8")
    monkeypatch.setattr(main_front, "MANAGED_MODE", True)
    monkeypatch.setattr(main_front, "MANAGED_CONTROL_FILE", str(control))

    with TestClient(main_front.app) as client:
        observed = client.get("/api/managed/work-admission")
        rejected = client.post("/api/projects/project_1/jobs", json={})

    assert observed.status_code == 200
    assert observed.json() == {"quiesced": True}
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "agent_quiesced"


def test_managed_quiesce_gate_fails_closed_on_corrupt_control(tmp_path, monkeypatch) -> None:
    control = tmp_path / "managed-control.json"
    control.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(main_front, "MANAGED_MODE", True)
    monkeypatch.setattr(main_front, "MANAGED_CONTROL_FILE", str(control))

    assert main_front._work_admission_quiesced() is True
