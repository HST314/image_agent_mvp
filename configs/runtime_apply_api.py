"""Managed API for applying a registered runtime revision on a new branch."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, status

from storage.project_store import ProjectStore, content_hash

from .runtime_revision import RuntimeRevisionError
from .runtime_settings import (
    ManagedConfigRevisionApplyRequest,
    apply_receipt,
    config_branch_name,
)


@dataclass(frozen=True, slots=True)
class RuntimeApplyDependencies:
    existing_store: Callable[[str], ProjectStore]
    managed_request_allowed: Callable[[str, Request], bool]
    managed_runtime: Callable[[str, ProjectStore], Any]
    project_runtime: Callable[[ProjectStore], Any]
    require_safe_checkpoint: Callable[[ProjectStore, str], dict[str, Any]]
    translate_error: Callable[[Exception], HTTPException]


def create_runtime_apply_router(dependencies: RuntimeApplyDependencies) -> APIRouter:
    router = APIRouter()

    @router.post("/api/managed/projects/{project_id}/config-revisions/apply")
    async def apply_managed_config_revision(
        project_id: str,
        body: ManagedConfigRevisionApplyRequest,
        request: Request,
    ) -> dict[str, Any]:
        if not dependencies.managed_request_allowed(project_id, request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "MANAGED_BY_HARNESS",
                    "message": "This endpoint only accepts the local owning Adapter.",
                },
            )
        try:
            return await asyncio.to_thread(
                _apply_managed_revision, dependencies, project_id, body
            )
        except Exception as exc:
            raise dependencies.translate_error(exc) from exc

    return router


def _checked_replay(
    store: ProjectStore, idempotency_key: str, request_hash: str
) -> dict[str, Any] | None:
    replay = store.find_config_apply(idempotency_key)
    if replay is not None and replay["request_hash"] != request_hash:
        raise RuntimeRevisionError(
            "IDEMPOTENCY_CONFLICT",
            "The idempotency key was reused for a different configuration apply request.",
        )
    return replay


def _apply_managed_revision(
    dependencies: RuntimeApplyDependencies,
    project_id: str,
    body: ManagedConfigRevisionApplyRequest,
) -> dict[str, Any]:
    store = dependencies.existing_store(project_id)
    store.recover_pending_transaction()
    request_hash = content_hash(
        body.model_dump(mode="json", exclude={"idempotency_key"})
    )
    replay = _checked_replay(store, body.idempotency_key, request_hash)
    if replay is not None:
        return apply_receipt(replay)
    runtime = dependencies.managed_runtime(body.runtime_config_revision_id, store)
    manifest = runtime.manifest
    if manifest is None:
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED",
            "Managed configuration apply requires a registered v2 revision manifest.",
        )
    if manifest["instance_id"] != project_id:
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED",
            "The runtime configuration revision belongs to another instance.",
        )
    if manifest["apply_mode"] != "safe_checkpoint_branch":
        raise RuntimeRevisionError(
            "INVALID_STATE_TRANSITION",
            "A running project requires a safe-checkpoint branch revision.",
        )
    if manifest["apply_status"] in {"DRAFT", "FAILED"}:
        raise RuntimeRevisionError(
            "INVALID_STATE_TRANSITION",
            "The runtime configuration revision is not confirmed for application.",
        )
    if runtime.config_hash != body.expected_config_hash:
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED",
            "The runtime configuration hash does not match the apply command.",
        )
    recorded_state = manifest.get("effective_from_state")
    if recorded_state is not None and recorded_state != body.effective_from_state:
        raise RuntimeRevisionError(
            "CONFIG_INTEGRITY_FAILED",
            "The apply command changed the revision's effective workflow state.",
        )
    with store.lock():
        replay = _checked_replay(store, body.idempotency_key, request_hash)
        if replay is not None:
            return apply_receipt(replay)
        dependencies.require_safe_checkpoint(store, body.from_checkpoint)
        current = dependencies.project_runtime(store)
        if (
            current.revision_id != body.expected_project_revision_id
            or current.config_hash != body.expected_project_config_hash
        ):
            raise RuntimeRevisionError(
                "SETTINGS_REVISION_CONFLICT",
                "The active project configuration changed after preview.",
            )
        branch_id = config_branch_name(
            body.runtime_config_revision_id, body.idempotency_key
        )
        if manifest.get("branch_id") not in {None, branch_id}:
            raise RuntimeRevisionError(
                "CONFIG_INTEGRITY_FAILED",
                "The runtime revision records a different configuration branch.",
            )
        if manifest.get("checkpoint_id") not in {None, body.from_checkpoint}:
            raise RuntimeRevisionError(
                "CONFIG_INTEGRITY_FAILED",
                "The runtime revision records a different source checkpoint.",
            )
        store.ensure_active_config_binding(current.branch_binding())
        store.branch_from(
            body.from_checkpoint,
            name=branch_id,
            mode="fork_after",
            config_binding=runtime.branch_binding(
                effective_from_state=body.effective_from_state
            ),
            config_apply_idempotency_key=body.idempotency_key,
            config_apply_request_hash=request_hash,
        )
        applied = store.find_config_apply(body.idempotency_key)
        if applied is None:
            raise RuntimeRevisionError(
                "CONFIG_INTEGRITY_FAILED",
                "The applied configuration branch could not be resolved.",
            )
        store.events.append(
            "config_revision_applied",
            runtime_config_revision_id=runtime.revision_id,
            branch_id=applied["branch_id"],
            checkpoint_id=applied["checkpoint_id"],
            config_hash=runtime.config_hash,
        )
        return apply_receipt(applied)
