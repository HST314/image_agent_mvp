"""Stable, user-presentable failures for external skill resources."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class ResourceError(RuntimeError):
    code: str
    resource: str
    trace_id: str
    degradation: str = "blocked"
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.resource} ({self.trace_id})"

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "resource": self.resource, "trace_id": self.trace_id,
                "degradation": self.degradation, "detail": self.detail}
