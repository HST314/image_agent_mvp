"""Workflow boundary gateway: hot reload, role checks and mandatory auditing."""
from __future__ import annotations
from typing import Any, Callable
from uuid import uuid4
from agent_core.models import ModelRole
from model_router.executor import ModelExecutor
from model_router.router import ModelRouter, ModelRoute
from storage.project_store import ProjectStore, content_hash

class RuntimeModelGateway:
    def __init__(self, store: ProjectStore, router: ModelRouter, executor: ModelExecutor[Any] | None = None, *, offline_mode: bool = False) -> None:
        self.store, self.router, self.executor, self.offline_mode = store, router, executor or ModelExecutor(), offline_mode

    def call(self, state: str, role: ModelRole, invoke: Callable[[Any], Any], *, messages: list[dict[str, Any]], variables: dict[str, Any], template_id: str, template_version: str, input_refs: list[str], parent_prompt: str | None = None, round_number: int | None = None, needs_images: int = 0) -> Any:
        self.router = self.router.reload_at_boundary()
        binding = self.router.validate_capability(state, role=role, needs_images=needs_images)
        route = ModelRoute(binding=binding, mock=True) if self.offline_mode else self.router.route_for_state(state)
        snapshot = binding.model_dump(mode="json")
        trace = f"trace_{uuid4().hex}"
        audit = {"messages": messages, "template_id": template_id, "template_version": template_version,
            "template_hash": content_hash(messages), "variables": variables, "input_refs": input_refs,
            "model": {"provider": binding.provider, "name": binding.model, "role": role.value},
            "parameters": binding.parameters, "config_hash": self.router.config_hash, "state": state,
            "trace_id": trace, "parent_prompt": parent_prompt, "round": round_number}
        self.store.events.append("model_config_loaded", state=state, config_hash=self.router.config_hash, binding=snapshot)
        return self.executor.audited_run(lambda: invoke(route), prompts=self.store.prompts, audit=audit)
