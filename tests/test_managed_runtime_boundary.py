from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main_front
from configs.managed_runtime import ManagedRuntime

ROOT = Path(__file__).resolve().parents[1]
MODEL_CONFIG = ROOT / "tests" / "fixtures" / "model_config.yaml"
RUNTIME_POLICY = ROOT / "tests" / "fixtures" / "runtime.yaml"


def test_formal_api_rejects_an_offline_request_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    response = TestClient(main_front.app, raise_server_exceptions=False).post(
        "/api/projects",
        json={"project_id": "formal-entry", "task_card": {}, "offline": True},
    )
    assert response.status_code == 422


def test_managed_runtime_rejects_test_only_policy_and_provider(tmp_path: Path) -> None:
    offline_runtime = tmp_path / "runtime.yaml"
    offline_runtime.write_text(
        RUNTIME_POLICY.read_text(encoding="utf-8").replace(
            "offline_mode: false", "offline_mode: true"
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="cannot enable offline mode"):
        ManagedRuntime.from_paths(MODEL_CONFIG, offline_runtime)

    offline_models = tmp_path / "model_config.yaml"
    offline_models.write_text(
        MODEL_CONFIG.read_text(encoding="utf-8").replace(
            "provider: ark", "provider: offline"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot use the offline provider"):
        ManagedRuntime.from_paths(offline_models, RUNTIME_POLICY)


def test_private_runtime_examples_and_user_instructions_are_absent() -> None:
    assert not (ROOT / "configs" / "model_config.yaml").exists()
    assert not (ROOT / "configs" / "runtime.yaml").exists()
    assert not (ROOT / "examples" / "sample_model_config.yaml").exists()

    forbidden = (
        "--model-config",
        "--offline",
        "configs/model_config.yaml",
        "configs/runtime.yaml",
        "ARK_API_KEY",
    )
    for relative in ("README.md", "FRONTEND_README.md", "docs/user_and_developer_guide.md"):
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert not any(item in content for item in forbidden), relative
