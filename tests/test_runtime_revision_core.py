"""Immutable runtime revision and model-call binding regressions."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from configs.managed_runtime import ManagedRuntime
from configs.runtime_revision import (
    RuntimeRevisionError,
    RuntimeRevisionManifest,
    canonical_yaml_bytes,
    effective_runtime,
    model_bindings,
    publish_revision,
    revision_content_hash,
    sha256_bytes,
)
from model_router.router import ModelRouter


MODEL_CONFIG = Path(__file__).resolve().parent / "fixtures" / "model_config.yaml"
RUNTIME_POLICY = Path(__file__).resolve().parent / "fixtures" / "runtime.yaml"


def _base_runtime() -> ManagedRuntime:
    return ManagedRuntime.from_paths(MODEL_CONFIG, RUNTIME_POLICY)


def _bundle(
    project_id: str,
    revision_id: str,
    *,
    policy,
    model_document: dict,
) -> tuple[dict, dict, dict]:
    runtime_document = effective_runtime(policy)
    runtime_sha = sha256_bytes(canonical_yaml_bytes(runtime_document))
    model_sha = sha256_bytes(canonical_yaml_bytes(model_document))
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = {
        "schema_version": "2.0",
        "task_id": project_id,
        "instance_id": project_id,
        "revision_id": revision_id,
        "parent_revision_id": "cfg-inst-r000001",
        "task_config_revision_id": "task-config-r000001",
        "overrides": {"candidate_concurrency": policy.candidate_concurrency},
        "effective_runtime": runtime_document,
        "model_bindings": model_bindings(model_document),
        "runtime_sha256": runtime_sha,
        "model_config_sha256": model_sha,
        "config_hash": revision_content_hash(runtime_sha, model_sha),
        "created_by": {"type": "system", "id": "runtime_test"},
        "created_at": created_at,
        "confirmed_at": created_at,
        "apply_mode": "safe_checkpoint_branch",
        "apply_status": "APPLIED",
        "branch_id": "config-000002-testhash",
        "checkpoint_id": "checkpoint_0123456789abcdef01234567",
        "effective_from_state": "confirmation_build",
    }
    return manifest, runtime_document, model_document


def test_registered_revision_is_hash_locked_across_router_reload(
    tmp_path: Path,
) -> None:
    base = _base_runtime()
    policy = base.policy.model_copy(update={"candidate_concurrency": 3})
    bundle = _bundle(
        "revision-project",
        "cfg-inst-r000002",
        policy=policy,
        model_document=base.model_document,
    )
    config_root = tmp_path / "runtime-config"
    publish_revision(config_root, *bundle)

    loaded = ManagedRuntime.from_revision(
        config_root,
        "cfg-inst-r000002",
        base=base,
        managed=False,
    )
    assert loaded.policy.candidate_concurrency == 3
    router = ModelRouter.from_file(
        loaded.model_config_path,
        expected_sha256=loaded.model_config_sha256,
        config_hash=loaded.config_hash,
        revision_id=loaded.revision_id,
        branch_id="config-000002-testhash",
    )
    assert router.reload_at_boundary().revision_id == "cfg-inst-r000002"

    os.chmod(loaded.model_config_path, 0o640)
    loaded.model_config_path.write_text(
        loaded.model_config_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="immutable revision"):
        router.reload_at_boundary()


@pytest.mark.parametrize("actor_type", ["human", "master", "system", "adapter"])
def test_managed_revision_accepts_harness_audit_actor_types(actor_type: str) -> None:
    base = _base_runtime()
    manifest, _, _ = _bundle(
        "managed-actor-project",
        "cfg-inst-r000002",
        policy=base.policy,
        model_document=base.model_document,
    )
    manifest["created_by"] = {"type": actor_type, "id": "harness_operator"}

    validated = RuntimeRevisionManifest.model_validate(manifest)

    assert validated.created_by.type == actor_type


def test_legacy_v2_revision_without_library_release_fields_remains_loadable(
    tmp_path: Path,
) -> None:
    base = _base_runtime()
    manifest, runtime_document, model_document = _bundle(
        "legacy-revision-project",
        "cfg-inst-r000002",
        policy=base.policy.model_copy(update={"candidate_concurrency": 3}),
        model_document=base.model_document,
    )
    for field in ("category_constraint", "style_direction"):
        runtime_document.pop(field)
    runtime_sha = sha256_bytes(canonical_yaml_bytes(runtime_document))
    manifest["runtime_sha256"] = runtime_sha
    manifest["config_hash"] = revision_content_hash(
        runtime_sha, manifest["model_config_sha256"]
    )
    root = tmp_path / "runtime-config"
    publish_revision(root, manifest, runtime_document, model_document)

    loaded = ManagedRuntime.from_revision(
        root,
        "cfg-inst-r000002",
        base=base,
        managed=False,
    )

    assert loaded.policy.candidate_concurrency == 3
    assert loaded.policy.category_constraint.release == "off"
    assert loaded.policy.style_direction.release == "off"


def test_revision_loader_rejects_symlinked_registered_file(tmp_path: Path) -> None:
    base = _base_runtime()
    bundle = _bundle(
        "symlink-project",
        "cfg-inst-r000002",
        policy=base.policy,
        model_document=base.model_document,
    )
    root = tmp_path / "runtime-config"
    target = publish_revision(root, *bundle)
    model_path = target / "model_config.yaml"
    os.chmod(target, 0o700)
    os.chmod(model_path, 0o640)
    model_path.unlink()
    model_path.symlink_to(MODEL_CONFIG)
    with pytest.raises(RuntimeRevisionError) as error:
        ManagedRuntime.from_revision(
            root, "cfg-inst-r000002", base=base, managed=False
        )
    assert error.value.code == "CONFIG_INTEGRITY_FAILED"
