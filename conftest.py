"""Shared test-only runtime registration for the managed Image Agent boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

import main_front
from agent_core.workflow_runner import WorkflowRunner
from configs.runtime_policy import RuntimePolicy

ROOT = Path(__file__).resolve().parent
MODEL_CONFIG = ROOT / "tests" / "fixtures" / "model_config.yaml"
RUNTIME_POLICY = ROOT / "tests" / "fixtures" / "runtime.yaml"


@pytest.fixture(autouse=True)
def managed_runtime_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_front, "MODEL_CONFIG", MODEL_CONFIG)
    monkeypatch.setattr(main_front, "RUNTIME_POLICY_PATH", RUNTIME_POLICY)


@pytest.fixture()
def offline_frontend_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register test doubles without exposing an offline product request field."""

    policy = RuntimePolicy.from_file(RUNTIME_POLICY).model_copy(
        update={"offline_mode": True}
    )
    monkeypatch.setattr(main_front, "_global_policy", lambda: policy)
    monkeypatch.setattr(
        main_front,
        "_runner",
        lambda store: WorkflowRunner(store, MODEL_CONFIG, offline_mode=True),
    )
    monkeypatch.setattr(
        main_front,
        "_project_runner",
        lambda store: WorkflowRunner(store, MODEL_CONFIG, offline_mode=True),
    )
