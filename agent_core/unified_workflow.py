"""Single domain state graph, approval gates, revision and delivery freezing."""
from __future__ import annotations

import difflib
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from storage.project_store import content_hash


class DomainState(str, Enum):
    TASK = "task"
    CLARIFICATION = "clarification"
    TASK_BOOK = "task_book"
    TASK_APPROVAL = "task_approval"
    CATEGORY_ANALYSIS = "category_analysis"
    STYLE_SELECTION = "style_selection"
    FIVE_RENDER = "five_render"
    MASTER_SELECTION = "master_selection"
    QUALITY_REWORK = "quality_rework"
    HUMAN_ACTION = "human_action"
    HUMAN_EDIT = "human_edit"
    REINSPECTION = "reinspection"
    FINAL_APPROVAL = "final_approval"
    DELIVERY_FROZEN = "delivery_frozen"
    DESCRIPTION = "description"


FLOW = tuple(DomainState)
PAID_STATES = frozenset({DomainState.STYLE_SELECTION, DomainState.FIVE_RENDER, DomainState.QUALITY_REWORK,
                         DomainState.HUMAN_EDIT, DomainState.REINSPECTION, DomainState.DESCRIPTION})
WAITING_STATES = frozenset({DomainState.TASK_APPROVAL, DomainState.MASTER_SELECTION, DomainState.HUMAN_ACTION,
                            DomainState.FINAL_APPROVAL})


class TaskRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    raw_task: str
    task_markdown: str
    actor: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    previous_hash: str | None = None
    revision_hash: str
    diff: str


class FrozenDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision_hash: str
    asset_id: str
    asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality_version: str
    actor: str
    confirmed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def revise_task(history: list[TaskRevision], raw_task: str, markdown: str, actor: str) -> TaskRevision:
    if not actor:
        raise ValueError("actor required")
    previous = history[-1] if history else None
    before = (previous.raw_task + "\n" + previous.task_markdown).splitlines(keepends=True) if previous else []
    after = (raw_task + "\n" + markdown).splitlines(keepends=True)
    digest = content_hash({"raw_task": raw_task, "task_markdown": markdown, "parent": previous.revision_hash if previous else None})
    return TaskRevision(version=len(history) + 1, raw_task=raw_task, task_markdown=markdown, actor=actor,
                        previous_hash=previous.revision_hash if previous else None, revision_hash=digest,
                        diff="".join(difflib.unified_diff(before, after, fromfile="previous", tofile="current")))


def approval_valid(snapshot: dict[str, Any]) -> bool:
    approval = snapshot.get("task_approval") or {}
    revision = snapshot.get("task_revision") or {}
    return bool(approval.get("actor") and approval.get("revision_hash") == revision.get("revision_hash"))


def require_transition(current: DomainState, target: DomainState, snapshot: dict[str, Any]) -> None:
    if FLOW.index(target) != FLOW.index(current) + 1:
        raise ValueError(f"illegal transition: {current.value}->{target.value}")
    if target in PAID_STATES and not approval_valid(snapshot):
        raise ValueError("TASK_APPROVAL_REQUIRED")


def freeze_delivery(snapshot: dict[str, Any], *, asset: dict[str, Any], quality_version: str, actor: str) -> FrozenDelivery:
    if not approval_valid(snapshot):
        raise ValueError("TASK_APPROVAL_REQUIRED")
    if snapshot.get("quality_asset_sha256") != asset.get("sha256"):
        raise ValueError("LATEST_ASSET_REINSPECTION_REQUIRED")
    if not snapshot.get("quality_passed"):
        raise ValueError("QUALITY_GATE_NOT_PASSED")
    return FrozenDelivery(revision_hash=snapshot["task_revision"]["revision_hash"], asset_id=asset["artifact_id"],
                          asset_sha256=asset["sha256"], quality_version=quality_version, actor=actor)


ERROR_ACTIONS = {
    "timeout_unknown": ("retry_after_confirmation", "abandon"),
    "rate_limited": ("retry", "abandon"),
    "authentication": (),
    "content_moderation": ("revise_task", "abandon"),
    "structured_output": ("retry", "abandon"),
    "invalid_input": (),
    "waiting_human": ("continue", "abandon"),
}


def classify_error(exc: Exception) -> str:
    """Map production failures to the recovery contract (deny retry by default)."""
    message = str(exc).upper()
    name = type(exc).__name__.lower()
    if "AUTH" in message or "PERMISSION" in message:
        return "authentication"
    if "RATE" in message and "LIMIT" in message:
        return "rate_limited"
    if "TIMEOUT" in message or "UNKNOWN" in message:
        return "timeout_unknown"
    if "MODERATION" in message or "CONTENT_POLICY" in message:
        return "content_moderation"
    if "VALIDATION" in name or "JSON" in message or "SCHEMA" in message:
        return "structured_output"
    return "invalid_input"


def recovery_actions(category: str) -> tuple[str, ...]:
    if category not in ERROR_ACTIONS:
        raise ValueError("UNKNOWN_ERROR_CATEGORY")
    return ERROR_ACTIONS[category]
