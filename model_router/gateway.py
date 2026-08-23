"""Workflow boundary gateway: hot reload, role checks and mandatory auditing."""
from __future__ import annotations
from typing import Any, Callable
from uuid import uuid4
from agent_core.models import ModelRole
from model_router.executor import ModelExecutor
from model_router.router import ModelRouter, ModelRoute
from model_router.usage import ProviderUsageObservation, capture_provider_usage
from storage.project_store import ProjectStore, content_hash


def unresolved_model_call_actions(store: ProjectStore) -> list[dict[str, Any]]:
    """Project unresolved paid-call outcomes without loading a model route."""

    events = store.history()
    resolved = {
        event["idempotency_key"]
        for event in events
        if event.get("type") == "model_call_unknown_resolved"
    }
    return [
        {
            "idempotency_key": event["idempotency_key"],
            "trace_id": event["trace_id"],
            "actions": ["retry_after_confirmation", "abandon"],
        }
        for event in events
        if event.get("type") == "model_call_unknown"
        and event["idempotency_key"] not in resolved
    ]


def resolve_unknown_model_call(
    store: ProjectStore, idempotency_key: str, action: str, actor: str
) -> None:
    if action not in {"retry_after_confirmation", "abandon"} or not actor:
        raise ValueError("无效的人工处置。")
    unresolved = {
        item["idempotency_key"] for item in unresolved_model_call_actions(store)
    }
    if idempotency_key not in unresolved:
        raise ValueError("未知态不存在或已经处置，未重复执行。")
    store.events.append(
        "model_call_unknown_resolved",
        idempotency_key=idempotency_key,
        action=action,
        actor=actor,
        resolved=True,
    )

class RuntimeModelGateway:
    def __init__(self, store: ProjectStore, router: ModelRouter, executor: ModelExecutor[Any] | None = None, *, offline_mode: bool = False) -> None:
        self.store, self.router, self.executor, self.offline_mode = store, router, executor or ModelExecutor(), offline_mode

    def call(self, state: str, role: ModelRole, invoke: Callable[[Any], Any], *, messages: list[dict[str, Any]], variables: dict[str, Any], template_id: str, template_version: str, input_refs: list[str], parent_prompt: str | None = None, round_number: int | None = None, needs_images: int = 0) -> Any:
        self.router = self.router.reload_at_boundary()
        binding = self.router.validate_capability(state, role=role, needs_images=needs_images)
        route = ModelRoute(binding=binding, mock=True) if self.offline_mode else self.router.route_for_state(state)
        snapshot = binding.model_dump(mode="json")
        trace = f"trace_{uuid4().hex}"
        idempotency_key = content_hash([state, template_id, template_version, messages, variables, input_refs])
        route = ModelRoute(binding=route.binding.model_copy(update={"parameters": {
            **route.binding.parameters, "_idempotency_key": idempotency_key}}), mock=route.mock)
        relevant = [event for event in self.store.history() if event.get("idempotency_key") == idempotency_key and event.get("type") in {"model_call_unknown", "model_call_unknown_resolved"}]
        unresolved = relevant[-1] if relevant and relevant[-1]["type"] == "model_call_unknown" else None
        if unresolved:
            raise RuntimeError("同一模型调用结果仍未知，须人工确认重试或放弃后才能继续。")
        audit = {"messages": messages, "template_id": template_id, "template_version": template_version,
            "template_hash": content_hash(messages), "variables": variables, "input_refs": input_refs,
            "model": {"provider": binding.provider, "name": binding.model, "role": role.value},
            "parameters": binding.parameters, "config_hash": self.router.config_hash, "state": state,
            "trace_id": trace, "request_id": idempotency_key, "idempotency_key": idempotency_key,
            "parent_prompt": parent_prompt, "round": round_number}
        self.store.events.append("model_config_loaded", state=state, config_hash=self.router.config_hash, binding=snapshot)
        self.store.events.append("model_call_started", state=state, trace_id=trace, idempotency_key=idempotency_key)
        try:
            observed_usage: list[ProviderUsageObservation] = []
            with capture_provider_usage(observed_usage.append):
                result = self.executor.audited_run(
                    lambda: invoke(route), prompts=self.store.prompts, audit=audit
                )
            self.store.events.append("model_call_completed", state=state, trace_id=trace, idempotency_key=idempotency_key)
            for index, usage in enumerate(observed_usage):
                token_usage = usage.token_usage
                self.store.events.append(
                    "model_usage_recorded",
                    usage_id=f"usage_{content_hash([idempotency_key, binding.model, index])[:24]}",
                    request_id=idempotency_key,
                    provider_request_id=usage.provider_request_id,
                    provider=binding.provider,
                    model=binding.model,
                    call_type=role.value,
                    usage_basis=(
                        "mixed" if token_usage and usage.billing_units
                        else "tokens" if token_usage
                        else "image_units"
                    ),
                    token_usage=token_usage,
                    billing_units=list(usage.billing_units),
                    raw_usage=usage.raw_usage,
                )
            return result
        except Exception as exc:
            if str(getattr(exc, "category", "")).endswith("_unknown"):
                self.store.events.append("model_call_unknown", state=state, trace_id=trace,
                                         idempotency_key=idempotency_key, possible_charge=True,
                                         recovery_actions=["retry_after_confirmation", "abandon"])
            raise

    def resolve_unknown(self, idempotency_key: str, action: str, actor: str) -> None:
        resolve_unknown_model_call(self.store, idempotency_key, action, actor)

    def unknown_actions(self) -> list[dict[str, Any]]:
        """API/UI projection for unresolved paid-call outcomes."""
        return unresolved_model_call_actions(self.store)
