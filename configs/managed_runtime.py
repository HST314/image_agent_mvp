"""Validated runtime files supplied by the owning Harness process."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from model_router.router import ModelRouter

from .runtime_policy import RuntimePolicy


@dataclass(frozen=True, slots=True)
class ManagedRuntime:
    model_config_path: Path
    runtime_policy_path: Path
    policy: RuntimePolicy

    @classmethod
    def from_environment(cls) -> "ManagedRuntime":
        return cls.from_paths(
            _environment_path("IMAGE_AGENT_MODEL_CONFIG"),
            _environment_path("IMAGE_AGENT_RUNTIME_POLICY"),
        )

    @classmethod
    def from_paths(
        cls,
        model_config_path: Path | None,
        runtime_policy_path: Path | None,
    ) -> "ManagedRuntime":
        if model_config_path is None or runtime_policy_path is None:
            raise RuntimeError(
                "Image Agent requires the read-only runtime files supplied by Harness."
            )
        model_path = model_config_path.resolve()
        runtime_path = runtime_policy_path.resolve()
        if not model_path.is_file() or not runtime_path.is_file():
            raise RuntimeError("The Harness-supplied Image Agent runtime files are unavailable.")
        policy = RuntimePolicy.from_file(runtime_path)
        if policy.offline_mode:
            raise RuntimeError("Managed Image Agent runtime cannot enable offline mode.")
        router = ModelRouter.from_file(model_path)
        router.validate_required_bindings()
        router.validate_managed_bindings()
        return cls(model_path, runtime_path, policy)


def optional_environment_path(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value) if value else None


def _environment_path(name: str) -> Path | None:
    return optional_environment_path(name)
