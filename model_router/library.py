"""模型库：设置页可选模型池与运行时绑定的能力匹配。

model_library.yaml 是备选池（按能力分 text/vlm/image 三组）；
model_config.yaml 仍是运行时实际绑定。阶段只允许绑定与其 model_role
对应分组内的模型（REQUIRED_STATE_ROLES 决定每个阶段所需角色）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field

from agent_core.models import ModelConfig, ModelRole, StateBinding
from model_router.router import REQUIRED_STATE_ROLES

# 模型库分组 → 模型角色；阶段角色经 REQUIRED_STATE_ROLES 查得后落到唯一分组。
GROUP_TO_ROLE: dict[str, ModelRole] = {
    "text_models": ModelRole.REASONING_LLM,
    "vlm_models": ModelRole.VISION_LANGUAGE_MODEL,
    "image_models": ModelRole.TEXT_TO_IMAGE_MODEL,
}
ROLE_TO_GROUP: dict[ModelRole, str] = {role: group for group, role in GROUP_TO_ROLE.items()}


class LibraryModelEntry(BaseModel):
    """模型库中的一条备选模型。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ModelLibrary(BaseModel):
    """按能力分组的备选模型池。"""

    model_config = ConfigDict(extra="forbid")
    library_id: str = "image-agent-model-library"
    text_models: list[LibraryModelEntry] = Field(default_factory=list)
    vlm_models: list[LibraryModelEntry] = Field(default_factory=list)
    image_models: list[LibraryModelEntry] = Field(default_factory=list)

    def group(self, name: str) -> list[LibraryModelEntry]:
        if name not in GROUP_TO_ROLE:
            raise KeyError(f"未知模型分组：{name}")
        return list(getattr(self, name))

    def find(self, entry_id: str, *, group: str) -> LibraryModelEntry | None:
        for entry in self.group(group):
            if entry.id == entry_id:
                return entry
        return None


def load_library(path: str | Path) -> ModelLibrary:
    library_path = Path(path)
    if not library_path.is_file():
        # 库缺失时返回空库：设置页正常渲染（各下拉为空），不阻断运行时。
        return ModelLibrary()
    payload = yaml.safe_load(library_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("模型库必须是键值对结构。")
    return ModelLibrary.model_validate(payload)


def load_config(path: str | Path) -> ModelConfig:
    """读取运行时模型绑定（model_config.yaml）。"""
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("模型配置必须是键值对结构。")
    return ModelConfig.model_validate(payload)


def settings_view(library: ModelLibrary, config: ModelConfig) -> dict[str, Any]:
    """GET /api/settings/models 的视图：备选池 + 各阶段当前绑定。"""
    bindings = {binding.state: binding for binding in config.state_bindings}
    states = []
    for state, role in REQUIRED_STATE_ROLES.items():
        binding = bindings.get(state)
        states.append({
            "state": state,
            "model_role": role.value,
            "group": ROLE_TO_GROUP[role],
            "binding": binding.model_dump(mode="json") if binding else None,
        })
    return {
        "library_id": library.library_id,
        "library": {
            group: [entry.model_dump(mode="json") for entry in library.group(group)]
            for group in GROUP_TO_ROLE
        },
        "states": states,
    }


def apply_bindings(
    library: ModelLibrary,
    config: ModelConfig,
    updates: dict[str, str],
) -> ModelConfig:
    """把「阶段 → 库条目 id」应用到运行时绑定，强制能力匹配。

    未列出的阶段保持原绑定；库条目提供 provider/model/默认参数，
    原绑定中运行期追加的参数（如 max_reference_images 覆盖）保留。
    """
    bindings = {binding.state: binding for binding in config.state_bindings}
    for state, entry_id in updates.items():
        if state not in REQUIRED_STATE_ROLES:
            raise ValueError(f"未知的工作流阶段：{state}")
        role = REQUIRED_STATE_ROLES[state]
        entry = library.find(entry_id, group=ROLE_TO_GROUP[role])
        if entry is None:
            raise ValueError(f"阶段 {state} 只能绑定{role.value}能力的模型。")
        current = bindings.get(state)
        # 同一模型重复保存时保留运行参数；切换到不同模型时采用库条目的默认参数，
        # 避免旧模型参数（如参考图上限）泄漏到新模型。
        if current and current.model == entry.model and current.provider == entry.provider:
            parameters = dict(current.parameters)
        else:
            parameters = dict(entry.parameters)
        bindings[state] = StateBinding(
            state=state,
            model_role=role,
            provider=entry.provider,
            model=entry.model,
            parameters=parameters,
            fallback_model=current.fallback_model if current else None,
        )
    ordered = [bindings[state] for state in REQUIRED_STATE_ROLES if state in bindings]
    ordered.extend(binding for state, binding in bindings.items() if state not in REQUIRED_STATE_ROLES)
    return config.model_copy(update={"state_bindings": ordered})


def write_model_config(path: str | Path, config: ModelConfig) -> None:
    """原子改写 model_config.yaml；ModelRouter 在下一个阶段边界热加载。"""
    config_path = Path(path)
    temp = config_path.with_name(f".{config_path.name}.{uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            yaml.safe_dump(config.model_dump(mode="json"), stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(config_path)
    finally:
        temp.unlink(missing_ok=True)
