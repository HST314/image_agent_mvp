"""T36 health diagnostics and T37 release-gate contracts."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import diagnostics
import main_front


def test_health_reports_all_required_probes_without_internal_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    response = TestClient(main_front.app).get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["trace_id"].startswith("trace_")
    assert {item["name"] for item in body["checks"]} == {
        "model_router", "job_executor", "storage", "event_writer", "asset_api", "runtime_resources"
    }
    serialized = response.text.lower()
    assert str(tmp_path).lower() not in serialized
    assert not any(word in serialized for word in ("api_key", "authorization", "traceback"))


@pytest.mark.parametrize("failed_probe,expected_code", [
    ("model_router", "MODEL_ROUTER_UNAVAILABLE"),
    ("job_executor", "JOB_EXECUTOR_UNAVAILABLE"),
    ("storage", "STORAGE_UNAVAILABLE"),
    ("event_writer", "EVENT_WRITER_UNAVAILABLE"),
    ("asset_api", "ASSET_API_UNAVAILABLE"),
    ("runtime_resources", "RUNTIME_RESOURCES_UNAVAILABLE"),
])
def test_health_failure_matrix_uses_stable_safe_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_probe: str, expected_code: str
) -> None:
    original = diagnostics._probe

    def fail_selected(name, code, trace_id, check):
        if name == failed_probe:
            check = lambda: (_ for _ in ()).throw(RuntimeError("secret=/private/path token=hidden"))
        return original(name, code, trace_id, check)

    monkeypatch.setattr(diagnostics, "_probe", fail_selected)
    result = diagnostics.run_diagnostics(
        projects_root=tmp_path / "projects", model_config=main_front.MODEL_CONFIG,
        app_root=main_front.APP_ROOT, job_registry=main_front.JOBS,
    )
    assert result["status"] == "degraded"
    failed = next(item for item in result["checks"] if item["name"] == failed_probe)
    assert failed == {"name": failed_probe, "status": "error", "error_code": expected_code}
    assert "/private/path" not in str(result)


def test_http_health_degraded_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_front, "run_diagnostics", lambda **_: {
        "status": "degraded", "trace_id": "trace_public",
        "checks": [{"name": "storage", "status": "error", "error_code": "STORAGE_UNAVAILABLE"}],
    })
    response = TestClient(main_front.app).get("/api/health")
    assert response.status_code == 503
    assert response.json()["trace_id"] == "trace_public"


def test_release_gate_assets_are_present() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "scripts/release_install_gate.py").is_file()
    text = (root / "docs/RELEASE_GATE.md").read_text(encoding="utf-8")
    assert "回滚" in text and "已知限制" in text and "真实供应商" in text
