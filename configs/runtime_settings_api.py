"""Standalone project settings API backed by immutable revision branches."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, status

from storage.project_store import ProjectStore, content_hash

from .runtime_revision import RuntimeRevisionError, publish_revision
from .runtime_settings import (
    StandaloneRuntimeSettingsRequest,
    apply_receipt,
    build_runtime_revision,
    config_branch_name,
    merge_settings_overrides,
    model_document_with_overrides,
    policy_with_overrides,
)


@dataclass(frozen=True, slots=True)
class RuntimeSettingsDependencies:
    existing_store: Callable[[str], ProjectStore]
    managed_mode: Callable[[], bool]
    project_runtime: Callable[[ProjectStore], Any]
    project_baseline_runtime: Callable[[ProjectStore], Any]
    next_project_revision_id: Callable[[ProjectStore, str], str]
    runtime_settings_view: Callable[[ProjectStore], dict[str, Any]]
    workflow_boundary: Callable[[ProjectStore], dict[str, Any]]
    require_safe_checkpoint: Callable[[ProjectStore, str], dict[str, Any]]
    translate_error: Callable[[Exception], HTTPException]


def create_runtime_settings_router(
    dependencies: RuntimeSettingsDependencies,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects/{project_id}/runtime-settings")
    async def get_project_runtime_settings(project_id: str) -> dict[str, Any]:
        _reject_managed_mode(dependencies)
        try:
            return await asyncio.to_thread(
                dependencies.runtime_settings_view,
                dependencies.existing_store(project_id),
            )
        except Exception as exc:
            raise dependencies.translate_error(exc) from exc

    @router.post("/api/projects/{project_id}/runtime-settings")
    async def revise_project_runtime_settings(
        project_id: str, body: StandaloneRuntimeSettingsRequest
    ) -> dict[str, Any]:
        _reject_managed_mode(dependencies)
        if not body.confirmed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "INVALID_STATE_TRANSITION",
                    "message": "Settings must be confirmed.",
                },
            )
        try:
            return await asyncio.to_thread(
                _revise_standalone_settings, dependencies, project_id, body
            )
        except Exception as exc:
            raise dependencies.translate_error(exc) from exc

    return router


def _reject_managed_mode(dependencies: RuntimeSettingsDependencies) -> None:
    if dependencies.managed_mode():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "MANAGED_BY_HARNESS",
                "message": "Managed runtime settings are controlled by the owning system.",
            },
        )


def _request_hash(body: Any) -> str:
    return content_hash(body.model_dump(mode="json", exclude={"idempotency_key"}))


def _checked_replay(
    store: ProjectStore, idempotency_key: str, request_hash: str, message: str
) -> dict[str, Any] | None:
    replay = store.find_config_apply(idempotency_key)
    if replay is not None and replay["request_hash"] != request_hash:
        raise RuntimeRevisionError("IDEMPOTENCY_CONFLICT", message)
    return replay


def _revise_standalone_settings(
    dependencies: RuntimeSettingsDependencies,
    project_id: str,
    body: StandaloneRuntimeSettingsRequest,
) -> dict[str, Any]:
    store = dependencies.existing_store(project_id)
    store.recover_pending_transaction()
    request_hash = _request_hash(body)
    replay = _checked_replay(
        store,
        body.idempotency_key,
        request_hash,
        "The idempotency key was reused for different project settings.",
    )
    if replay is not None:
        return {
            **apply_receipt(replay),
            "settings": dependencies.runtime_settings_view(store),
        }
    with store.lock():
        replay = _checked_replay(
            store,
            body.idempotency_key,
            request_hash,
            "The idempotency key was reused for different project settings.",
        )
        if replay is not None:
            return {
                **apply_receipt(replay),
                "settings": dependencies.runtime_settings_view(store),
            }
        active = dependencies.project_runtime(store)
        if active.revision_id != body.base_revision_id:
            raise RuntimeRevisionError(
                "SETTINGS_REVISION_CONFLICT",
                "The project runtime settings changed after they were read.",
            )
        boundary = dependencies.workflow_boundary(store)
        checkpoint_id = str(boundary.get("checkpoint_id") or "")
        dependencies.require_safe_checkpoint(store, checkpoint_id)
        store.ensure_active_config_binding(active.branch_binding())
        overrides = merge_settings_overrides(active.overrides, body.overrides)
        base = dependencies.project_baseline_runtime(store)
        policy = policy_with_overrides(base.policy, overrides)
        model_document = model_document_with_overrides(base.model_document, overrides)
        revision_id = dependencies.next_project_revision_id(store, active.revision_id)
        branch_id = config_branch_name(revision_id, body.idempotency_key)
        effective_state = "next_workflow_step"
        preliminary = build_runtime_revision(
            project_id=store.project_id,
            revision_id=revision_id,
            parent_revision_id=active.revision_id,
            task_config_revision_id=active.task_config_revision_id,
            overrides=overrides,
            policy=policy,
            model_document=model_document,
            actor_type="member",
            actor_id=body.actor,
            apply_mode="safe_checkpoint_branch",
            branch_id=branch_id,
            checkpoint_id=checkpoint_id,
            effective_from_state=effective_state,
        )

        def publish(receipt: dict[str, Any]) -> None:
            final = build_runtime_revision(
                project_id=store.project_id,
                revision_id=revision_id,
                parent_revision_id=active.revision_id,
                task_config_revision_id=active.task_config_revision_id,
                overrides=overrides,
                policy=policy,
                model_document=model_document,
                actor_type="member",
                actor_id=body.actor,
                apply_mode="safe_checkpoint_branch",
                branch_id=receipt["branch_id"],
                checkpoint_id=receipt["checkpoint_id"],
                effective_from_state=effective_state,
            )
            publish_revision(store.root / "runtime-config", final[0], final[1], final[2])

        store.branch_from(
            checkpoint_id,
            name=branch_id,
            mode="fork_after",
            config_binding=preliminary[3],
            config_apply_idempotency_key=body.idempotency_key,
            config_apply_request_hash=request_hash,
            after_persist=publish,
        )
        applied = store.find_config_apply(body.idempotency_key)
        if applied is None:
            raise RuntimeRevisionError(
                "CONFIG_INTEGRITY_FAILED",
                "The project settings revision could not be resolved.",
            )
        store.events.append(
            "config_revision_applied",
            runtime_config_revision_id=revision_id,
            branch_id=applied["branch_id"],
            checkpoint_id=applied["checkpoint_id"],
            config_hash=applied["config_hash"],
            actor=body.actor,
        )
        return {
            **apply_receipt(applied),
            "settings": dependencies.runtime_settings_view(store),
        }
