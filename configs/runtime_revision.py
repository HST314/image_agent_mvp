"""Immutable, credential-safe runtime configuration revisions."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.models import ModelConfig

from .runtime_policy import RuntimePolicy


REVISION_ID = re.compile(r"^cfg-inst-r[0-9]{6}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MODEL_STATES = (
    "intake_clarify",
    "confirmation_build",
    "initial_candidate_generation",
    "self_check_inspection",
    "self_check_rework",
    "human_prompt_rework",
)
RUNTIME_FIELDS = (
    "question_preference",
    "max_auto_questions",
    "clarification_total_budget",
    "candidate_concurrency",
    "default_output_size",
    "response_format",
    "watermark",
    "self_check",
)
SELF_CHECK_FIELDS = (
    "termination",
    "fixed_rounds",
    "max_rounds",
    "stop_early_on_pass",
)
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:api_key|access_token|authorization|base_url|cookie|credential|"
    r"endpoint|password|private_key|secret)(?:$|_)",
    re.IGNORECASE,
)
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_TRAVERSAL = re.compile(r"(?:^|[/\\])\.\.(?:[/\\]|$)")
_CREDENTIAL_VALUE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bgh[pousr]_[A-Za-z0-9]{8,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----)",
    re.IGNORECASE,
)
_MAX_CONFIG_BYTES = 2 * 1024 * 1024


class RuntimeRevisionError(ValueError):
    """Stable configuration error that may cross the HTTP boundary."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RevisionActor(_StrictModel):
    type: Literal["member", "agent", "system"]
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class EffectiveSelfCheck(_StrictModel):
    termination: Literal["fix", "solo"]
    fixed_rounds: int = Field(ge=1, le=20)
    max_rounds: int = Field(ge=1, le=50)
    stop_early_on_pass: bool

    @model_validator(mode="after")
    def validate_rounds(self) -> "EffectiveSelfCheck":
        if self.fixed_rounds > self.max_rounds:
            raise ValueError("fixed_rounds cannot exceed max_rounds")
        return self


class EffectiveRuntime(_StrictModel):
    question_preference: Literal["proactive", "blocking_only"]
    max_auto_questions: int = Field(ge=0, le=10)
    clarification_total_budget: int = Field(ge=0, le=100)
    candidate_concurrency: int = Field(ge=1, le=5)
    default_output_size: str = Field(pattern=r"^(?:[1-9][0-9]{1,4}x[1-9][0-9]{1,4}|[124]K)$")
    response_format: Literal["url", "b64_json"]
    watermark: bool
    self_check: EffectiveSelfCheck


class RuntimeRevisionManifest(_StrictModel):
    schema_version: Literal["2.0"]
    task_id: str = Field(min_length=1, max_length=128)
    instance_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(pattern=r"^cfg-inst-r[0-9]{6}$")
    parent_revision_id: str | None = Field(default=None, pattern=r"^cfg-inst-r[0-9]{6}$")
    task_config_revision_id: str = Field(pattern=r"^task-config-r[0-9]{6}$")
    overrides: dict[str, Any]
    effective_runtime: EffectiveRuntime
    model_bindings: dict[str, str]
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: RevisionActor
    created_at: str = Field(min_length=1, max_length=64)
    confirmed_at: str | None = Field(default=None, min_length=1, max_length=64)
    apply_mode: Literal["before_start", "safe_checkpoint_branch"]
    apply_status: Literal[
        "DRAFT", "CONFIRMED", "WAITING_SAFE_POINT", "APPLYING", "APPLIED", "FAILED"
    ]
    branch_id: str | None = Field(default=None, min_length=1, max_length=128)
    checkpoint_id: str | None = Field(default=None, pattern=r"^checkpoint_[0-9a-f]{24}$")
    effective_from_state: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
    )

    @model_validator(mode="after")
    def validate_cross_fields(self) -> "RuntimeRevisionManifest":
        if self.parent_revision_id == self.revision_id:
            raise ValueError("a runtime revision cannot be its own parent")
        if set(self.model_bindings) != set(MODEL_STATES):
            raise ValueError("model_bindings must contain exactly the supported workflow states")
        if any(not isinstance(value, str) or not value for value in self.model_bindings.values()):
            raise ValueError("model bindings cannot be empty")
        validate_overrides(self.overrides)
        if self.apply_status == "DRAFT" and self.confirmed_at is not None:
            raise ValueError("draft revisions cannot be confirmed")
        if self.apply_status != "DRAFT" and self.confirmed_at is None:
            raise ValueError("non-draft revisions require confirmed_at")
        if self.apply_mode == "before_start" and (
            self.branch_id is not None or self.checkpoint_id is not None
        ):
            raise ValueError("before-start revisions cannot reference a branch checkpoint")
        if self.apply_mode == "safe_checkpoint_branch" and self.apply_status == "APPLIED":
            if self.branch_id is None or self.checkpoint_id is None or self.effective_from_state is None:
                raise ValueError("applied branch revisions require branch/checkpoint/state references")
        return self


@dataclass(frozen=True, slots=True)
class LoadedRuntimeRevision:
    root: Path
    manifest: RuntimeRevisionManifest
    runtime_document: dict[str, Any]
    model_document: dict[str, Any]
    runtime_policy: RuntimePolicy
    model_config_path: Path


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_yaml_bytes(value: Any) -> bytes:
    return yaml.safe_dump(
        value, allow_unicode=True, default_flow_style=False, sort_keys=True
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def revision_content_hash(runtime_sha256: str, model_config_sha256: str) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "runtime_sha256": runtime_sha256,
                "model_config_sha256": model_config_sha256,
            }
        )
    )


def effective_runtime(policy: RuntimePolicy) -> dict[str, Any]:
    snapshot = policy.snapshot()
    return {
        **{field: snapshot[field] for field in RUNTIME_FIELDS if field != "self_check"},
        "self_check": {
            field: snapshot["self_check"][field] for field in SELF_CHECK_FIELDS
        },
    }


def model_bindings(document: dict[str, Any]) -> dict[str, str]:
    config = ModelConfig.model_validate(document)
    bindings = {
        binding.state: binding.model
        for binding in config.state_bindings
        if binding.state in MODEL_STATES
    }
    if set(bindings) != set(MODEL_STATES):
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED",
            "The runtime revision does not define every required model binding.",
        )
    return bindings


def validate_overrides(value: dict[str, Any]) -> None:
    allowed = {*RUNTIME_FIELDS, "advanced_model_overrides"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"runtime overrides contain unsupported fields: {sorted(unknown)}")
    self_check = value.get("self_check")
    if self_check is not None and (
        not isinstance(self_check, dict) or set(self_check) - set(SELF_CHECK_FIELDS)
    ):
        raise ValueError("self_check overrides contain unsupported fields")
    advanced = value.get("advanced_model_overrides")
    if advanced is not None and (
        not isinstance(advanced, dict) or set(advanced) - set(MODEL_STATES)
    ):
        raise ValueError("advanced model overrides contain unsupported states")
    scalar_rules: dict[str, Any] = {
        "question_preference": lambda item: item in {"proactive", "blocking_only"},
        "max_auto_questions": lambda item: type(item) is int and 0 <= item <= 10,
        "clarification_total_budget": lambda item: type(item) is int and 0 <= item <= 100,
        "candidate_concurrency": lambda item: type(item) is int and 1 <= item <= 5,
        "default_output_size": lambda item: isinstance(item, str)
        and re.fullmatch(r"(?:[1-9][0-9]{1,4}x[1-9][0-9]{1,4}|[124]K)", item),
        "response_format": lambda item: item in {"url", "b64_json"},
        "watermark": lambda item: type(item) is bool,
    }
    for field, rule in scalar_rules.items():
        if field in value and not rule(value[field]):
            raise ValueError(f"runtime override '{field}' is invalid")
    if isinstance(self_check, dict):
        validators = {
            "termination": lambda item: item in {"fix", "solo"},
            "fixed_rounds": lambda item: type(item) is int and 1 <= item <= 20,
            "max_rounds": lambda item: type(item) is int and 1 <= item <= 50,
            "stop_early_on_pass": lambda item: type(item) is bool,
        }
        if any(not validators[field](item) for field, item in self_check.items()):
            raise ValueError("self_check overrides are invalid")
        if (
            "fixed_rounds" in self_check
            and "max_rounds" in self_check
            and self_check["fixed_rounds"] > self_check["max_rounds"]
        ):
            raise ValueError("fixed_rounds cannot exceed max_rounds")
    if isinstance(advanced, dict) and any(
        not isinstance(item, str) or not 1 <= len(item) <= 256
        for item in advanced.values()
    ):
        raise ValueError("advanced model overrides are invalid")


def validate_public_tree(value: Any, *, path: str = "$", depth: int = 0) -> None:
    if depth > 32:
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The runtime revision exceeds the nesting limit."
        )
    if isinstance(value, dict):
        if len(value) > 512:
            raise RuntimeRevisionError(
                "CONFIG_INTEGRITY_FAILED", "The runtime revision exceeds the item limit."
            )
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeRevisionError(
                    "CONFIG_INTEGRITY_FAILED", "Runtime revision keys must be strings."
                )
            lowered = str(key).casefold()
            if _SENSITIVE_KEY.search(lowered):
                raise RuntimeRevisionError(
                    "CONFIG_INTEGRITY_FAILED",
                    f"The runtime revision contains a private field at {path}.",
                )
            validate_public_tree(item, path=f"{path}.{key}", depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 512:
            raise RuntimeRevisionError(
                "CONFIG_INTEGRITY_FAILED", "The runtime revision exceeds the item limit."
            )
        for index, item in enumerate(value):
            validate_public_tree(item, path=f"{path}[{index}]", depth=depth + 1)
    elif isinstance(value, str) and (
        _URL.match(value)
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:[\\/]", value)
        or _TRAVERSAL.search(value)
        or _CREDENTIAL_VALUE.search(value)
    ):
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED",
            f"The runtime revision contains an absolute path at {path}.",
        )
    elif value is None or isinstance(value, bool | int | str):
        return
    elif isinstance(value, float) and math.isfinite(value):
        return
    else:
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED",
            f"The runtime revision contains an unsupported value at {path}.",
        )


def merge_runtime_policy(
    base_policy: RuntimePolicy,
    runtime_document: dict[str, Any],
    *,
    managed: bool,
) -> RuntimePolicy:
    if set(runtime_document) != set(RUNTIME_FIELDS):
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED",
            "The runtime revision must contain exactly the editable effective runtime fields.",
        )
    safe = EffectiveRuntime.model_validate(runtime_document).model_dump(mode="json")
    payload = base_policy.snapshot()
    payload.update({key: value for key, value in safe.items() if key != "self_check"})
    self_check = dict(payload["self_check"])
    self_check.update(safe["self_check"])
    if managed:
        self_check["release"] = "manual"
    payload["self_check"] = self_check
    payload["offline_mode"] = False if managed else payload["offline_mode"]
    return RuntimePolicy.model_validate(payload)


def _regular_bytes(path: Path, *, root: Path) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise RuntimeRevisionError(
                "CONFIG_INTEGRITY_FAILED", "A runtime revision file is missing or unsafe."
            )
        resolved = path.resolve(strict=True)
        if root.resolve(strict=True) not in resolved.parents:
            raise RuntimeRevisionError(
                "CONFIG_INTEGRITY_FAILED", "A runtime revision path escaped its registered root."
            )
        if resolved.stat().st_size > _MAX_CONFIG_BYTES:
            raise RuntimeRevisionError(
                "CONFIG_INTEGRITY_FAILED", "A runtime revision file exceeds the size limit."
            )
        return resolved.read_bytes()
    except RuntimeRevisionError:
        raise
    except OSError as exc:
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "A runtime revision file could not be read."
        ) from exc


def current_revision_id(config_root: Path) -> str:
    if config_root.is_symlink():
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The runtime configuration state root is unsafe."
        )
    root = config_root.resolve(strict=True)
    state_bytes = _regular_bytes(root / "state.json", root=root)
    try:
        state = json.loads(state_bytes)
    except json.JSONDecodeError as exc:
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The runtime configuration state is invalid."
        ) from exc
    revision_id = state.get("current_revision_id") if isinstance(state, dict) else None
    if not isinstance(revision_id, str) or REVISION_ID.fullmatch(revision_id) is None:
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The runtime configuration state has an invalid pointer."
        )
    return revision_id


def load_revision(
    config_root: Path,
    revision_id: str,
    *,
    base_policy: RuntimePolicy,
    managed: bool,
) -> LoadedRuntimeRevision:
    if REVISION_ID.fullmatch(revision_id) is None:
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The runtime configuration revision ID is invalid."
        )
    if config_root.is_symlink():
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The registered runtime configuration root is unsafe."
        )
    try:
        root = config_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The registered runtime configuration root is unavailable."
        ) from exc
    if root.is_symlink() or not root.is_dir():
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The registered runtime configuration root is unsafe."
        )
    revisions_root = root / "revisions"
    revision_root = revisions_root / revision_id
    if revisions_root.is_symlink() or revision_root.is_symlink() or not revision_root.is_dir():
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The requested runtime configuration revision is not registered."
        )
    resolved_revision = revision_root.resolve(strict=True)
    if resolved_revision.parent != revisions_root.resolve(strict=True):
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The runtime configuration revision escaped its root."
        )
    manifest_bytes = _regular_bytes(resolved_revision / "manifest.json", root=resolved_revision)
    runtime_bytes = _regular_bytes(resolved_revision / "runtime.yaml", root=resolved_revision)
    model_bytes = _regular_bytes(resolved_revision / "model_config.yaml", root=resolved_revision)
    try:
        manifest_payload = json.loads(manifest_bytes)
        runtime_document = yaml.safe_load(runtime_bytes)
        model_document = yaml.safe_load(model_bytes)
        manifest = RuntimeRevisionManifest.model_validate(manifest_payload)
    except (json.JSONDecodeError, yaml.YAMLError, ValueError) as exc:
        if isinstance(exc, RuntimeRevisionError):
            raise
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The runtime configuration revision is malformed."
        ) from exc
    if not isinstance(runtime_document, dict) or not isinstance(model_document, dict):
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "Runtime revision files must contain mappings."
        )
    if manifest.revision_id != revision_id:
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The runtime revision identity is inconsistent."
        )
    runtime_sha = sha256_bytes(runtime_bytes)
    model_sha = sha256_bytes(model_bytes)
    if (
        manifest.runtime_sha256 != runtime_sha
        or manifest.model_config_sha256 != model_sha
        or manifest.config_hash != revision_content_hash(runtime_sha, model_sha)
    ):
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The runtime configuration revision failed hash validation."
        )
    validate_public_tree(manifest_payload)
    validate_public_tree(runtime_document)
    validate_public_tree(model_document)
    policy = merge_runtime_policy(base_policy, runtime_document, managed=managed)
    if effective_runtime(policy) != manifest.effective_runtime.model_dump(mode="json"):
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The runtime revision manifest does not match runtime.yaml."
        )
    if model_bindings(model_document) != manifest.model_bindings:
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The runtime revision manifest does not match model_config.yaml."
        )
    return LoadedRuntimeRevision(
        root=resolved_revision,
        manifest=manifest,
        runtime_document=runtime_document,
        model_document=model_document,
        runtime_policy=policy,
        model_config_path=resolved_revision / "model_config.yaml",
    )


def publish_revision(
    config_root: Path,
    manifest: dict[str, Any],
    runtime_document: dict[str, Any],
    model_document: dict[str, Any],
) -> Path:
    """Publish one complete revision directory without replacing an existing ID."""

    parsed = RuntimeRevisionManifest.model_validate(manifest)
    validate_public_tree(manifest)
    validate_public_tree(runtime_document)
    validate_public_tree(model_document)
    runtime_bytes = canonical_yaml_bytes(runtime_document)
    model_bytes = canonical_yaml_bytes(model_document)
    if (
        parsed.runtime_sha256 != sha256_bytes(runtime_bytes)
        or parsed.model_config_sha256 != sha256_bytes(model_bytes)
        or parsed.config_hash
        != revision_content_hash(parsed.runtime_sha256, parsed.model_config_sha256)
    ):
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The runtime revision payload does not match its manifest."
        )
    files = {
        "manifest.json": canonical_json_bytes(manifest) + b"\n",
        "runtime.yaml": runtime_bytes,
        "model_config.yaml": model_bytes,
    }
    if config_root.is_symlink():
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The project runtime configuration root is unsafe."
        )
    root = config_root.resolve()
    revisions = root / "revisions"
    revisions.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or revisions.is_symlink():
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED", "The project runtime configuration root is unsafe."
        )
    target = revisions / parsed.revision_id
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise RuntimeRevisionError(
                "SETTINGS_REVISION_CONFLICT", "The immutable revision ID is unsafe."
            )
        if any(
            (target / name).is_symlink()
            or not (target / name).is_file()
            or (target / name).read_bytes() != content
            for name, content in files.items()
        ):
            raise RuntimeRevisionError(
                "SETTINGS_REVISION_CONFLICT", "The immutable revision ID was reused."
            )
        return target
    temporary = revisions / f".{parsed.revision_id}.{uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)
    try:
        for name, content in files.items():
            path = temporary / name
            with path.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(path, 0o440)
        os.replace(temporary, target)
        os.chmod(target, 0o500)
        if os.name != "nt":
            descriptor = os.open(revisions, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return target
