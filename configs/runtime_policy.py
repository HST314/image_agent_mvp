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


class RuntimePolicy(BaseModel):
    """Every declared field is consumed by a named production concern."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_auto_questions: int = Field(3, ge=0, le=10)
    stream_model_output: Literal[False] = False  # true is rejected until the streaming job phase is installed
    clarification_total_budget: int = Field(10, ge=0, le=100)
    self_check: SelfCheckPolicyConfig = Field(default_factory=SelfCheckPolicyConfig)
    max_render_retries: Literal[0] = 0
    candidate_concurrency: int = Field(5, ge=1, le=5)
    model_timeout_seconds: float = Field(180, gt=0, le=3600)
    image_api_base_url: str = ""
    default_output_size: str = Field("1024x1024", pattern=r"^(\d{2,5}x\d{2,5}|[124]K)$")
    response_format: Literal["url", "b64_json"] = "url"
    watermark: bool = False
    offline_mode: bool = False
    allow_skill_degradation: bool = False

    CONSUMERS: ClassVar[dict[str, str]] = {
        "max_auto_questions": "interaction.question_generator",
        "stream_model_output": "model_router.clients",
        "clarification_total_budget": "agent_core.workflow_runner",
        "self_check": "calibrator.calibration_loop",
        "max_render_retries": "agent_core.batch(no automatic paid retry)",
        "candidate_concurrency": "agent_core.batch",
        "model_timeout_seconds": "model_router.clients SDK timeout",
        "image_api_base_url": "render_clients.image_render_client",
        "default_output_size": "render_clients.payload_mapper",
        "response_format": "render_clients.payload_mapper",
        "watermark": "render_clients.payload_mapper",
        "offline_mode": "model_router.gateway",
        "allow_skill_degradation": "skills.resource_loader",
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
