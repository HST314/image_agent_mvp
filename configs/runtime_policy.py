"""Strict, reproducible runtime policy loading."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SelfCheckPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    termination: Literal["fix", "solo"] = "solo"
    fixed_rounds: int = Field(2, ge=1, le=20)
    max_rounds: int = Field(4, ge=1, le=50)
    stop_early_on_pass: bool = False
    release: Literal["auto", "manual"] = "auto"

    @model_validator(mode="after")
    def validate_rounds(self):
        if self.fixed_rounds > self.max_rounds:
            raise ValueError("fixed_rounds cannot exceed max_rounds")
        return self


class SkillInvocationPolicyConfig(BaseModel):
    """Release policy shared by one explicit library boundary.

    off = 不使用数据库：跳过该库的加载与提示词注入，阶段界面仍保留并自动通过。
    """
    model_config = ConfigDict(extra="forbid", frozen=True)
    release: Literal["auto", "manual", "off"] = "auto"


class RuntimePolicy(BaseModel):
    """Every declared field is consumed by a named production concern."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_auto_questions: int = Field(3, ge=0, le=10)
    stream_model_output: Literal[False] = False  # true is rejected until the streaming job phase is installed
    clarification_total_budget: int = Field(10, ge=0, le=100)
    # 提问偏好：proactive=全程积极全面追问（允许模型提出任务卡未知项之外的新问题，
    # 答案写入任务卡已知事实，随任务书注入后续阶段）；blocking_only=只问阻断交付的关键问题。
    question_preference: Literal["proactive", "blocking_only"] = "proactive"
    category_constraint: SkillInvocationPolicyConfig = Field(default_factory=SkillInvocationPolicyConfig)
    style_direction: SkillInvocationPolicyConfig = Field(default_factory=SkillInvocationPolicyConfig)
    # Kept as a read-compatible legacy field. New workflow code uses the two
    # explicit gates above; old persisted projects remain loadable.
    skill_invocation: SkillInvocationPolicyConfig = Field(default_factory=SkillInvocationPolicyConfig)
    self_check: SelfCheckPolicyConfig = Field(default_factory=SelfCheckPolicyConfig)
    max_render_retries: Literal[0] = 0
    candidate_concurrency: int = Field(5, ge=1, le=5)
    model_timeout_seconds: float = Field(180, gt=0, le=3600)
    image_api_base_url: str = ""
    default_output_size: str = Field("2560x1440", pattern=r"^(\d{2,5}x\d{2,5}|[124]K)$")
    response_format: Literal["url", "b64_json"] = "url"
    watermark: bool = False
    offline_mode: bool = False
    allow_skill_degradation: bool = False
    style_library_root: str = "agent-library"
    source_config_revision: str | None = None
    config_hash: str | None = None
    generated_at: str | None = None

    CONSUMERS: ClassVar[dict[str, str]] = {
        "max_auto_questions": "interaction.question_generator",
        "stream_model_output": "model_router.clients",
        "clarification_total_budget": "agent_core.workflow_runner",
        "question_preference": "interaction.question_generator(clarify prompt mode)",
        "category_constraint": "agent_core.workflow_runner(category constraint gate)",
        "style_direction": "agent_core.workflow_runner(style direction gate)",
        "skill_invocation": "legacy persisted-policy compatibility decoder",
        "self_check": "calibrator.calibration_loop",
        "max_render_retries": "agent_core.batch(no automatic paid retry)",
        "candidate_concurrency": "agent_core.batch",
        "model_timeout_seconds": "model_router.clients SDK timeout",
        "image_api_base_url": "render_clients.image_render_client",
        "default_output_size": "render_clients.payload_mapper",
        "response_format": "render_clients.payload_mapper",
        "watermark": "render_clients.payload_mapper",
        "offline_mode": "model_router.gateway",
        "allow_skill_degradation": "agent_core.workflow_runner(resource fallback gates)",
        "style_library_root": "skills.style_library",
        "source_config_revision": "harness.task_config(materialization metadata)",
        "config_hash": "harness.task_config(materialization integrity)",
        "generated_at": "harness.task_config(materialization audit)",
    }

    def consumer_matrix(self) -> dict[str, str]:
        return dict(self.CONSUMERS)

    def snapshot(self) -> dict:
        return self.model_dump(mode="json")

    def sha256(self) -> str:
        raw = json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def from_file(cls, path: str | Path) -> "RuntimePolicy":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("runtime policy must be a mapping")
        return cls.model_validate(data)
