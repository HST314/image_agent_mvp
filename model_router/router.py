"""State-to-model routing based on ModelConfig."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent_core.models import ModelConfig, ModelRole, StateBinding


REQUIRED_STATE_ROLES: dict[str, ModelRole] = {
    "intake_clarify": ModelRole.REASONING_LLM,
    "confirmation_build": ModelRole.REASONING_LLM,
    "initial_candidate_generation": ModelRole.TEXT_TO_IMAGE_MODEL,
    "self_check_inspection": ModelRole.VISION_LANGUAGE_MODEL,
    "self_check_rework": ModelRole.TEXT_TO_IMAGE_MODEL,
    "human_prompt_rework": ModelRole.TEXT_TO_IMAGE_MODEL,
}

PROVIDER_KEY_ENV: dict[str, str] = {
    "ark": "ARK_API_KEY",
    "volcengine": "ARK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "vlm": "VLM_API_KEY",
}


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """Resolved model route plus offline mock metadata."""

    binding: StateBinding
    mock: bool
    key_env: str | None = None


class ModelRouter:
    """Resolve model bindings for workflow states."""

    def __init__(self, config: ModelConfig, *, source_path: Path | None = None, config_hash: str = "") -> None:
        self._bindings = {binding.state: binding for binding in config.state_bindings}
        self.source_path = source_path
        self.config_hash = config_hash

    @classmethod
    def from_file(cls, path: str | Path) -> "ModelRouter":
        """Load model routing from a JSON or YAML config file."""

        config_path = Path(path)
        raw_text = config_path.read_text(encoding="utf-8")
        if config_path.suffix.lower() == ".json":
            payload = json.loads(raw_text)
        else:
            payload = yaml.safe_load(raw_text)
        import hashlib
        return cls(ModelConfig.model_validate(payload), source_path=config_path, config_hash=hashlib.sha256(raw_text.encode()).hexdigest())

    def reload_at_boundary(self) -> "ModelRouter":
        """Reload only at a workflow state or iteration boundary."""
        return self.from_file(self.source_path) if self.source_path else self

    def binding_for_state(self, state: str) -> StateBinding:
        """Return the configured binding for a state."""

        try:
            return self._bindings[state]
        except KeyError as exc:
            raise KeyError(f"No model binding configured for state '{state}'.") from exc

    def route_for_state(self, state: str) -> ModelRoute:
        """Return a state binding and whether it will run in mock mode."""

        binding = self.binding_for_state(state)
        key_env = PROVIDER_KEY_ENV.get(binding.provider)
        mock = binding.provider == "offline"
        if key_env and not os.getenv(key_env) and not mock:
            raise RuntimeError(f"{state} 缺少远程模型凭证；仅显式 offline 配置允许模拟运行。")
        return ModelRoute(binding=binding, mock=mock, key_env=key_env)

    def validate_capability(self, state: str, *, role: ModelRole, needs_images: int = 0) -> StateBinding:
        binding = self.binding_for_state(state)
        if binding.model_role is not role:
            raise ValueError(f"模型角色不匹配：{state} 需要 {role.value}。")
        limit = int(binding.parameters.get("max_reference_images", 99))
        if needs_images > limit:
            raise ValueError(f"当前模型最多支持 {limit} 张参考图。")
        return binding

    def validate_required_bindings(self) -> None:
        """Validate the default MVP workflow states against expected model roles."""

        for state, expected_role in REQUIRED_STATE_ROLES.items():
            binding = self.binding_for_state(state)
            if binding.model_role is not expected_role:
                raise ValueError(
                    f"State '{state}' must use role '{expected_role.value}', got '{binding.model_role.value}'."
                )
