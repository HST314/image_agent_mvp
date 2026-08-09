"""Versioned runtime context persisted at successful workflow boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter
from storage.project_store import FORMAT_VERSION, CorruptProjectError, content_hash

from agent_core.models import (
    CalibrationAttempt,
    CandidateAsset,
    CategorySkill,
    DirectionSelection,
    FinalApprovalRecord,
    ImageTaskCard,
    MasterCandidateLock,
    PromptVersion,
    QuestionAnswerRecord,
    QuestionCard,
    StyleCard,
    StyleIdeaCard,
    TaskConfirmationDoc,
    TraceLog,
)


@dataclass(slots=True)
class AgentContext:
    """Mutable in-memory context for an offline workflow run."""

    task_card: ImageTaskCard
    question_card: QuestionCard | None = None
    answer_record: QuestionAnswerRecord | None = None
    confirmation_doc: TaskConfirmationDoc | None = None
    category_skill: CategorySkill | None = None
    style_cards: list[StyleCard] = field(default_factory=list)
    style_idea_cards: list[StyleIdeaCard] = field(default_factory=list)
    prompt_versions: list[PromptVersion] = field(default_factory=list)
    candidate_assets: list[CandidateAsset] = field(default_factory=list)
    direction_selection: DirectionSelection | None = None
    master_lock: MasterCandidateLock | None = None
    calibration_attempts: list[CalibrationAttempt] = field(default_factory=list)
    final_approval: FinalApprovalRecord | None = None
    traces: list[TraceLog] = field(default_factory=list)

    def dump_snapshot(self) -> dict[str, Any]:
        """Return a versioned, self-validating checkpoint payload."""
        fields = {
            name: _dump(getattr(self, name))
            for name in self.__dataclass_fields__
        }
        envelope = {"format_version": FORMAT_VERSION, "context": fields}
        envelope["integrity_hash"] = content_hash(envelope)
        return envelope

    @classmethod
    def load_snapshot(cls, snapshot: dict[str, Any]) -> "AgentContext":
        raw = dict(snapshot)
        checksum = raw.pop("integrity_hash", None)
        if raw.get("format_version") != FORMAT_VERSION or checksum != content_hash(raw):
            raise CorruptProjectError("运行快照版本或完整性校验失败。")
        data = raw.get("context")
        if not isinstance(data, dict):
            raise CorruptProjectError("运行快照缺少上下文。")
        types = {
            "task_card": ImageTaskCard, "question_card": QuestionCard | None,
            "answer_record": QuestionAnswerRecord | None, "confirmation_doc": TaskConfirmationDoc | None,
            "category_skill": CategorySkill | None, "style_cards": list[StyleCard],
            "style_idea_cards": list[StyleIdeaCard], "prompt_versions": list[PromptVersion],
            "candidate_assets": list[CandidateAsset], "direction_selection": DirectionSelection | None,
            "master_lock": MasterCandidateLock | None, "calibration_attempts": list[CalibrationAttempt],
            "final_approval": FinalApprovalRecord | None, "traces": list[TraceLog],
        }
        try:
            return cls(**{key: TypeAdapter(annotation).validate_python(data.get(key)) for key, annotation in types.items()})
        except Exception as exc:
            raise CorruptProjectError(f"运行快照内容无效：{exc}") from exc


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump(item) for item in value]
    return value
