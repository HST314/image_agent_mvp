"""Pydantic V2 models for the image agent MVP contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def new_id(prefix: str) -> str:
    """Create a stable, readable identifier for generated records."""

    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


class StrictBaseModel(BaseModel):
    """Base model with explicit serialization defaults."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TaskStatus(str, Enum):
    """Lifecycle status values stored on an image task card."""

    DRAFT = "draft"
    CLARIFYING = "clarifying"
    PENDING_CONFIRM = "pending_confirm"
    APPROVED = "approved"
    GENERATING = "generating"
    REVIEW = "review"
    REWORK = "rework"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class QuestionCardStatus(str, Enum):
    """Answer status values for a clarification card."""

    PENDING_ANSWER = "pending_answer"
    ANSWERED = "answered"
    SKIPPED = "skipped"
    EXPIRED = "expired"


class SignStatus(str, Enum):
    """Human sign-off status for the confirmation document."""

    PENDING_SIGN = "pending_sign"
    APPROVED = "approved"
    MODIFIED = "modified"


class RiskLevel(str, Enum):
    """Risk levels used when unknown fields need default handling."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKING = "blocking"


class SkillStatus(str, Enum):
    """Publication status for runtime-loaded knowledge cards."""

    CANDIDATE = "candidate"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class ModelRole(str, Enum):
    """Model roles routed by workflow state."""

    REASONING_LLM = "reasoning_llm"
    VISION_LANGUAGE_MODEL = "vision_language_model"
    TEXT_TO_IMAGE_MODEL = "text_to_image_model"


class SourceRef(StrictBaseModel):
    """Reference to source material used by the task."""

    ref_id: str
    ref_type: str
    excerpt: str | None = None
    source_hash: str | None = None


class AssetInput(StrictBaseModel):
    """Input asset and its allowed usage rule."""

    asset_id: str
    asset_type: str
    usage_rule: str
    verified: bool = False


class CategoryRef(StrictBaseModel):
    """Optional runtime category-skill reference supplied by input data."""

    category_id: str
    version: str | None = None


class ImageTaskCard(StrictBaseModel):
    """Validated image task input contract."""

    task_id: str
    project_id: str
    parent_task_id: str | None = None
    source_refs: list[SourceRef]
    deliverable_goal: str
    usage_context: str
    category_ref: CategoryRef | None = None
    known_facts: dict[str, Any] = Field(default_factory=dict)
    unknowns: dict[str, Any] = Field(default_factory=dict)
    asset_inputs: list[AssetInput] = Field(default_factory=list)
    question_card_id: str | None = None
    confirmation_doc_id: str | None = None
    selected_style_direction_ids: list[str] = Field(default_factory=list)
    selected_master_asset_id: str | None = None
    calibration_history: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.DRAFT

    @field_validator("source_refs")
    @classmethod
    def source_refs_must_not_be_empty(cls, value: list[SourceRef]) -> list[SourceRef]:
        """Require at least one source reference for traceability."""

        if not value:
            raise ValueError("source_refs must contain at least one reference")
        return value


class QuestionOption(StrictBaseModel):
    """One mutually exclusive answer option."""

    option_id: str
    label: str
    description: str


class QuestionItem(StrictBaseModel):
    """Clarification question with options, recommendation, and impact."""

    question_id: str
    field: str
    question: str
    options: list[QuestionOption] = Field(min_length=2)
    recommended_option_id: str
    impact: str
    evidence: str = ""
    missing: bool = True
    has_safe_default: bool = False
    blocking: bool = False
    semantic_fingerprint: str = ""
    user_selected_option_id: str | None = None
    user_free_text: str | None = None

    @field_validator("recommended_option_id")
    @classmethod
    def recommended_option_must_exist(cls, value: str, info: Any) -> str:
        """Validate the recommended option against the local option ids."""

        options = info.data.get("options", [])
        if options and value not in {option.option_id for option in options}:
            raise ValueError("recommended_option_id must match one option_id")
        return value


class QuestionCard(StrictBaseModel):
    """Configurable clarification card produced during intake."""

    question_card_id: str = Field(default_factory=lambda: new_id("question_card"))
    task_id: str
    generated_by_state: Literal["intake_clarify"] = "intake_clarify"
    questions: list[QuestionItem] = Field(min_length=0, max_length=10)
    status: QuestionCardStatus = QuestionCardStatus.PENDING_ANSWER


class QuestionAnswer(StrictBaseModel):
    """Collected answer for one clarification question."""

    question_id: str
    selected_option_id: str | None
    free_text: str | None = None
    skipped: bool = False


class QuestionAnswerRecord(StrictBaseModel):
    """Answer set collected for a question card."""

    answer_record_id: str = Field(default_factory=lambda: new_id("answer_record"))
    question_card_id: str
    task_id: str
    answers: list[QuestionAnswer]
    answered_at: datetime = Field(default_factory=utc_now)


class ConfirmedFact(StrictBaseModel):
    """A fact treated as confirmed in the task confirmation document."""

    field: str
    value: Any
    source_ref: str
    locked: bool = True


class UnknownHandling(StrictBaseModel):
    """Default handling policy for unresolved information."""

    field: str
    handling: str
    risk_level: RiskLevel


class StyleStrategy(StrictBaseModel):
    """Selection strategy for later style direction generation."""

    direction_count: Literal[5] = 5
    selection_rule: str
    diversity_requirement: str


class TaskConfirmationDoc(StrictBaseModel):
    """Human-reviewable task confirmation document."""

    confirmation_doc_id: str = Field(default_factory=lambda: new_id("confirmation_doc"))
    task_id: str
    version: str = "1.0"
    summary: str = ""
    confirmed_facts: list[ConfirmedFact]
    default_handling_for_unknowns: list[UnknownHandling]
    selected_style_strategy: StyleStrategy = Field(
        default_factory=lambda: StyleStrategy(
            selection_rule="提供五个不同方向机制供人工审核",
            diversity_requirement="方向必须在构图、视觉语言和交付风险上有差异",
        )
    )
    forbidden_items: list[str] = Field(default_factory=list)
    human_annotations: list[str] = Field(default_factory=list)
    markdown_body: str = ""
    sign_status: SignStatus = SignStatus.PENDING_SIGN
    signed_by: str | None = None
    signed_at: datetime | None = None


class SpecificationFact(StrictBaseModel):
    fact_id: str = Field(default_factory=lambda: new_id("fact"))
    label: str
    value: str
    provenance: str
    status: Literal["confirmed", "extracted", "tentative", "blocking"]


class TaskSpecification(StrictBaseModel):
    task_id: str
    version: int = Field(default=1, ge=1)
    facts: list[SpecificationFact] = Field(default_factory=list)
    parent_hash: str | None = None
    content_hash: str = ""

    def finalized(self) -> "TaskSpecification":
        from storage.project_store import content_hash
        copy = self.model_copy(deep=True)
        copy.content_hash = ""
        copy.content_hash = content_hash(copy.model_dump(mode="json"))
        return copy


class StyleIdeaCard(StrictBaseModel):
    """Human-readable style direction interpreted from a style reference."""

    idea_id: str = Field(default_factory=lambda: new_id("style_idea"))
    task_id: str
    source_style_id: str
    title: str
    composition: str
    material: str
    fit_reason: str
    major_risk: str
    prompt_supplement: str
    reference_asset: str | None = None
    generated_by: str = "mock_style_vlm"
    created_at: datetime = Field(default_factory=utc_now)


class AppliesWhen(StrictBaseModel):
    """Runtime match hints for external skill cards."""

    keywords: list[str] = Field(default_factory=list)
    semantic_rules: list[str] = Field(default_factory=list)
    negative_match_rules: list[str] = Field(default_factory=list)


class RequiredQuestion(StrictBaseModel):
    """Question requirement supplied by a runtime-loaded skill."""

    field: str
    question: str
    blocks_generation: bool
    default_handling: str | None = None


class PromptInjection(StrictBaseModel):
    """Prompt fragments supplied by a runtime-loaded skill."""

    category_description: str | None = None
    production_constraints: list[str] = Field(default_factory=list)
    visual_rules: list[str] = Field(default_factory=list)
    forbidden_elements: list[str] = Field(default_factory=list)


class CategorySkill(StrictBaseModel):
    """Generic runtime-loaded category skill."""

    category_id: str
    version: str
    display_name: str | None = None
    applies_when: AppliesWhen
    required_questions: list[RequiredQuestion] = Field(default_factory=list)
    prompt_injection: PromptInjection
    review_checks: list[str] = Field(default_factory=list)
    status: SkillStatus


class VisualLanguage(StrictBaseModel):
    """Style card visual-language properties."""

    materiality: list[str] = Field(default_factory=list)
    lighting: str | None = None
    camera: str | None = None
    density: str | None = None
    scheme: str | None = None


class StyleCard(StrictBaseModel):
    """Generic runtime-loaded style card."""

    style_id: str
    version: str
    style_name: str | None = None
    tags: list[str] = Field(default_factory=list)
    best_for: list[str] = Field(default_factory=list)
    avoid_for: list[str] = Field(default_factory=list)
    composition: str
    visual_language: VisualLanguage
    risk_notes: list[str] = Field(default_factory=list)
    negative_elements: list[str] = Field(default_factory=list)
    reference_assets: list[str] = Field(default_factory=list)
    status: SkillStatus


class StateBinding(StrictBaseModel):
    """Model binding for one workflow state."""

    state: str
    model_role: ModelRole
    provider: str
    model: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    fallback_model: str | None = None


class ModelConfig(StrictBaseModel):
    """Configurable model routing contract."""

    model_config_id: str
    state_bindings: list[StateBinding]


class TraceError(StrictBaseModel):
    """Structured error data for trace logging."""

    code: str
    message: str
    retryable: bool = False


class TraceLog(StrictBaseModel):
    """Append-only trace record for auditability."""

    trace_id: str = Field(default_factory=lambda: new_id("trace"))
    timestamp: datetime = Field(default_factory=utc_now)
    project_id: str
    task_id: str
    state: str
    event_type: str
    summary: str
    input_refs: list[str] = Field(default_factory=list)
    output_refs: list[str] = Field(default_factory=list)
    model_role: str | None = None
    model_name: str | None = None
    prompt_version_id: str | None = None
    asset_id: str | None = None
    decision: str | None = None
    retry_count: int = Field(default=0, ge=0)
    error: TraceError | None = None


class PromptVersion(StrictBaseModel):
    """Versioned prompt text with traceable composition metadata."""

    prompt_version_id: str = Field(default_factory=lambda: new_id("prompt"))
    task_id: str
    confirmation_doc_id: str
    style_id: str
    category_id: str
    style_idea_id: str | None = None
    template_version: str = "render_prompt_v1"
    prompt_text: str
    variables: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class CandidateAsset(StrictBaseModel):
    """Rendered candidate image asset record."""

    asset_id: str = Field(default_factory=lambda: new_id("asset"))
    task_id: str
    project_id: str
    prompt_version_id: str
    style_id: str
    category_id: str
    url: str
    version: str = "1.0"
    status: str = "candidate"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class DirectionSelection(StrictBaseModel):
    """Human-selected master candidate from exactly five initial candidates."""

    selection_id: str = Field(default_factory=lambda: new_id("direction_selection"))
    task_id: str
    selected_asset_ids: list[str] = Field(min_length=1, max_length=1)
    selected_by: str
    created_at: datetime = Field(default_factory=utc_now)


class MasterCandidateLock(StrictBaseModel):
    """Locked master candidate selected from primary directions."""

    lock_id: str = Field(default_factory=lambda: new_id("master_lock"))
    task_id: str
    asset_id: str
    locked_by: str
    created_at: datetime = Field(default_factory=utc_now)


class VisualCheckResult(StrictBaseModel):
    """Structured VLM visual self-calibration result."""

    passed: bool
    deviations: list[str] = Field(default_factory=list)
    rework_prompt_delta: str = ""
    model_name: str = "deterministic-visual-checker"
    decision: Literal["continue", "pass", "blocked"] = "continue"
    issues: list[dict[str, Any]] = Field(default_factory=list)
    preserve: list[str] = Field(default_factory=list)
    stop_reason: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class WorkflowState(str, Enum):
    """Recoverable states in the 5-to-1 workflow."""

    INTAKE_CLARIFY = "intake_clarify"
    CONFIRMATION_BUILD = "confirmation_build"
    INITIAL_CANDIDATE_GENERATION = "initial_candidate_generation"
    MASTER_CANDIDATE_SELECTION = "master_candidate_selection"
    SELF_CHECK_ITERATION = "self_check_iteration"
    HUMAN_PROMPT_ITERATION = "human_prompt_iteration"
    FINAL_APPROVAL = "final_approval"


class ReferenceImage(StrictBaseModel):
    """Ordered image reference and its reason for inclusion."""

    uri: str
    role: Literal["current", "base", "style", "content"]
    source: str
    sha256: str
    order: int = Field(ge=0)
    reason: str


class CalibrationAttempt(StrictBaseModel):
    """One visual calibration attempt for a candidate asset."""

    attempt_id: str = Field(default_factory=lambda: new_id("calibration_attempt"))
    task_id: str
    asset_id: str
    retry_count: int = Field(ge=0)
    check_result: VisualCheckResult
    created_at: datetime = Field(default_factory=utc_now)


class FinalApprovalRecord(StrictBaseModel):
    """Final human approval or manual override decision."""

    approval_id: str = Field(default_factory=lambda: new_id("final_approval"))
    task_id: str
    approved: bool
    actor: str
    override_prompt: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
