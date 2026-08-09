"""Versioned parent/sub-agent boundary contracts."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DesignTaskEnvelopeV1(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=8)
    task: dict[str, Any]
    trace_id: str | None = None


class DeliveryAssetV1(ContractModel):
    artifact_id: str = Field(pattern=r"^artifact_[0-9a-f]{24}$")
    uri: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("uri")
    @classmethod
    def stable_uri(cls, value: str) -> str:
        if not value.startswith("artifact://"):
            raise ValueError("delivery assets require artifact:// URIs")
        return value


class DesignDeliveryEnvelopeV1(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    project_id: str
    final_image: DeliveryAssetV1
    design_note_markdown: str = Field(min_length=1)
    design_note: dict[str, Any]
    trace_ref: str = Field(min_length=1)
