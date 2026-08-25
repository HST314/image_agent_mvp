"""Validated immutable runtime revisions supplied by the owning process."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from model_router.router import ModelRouter

from .runtime_policy import RuntimePolicy
from .runtime_revision import (
    LoadedRuntimeRevision,
    canonical_json_bytes,
    canonical_yaml_bytes,
    current_revision_id,
    effective_runtime,
    load_revision,
    model_bindings,
    revision_content_hash,
    sha256_bytes,
)


@dataclass(frozen=True, slots=True)
class ManagedRuntime:
    model_config_path: Path
    runtime_policy_path: Path
    policy: RuntimePolicy
    revision_id: str
    task_config_revision_id: str
    runtime_config_sha256: str
    model_config_sha256: str
    config_hash: str
    model_document: dict[str, Any]
    overrides: dict[str, Any]
    manifest: dict[str, Any] | None = None

    @classmethod
    def from_environment(
        cls,
        *,
        revision_id: str | None = None,
        managed: bool = True,
    ) -> "ManagedRuntime":
        base = cls.from_paths(
            _environment_path("IMAGE_AGENT_MODEL_CONFIG"),
            _environment_path("IMAGE_AGENT_RUNTIME_POLICY"),
        )
        config_root = _environment_path("IMAGE_AGENT_CONFIG_ROOT")
        if config_root is None:
            if revision_id is not None and revision_id != base.revision_id:
                raise RuntimeError("The requested Image Agent runtime revision is unavailable.")
            return base
        selected = revision_id or current_revision_id(config_root)
        return cls.from_revision(config_root, selected, base=base, managed=managed)

    @classmethod
    def from_paths(
        cls,
        model_config_path: Path | None,
        runtime_policy_path: Path | None,
    ) -> "ManagedRuntime":
        if model_config_path is None or runtime_policy_path is None:
            raise RuntimeError(
                "Image Agent requires the read-only runtime files supplied by its owner."
            )
        if model_config_path.is_symlink() or runtime_policy_path.is_symlink():
            raise RuntimeError("The supplied Image Agent runtime files are unavailable or unsafe.")
        model_path = model_config_path.resolve()
        runtime_path = runtime_policy_path.resolve()
        if (
            model_path.is_symlink()
            or runtime_path.is_symlink()
            or not model_path.is_file()
            or not runtime_path.is_file()
        ):
            raise RuntimeError("The supplied Image Agent runtime files are unavailable or unsafe.")
        model_bytes = model_path.read_bytes()
        runtime_bytes = runtime_path.read_bytes()
        policy = RuntimePolicy.from_file(runtime_path)
        if policy.offline_mode:
            raise RuntimeError("Managed Image Agent runtime cannot enable offline mode.")
        try:
            document = yaml.safe_load(model_bytes)
        except yaml.YAMLError as exc:
            raise RuntimeError("The supplied model configuration is invalid.") from exc
        if not isinstance(document, dict):
            raise RuntimeError("The supplied model configuration must be a mapping.")
        router = ModelRouter.from_file(model_path)
        router.validate_required_bindings()
        router.validate_managed_bindings()
        runtime_sha = sha256_bytes(runtime_bytes)
        model_sha = sha256_bytes(model_bytes)
        return cls(
            model_config_path=model_path,
            runtime_policy_path=runtime_path,
            policy=policy,
            revision_id="cfg-inst-r000001",
            task_config_revision_id="task-config-r000001",
            runtime_config_sha256=runtime_sha,
            model_config_sha256=model_sha,
            config_hash=revision_content_hash(runtime_sha, model_sha),
            model_document=document,
            overrides={},
        )

    @classmethod
    def from_revision(
        cls,
        config_root: Path,
        revision_id: str,
        *,
        base: "ManagedRuntime",
        managed: bool,
    ) -> "ManagedRuntime":
        loaded: LoadedRuntimeRevision = load_revision(
            config_root,
            revision_id,
            base_policy=base.policy,
            managed=managed,
        )
        manifest = loaded.manifest
        router = ModelRouter.from_file(
            loaded.model_config_path,
            expected_sha256=manifest.model_config_sha256,
            config_hash=manifest.config_hash,
            revision_id=manifest.revision_id,
        )
        router.validate_required_bindings()
        if managed:
            router.validate_managed_bindings()
        return cls(
            model_config_path=loaded.model_config_path,
            runtime_policy_path=loaded.root / "runtime.yaml",
            policy=loaded.runtime_policy,
            revision_id=manifest.revision_id,
            task_config_revision_id=manifest.task_config_revision_id,
            runtime_config_sha256=manifest.runtime_sha256,
            model_config_sha256=manifest.model_config_sha256,
            config_hash=manifest.config_hash,
            model_document=loaded.model_document,
            overrides=dict(manifest.overrides),
            manifest=manifest.model_dump(mode="json"),
        )

    def with_policy(self, policy: RuntimePolicy) -> "ManagedRuntime":
        """Bind a legacy branch policy without following a mutable global file."""

        runtime_sha = sha256_bytes(canonical_yaml_bytes(effective_runtime(policy)))
        return replace(
            self,
            policy=policy,
            runtime_config_sha256=runtime_sha,
            config_hash=revision_content_hash(runtime_sha, self.model_config_sha256),
        )

    def branch_binding(
        self, *, effective_from_state: str | None = None
    ) -> dict[str, Any]:
        selected_state = effective_from_state
        if selected_state is None and self.manifest is not None:
            selected_state = self.manifest.get("effective_from_state")
        selected_state = str(selected_state or "initial")
        policy = self.policy.snapshot()
        return {
            "runtime_config_revision_id": self.revision_id,
            "task_config_revision_id": self.task_config_revision_id,
            "runtime_policy": policy,
            "runtime_policy_hash": hashlib.sha256(canonical_json_bytes(policy)).hexdigest(),
            "runtime_config_sha256": self.runtime_config_sha256,
            "model_config_hash": self.model_config_sha256,
            "config_hash": self.config_hash,
            "effective_from_state": selected_state,
        }

    def assert_branch_binding(self, binding: dict[str, Any]) -> None:
        expected = self.branch_binding(
            effective_from_state=str(binding.get("effective_from_state") or "initial")
        )
        for field in (
            "runtime_config_revision_id",
            "task_config_revision_id",
            "runtime_policy_hash",
            "runtime_config_sha256",
            "model_config_hash",
            "config_hash",
        ):
            actual = binding.get(field)
            if actual is not None and actual != expected[field]:
                raise RuntimeError(f"The active branch {field} failed integrity validation.")
        persisted_policy = binding.get("runtime_policy")
        if persisted_policy is not None and persisted_policy != expected["runtime_policy"]:
            raise RuntimeError("The active branch runtime policy does not match its revision.")
        recorded_state = None if self.manifest is None else self.manifest.get(
            "effective_from_state"
        )
        if recorded_state is not None and binding.get("effective_from_state") != recorded_state:
            raise RuntimeError(
                "The active branch effective state does not match its revision."
            )

    def safe_runtime(self) -> dict[str, Any]:
        return effective_runtime(self.policy)

    def safe_model_bindings(self) -> dict[str, str]:
        return model_bindings(self.model_document)


def optional_environment_path(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value) if value else None


def _environment_path(name: str) -> Path | None:
    return optional_environment_path(name)
