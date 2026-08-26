"""Typed task-setting commands and revision materialization helpers."""

from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_core.models import ModelConfig
from model_router.router import PROVIDER_KEY_ENV, REQUIRED_STATE_ROLES

from .runtime_policy import RuntimePolicy
from .runtime_revision import (
    LIBRARY_RELEASE_FIELDS,
    RUNTIME_FIELDS,
    RuntimeRevisionError,
    RuntimeRevisionManifest,
    canonical_json_bytes,
    canonical_yaml_bytes,
    effective_runtime,
    model_bindings,
    revision_content_hash,
    sha256_bytes,
)


class StrictSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SelfCheckSettingsPatch(StrictSettingsRequest):
    termination: Literal["fix", "solo"] | None = None
    fixed_rounds: int | None = Field(default=None, ge=1, le=20)
    max_rounds: int | None = Field(default=None, ge=1, le=50)
    stop_early_on_pass: bool | None = None


class LibraryReleaseSettingsPatch(StrictSettingsRequest):
    release: Literal["auto", "manual", "off"] | None = None


class AdvancedModelSettingsPatch(StrictSettingsRequest):
    intake_clarify: str | None = Field(default=None, min_length=1, max_length=256)
    confirmation_build: str | None = Field(default=None, min_length=1, max_length=256)
    initial_candidate_generation: str | None = Field(default=None, min_length=1, max_length=256)
    self_check_inspection: str | None = Field(default=None, min_length=1, max_length=256)
    self_check_rework: str | None = Field(default=None, min_length=1, max_length=256)
    human_prompt_rework: str | None = Field(default=None, min_length=1, max_length=256)


class RuntimeSettingsPatch(StrictSettingsRequest):
    question_preference: Literal["proactive", "blocking_only"] | None = None
    max_auto_questions: int | None = Field(default=None, ge=0, le=10)
    clarification_total_budget: int | None = Field(default=None, ge=0, le=100)
    category_constraint: LibraryReleaseSettingsPatch | None = None
    style_direction: LibraryReleaseSettingsPatch | None = None
    candidate_concurrency: int | None = Field(default=None, ge=1, le=5)
    default_output_size: str | None = Field(
        default=None, pattern=r"^(?:[1-9][0-9]{1,4}x[1-9][0-9]{1,4}|[124]K)$"
    )
    response_format: Literal["url", "b64_json"] | None = None
    watermark: bool | None = None
    self_check: SelfCheckSettingsPatch | None = None
    advanced_model_overrides: AdvancedModelSettingsPatch | None = None


class StandaloneRuntimeSettingsRequest(StrictSettingsRequest):
    base_revision_id: str = Field(pattern=r"^cfg-inst-r[0-9]{6}$")
    overrides: RuntimeSettingsPatch
    actor: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    confirmed: bool
    idempotency_key: str = Field(
        min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )


class ManagedConfigRevisionApplyRequest(StrictSettingsRequest):
    runtime_config_revision_id: str = Field(pattern=r"^cfg-inst-r[0-9]{6}$")
    from_checkpoint: str = Field(pattern=r"^checkpoint_[0-9a-f]{24}$")
    expected_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_project_revision_id: str = Field(pattern=r"^cfg-inst-r[0-9]{6}$")
    expected_project_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_from_state: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    idempotency_key: str = Field(
        min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )


def build_runtime_revision(
    *,
    project_id: str,
    revision_id: str,
    parent_revision_id: str | None,
    task_config_revision_id: str,
    overrides: dict[str, Any],
    policy: RuntimePolicy,
    model_document: dict[str, Any],
    actor_type: Literal["member", "agent", "system"],
    actor_id: str,
    apply_mode: Literal["before_start", "safe_checkpoint_branch"],
    branch_id: str | None,
    checkpoint_id: str | None,
    effective_from_state: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    runtime_document = effective_runtime(policy)
    model_document = deepcopy(model_document)
    runtime_sha = sha256_bytes(canonical_yaml_bytes(runtime_document))
    model_sha = sha256_bytes(canonical_yaml_bytes(model_document))
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = {
        "schema_version": "2.0",
        "task_id": project_id,
        "instance_id": project_id,
        "revision_id": revision_id,
        "parent_revision_id": parent_revision_id,
        "task_config_revision_id": task_config_revision_id,
        "overrides": deepcopy(overrides),
        "effective_runtime": runtime_document,
        "model_bindings": model_bindings(model_document),
        "runtime_sha256": runtime_sha,
        "model_config_sha256": model_sha,
        "config_hash": revision_content_hash(runtime_sha, model_sha),
        "created_by": {"type": actor_type, "id": actor_id},
        "created_at": created_at,
        "confirmed_at": created_at,
        "apply_mode": apply_mode,
        "apply_status": "APPLIED",
        "branch_id": branch_id,
        "checkpoint_id": checkpoint_id,
        "effective_from_state": effective_from_state,
    }
    RuntimeRevisionManifest.model_validate(manifest)
    policy_snapshot = policy.snapshot()
    binding = {
        "runtime_config_revision_id": revision_id,
        "task_config_revision_id": task_config_revision_id,
        "runtime_policy": policy_snapshot,
        "runtime_policy_hash": sha256_bytes(canonical_json_bytes(policy_snapshot)),
        "runtime_config_sha256": runtime_sha,
        "model_config_hash": model_sha,
        "config_hash": manifest["config_hash"],
        "effective_from_state": effective_from_state,
    }
    return manifest, runtime_document, model_document, binding


def merge_settings_overrides(
    current: dict[str, Any], patch: RuntimeSettingsPatch
) -> dict[str, Any]:
    merged = deepcopy(current)
    changes = patch.model_dump(mode="json", exclude_unset=True)
    for field, value in changes.items():
        if field in {*LIBRARY_RELEASE_FIELDS, "self_check", "advanced_model_overrides"}:
            if value is None:
                merged.pop(field, None)
                continue
            nested = dict(merged.get(field) or {})
            for nested_field, nested_value in value.items():
                if nested_value is None:
                    nested.pop(nested_field, None)
                else:
                    nested[nested_field] = nested_value
            if nested:
                merged[field] = nested
            else:
                merged.pop(field, None)
            continue
        if value is None:
            merged.pop(field, None)
        else:
            merged[field] = value
    return merged


def policy_with_overrides(
    base: RuntimePolicy, overrides: dict[str, Any]
) -> RuntimePolicy:
    payload = base.snapshot()
    for field in RUNTIME_FIELDS:
        if field in {*LIBRARY_RELEASE_FIELDS, "self_check"}:
            nested = dict(payload[field])
            nested.update(overrides.get(field) or {})
            payload[field] = nested
        elif field in overrides:
            payload[field] = overrides[field]
    return RuntimePolicy.model_validate(payload)


def model_document_with_overrides(
    base_document: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    config = ModelConfig.model_validate(base_document)
    requested = overrides.get("advanced_model_overrides") or {}
    if not requested:
        return config.model_dump(mode="json")
    candidates = list(config.state_bindings)
    updated = []
    for binding in config.state_bindings:
        model_name = requested.get(binding.state)
        if model_name is None:
            updated.append(binding)
            continue
        required_role = REQUIRED_STATE_ROLES.get(binding.state)
        selected = next(
            (
                candidate
                for candidate in candidates
                if candidate.model == model_name and candidate.model_role == required_role
            ),
            None,
        )
        if selected is None:
            raise RuntimeRevisionError(
                "MODEL_NOT_APPROVED",
                f"Model '{model_name}' is not approved for state '{binding.state}'.",
            )
        key_env = PROVIDER_KEY_ENV.get(selected.provider)
        if key_env and not os.getenv(key_env):
            raise RuntimeRevisionError(
                "MODEL_PROVIDER_NOT_AUTHORIZED",
                f"The selected model Provider is not authorized for state '{binding.state}'.",
            )
        updated.append(
            binding.model_copy(
                update={
                    "provider": selected.provider,
                    "model": selected.model,
                    "parameters": dict(selected.parameters),
                    "fallback_model": selected.fallback_model,
                }
            )
        )
    return config.model_copy(update={"state_bindings": updated}).model_dump(mode="json")


def config_branch_name(revision_id: str, idempotency_key: str) -> str:
    suffix = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:8]
    return f"config-{revision_id.rsplit('r', 1)[1]}-{suffix}"


def apply_receipt(
    replay: dict[str, Any], *, status_value: str | None = None
) -> dict[str, Any]:
    return {
        "status": status_value or replay.get("status") or "APPLIED_ON_BRANCH",
        "runtime_config_revision_id": replay["runtime_config_revision_id"],
        "branch_id": replay["branch_id"],
        "checkpoint_id": replay["checkpoint_id"],
        "from_checkpoint": replay["from_checkpoint"],
        "effective_from_state": replay["effective_from_state"],
        "runtime_policy_hash": replay["runtime_policy_hash"],
        "runtime_config_sha256": replay["runtime_config_sha256"],
        "model_config_hash": replay["model_config_hash"],
        "config_hash": replay["config_hash"],
    }
