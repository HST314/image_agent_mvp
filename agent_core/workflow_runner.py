"""Production workflow runner and explicit state-handler registry."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from agent_core.batch import CandidateBatchError, CandidateBatchGenerator
from agent_core.models import (ImageTaskCard, ModelRole, QuestionAnswer,
                               QuestionAnswerRecord, QuestionCard,
                               SpecificationFact, TaskSpecification,
                               VisualCheckResult)
from agent_core.state_machine import RecoverableWorkflow
from agent_core.workflow import SelfCheckPolicy, validate_transition
from agent_core.unified_workflow import (DomainState, classify_error, freeze_delivery,
                                         recovery_actions, revise_task, TaskRevision)
from calibrator.calibration_loop import CalibrationLoop, ManualAction
from interaction.confirmation_builder import (
    build_confirmation_doc,
    specification_from_task,
    specification_to_markdown,
    specification_value,
    update_specification_from_markdown,
)
from interaction.question_generator import generate_question_card, resolve_unknown_field
from model_router.clients import build_text_client, build_vlm_client
from model_router.gateway import RuntimeModelGateway
from model_router.router import ModelRoute, ModelRouter
from render_clients.ark_client import ArkImageRenderClient
from render_clients.payload_mapper import build_render_payload
from storage.project_store import ProjectStore, content_hash
from storage.assets import normalize_image_asset
from storage.provider_assets import ProviderImageAdapter
from interaction.presenter import Presenter
from configs.runtime_policy import RuntimePolicy
from model_router.executor import ModelExecutor
from skills.resource_loader import load_with_policy
from skills.errors import ResourceError
from calibrator.structured_inspection import parse_with_one_repair, InspectionOutputError
from agent_core.delivery import build_delivery, persist_delivery

Handler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass
class RunnerOptions:
    selected_id: str | None = None
    manual_action: ManualAction | None = None
    human_prompt: str | None = None
    edited_markdown: str | None = None
    final_approved: bool = False
    clarification_answers: dict[str, Any] | None = None
    task_approved: bool = False
    actor: str | None = None
    skill_action: Literal["approve", "retry"] | None = None
    category_action: Literal["approve", "retry"] | None = None
    clarification_action: Literal["apply_safe_defaults", "continue_after_budget_change"] | None = None


class WorkflowRunner:
    """Run registered real handlers and checkpoint every successful boundary."""

    ORDER = ("category_constraint", "intake_clarify", "confirmation_build", "initial_candidate_generation",
             "master_candidate_selection", "self_check_iteration", "human_prompt_iteration", "final_approval")
    DOMAIN_TARGET = {
        "category_constraint": DomainState.TASK,
        "intake_clarify": DomainState.CLARIFICATION,
        "confirmation_build": DomainState.TASK_APPROVAL,
        "initial_candidate_generation": DomainState.FIVE_RENDER,
        "master_candidate_selection": DomainState.MASTER_SELECTION,
        "self_check_iteration": DomainState.QUALITY_REWORK,
        "human_prompt_iteration": DomainState.HUMAN_EDIT,
        "final_approval": DomainState.DELIVERY_FROZEN,
    }

    def __init__(self, store: ProjectStore, config: Path, *, offline_mode: bool = False,
                 output: Callable[[str], None] | None = None) -> None:
        self.store = store
        policy_file = store.root / "runtime_policy.json"
        stored = json.loads(policy_file.read_text(encoding="utf-8")).get("policy", {}) if policy_file.exists() else {}
        self.policy = RuntimePolicy.model_validate(stored) if stored else RuntimePolicy(offline_mode=offline_mode)
        if stored and self.policy.offline_mode != offline_mode:
            raise ValueError("工程真实/离线模式已在创建时固化，运行中不可切换。")
        executor = ModelExecutor(max_attempts=self.policy.max_render_retries + 1,
                                 timeout=self.policy.model_timeout_seconds)
        self.gateway = RuntimeModelGateway(store, ModelRouter.from_file(config), executor, offline_mode=offline_mode)
        self.offline_mode = offline_mode
        self.output = output or (lambda _: None)
        self.presenter = Presenter()
        self.workflow = RecoverableWorkflow(store)
        self.provider_assets = ProviderImageAdapter(store)
        self.handlers: dict[str, Handler] = {
            "category_constraint": self._category_constraint,
            "intake_clarify": self._clarify, "confirmation_build": self._confirmation,
            "initial_candidate_generation": self._candidates, "master_candidate_selection": self._selection,
            "self_check_iteration": self._self_check, "human_prompt_iteration": self._human_rework,
            "final_approval": self._final,
        }

    def next_state(self, snapshot: dict[str, Any] | None) -> str:
        if snapshot is None or not snapshot.get("state"): return self.ORDER[0]
        phase = snapshot.get("phase")
        if phase in {"waiting_category_approval", "waiting_human_approval", "waiting_clarification",
                     "waiting_clarification_review", "waiting_master_selection",
                     "waiting_skill_approval", "skill_approved_pending_render"}:
            return str(snapshot.get("state"))
        if phase in {"additional_rounds_approved", "waiting_reinspection"}:
            return "self_check_iteration"
        if phase == "waiting_human_tune":
            return "human_prompt_iteration"
        current = str(snapshot.get("state", ""))
        if current not in self.ORDER: raise ValueError("检查点中的流程状态无法识别。")
        index = self.ORDER.index(current)
        if index + 1 >= len(self.ORDER): raise ValueError("工程已经完成最终确认。")
        return self.ORDER[index + 1]

    def run(self, snapshot: dict[str, Any] | None, options: RunnerOptions, *, only_state: str | None = None) -> dict[str, Any]:
        with self.store.lock():
            return self._run_locked(snapshot, options, only_state=only_state)

    def _run_locked(self, snapshot: dict[str, Any] | None, options: RunnerOptions, *, only_state: str | None = None) -> dict[str, Any]:
        data = dict(snapshot or {}); target = only_state or self.next_state(snapshot)
        if "task_card" in data and "domain_state" not in data:
            data["domain_state"] = DomainState.TASK.value
        while True:
            current = str(data.get("state", ""))
            if current and current != target:
                validate_transition(current, target)
            handler = self.handlers[target]
            self.store.start_step(target, input_hash=content_hash(data))
            try:
                result = handler(data, options.__dict__)
                data = {**data, **result, "state": target}
                if "domain_state" in data:
                    domain_target = self.DOMAIN_TARGET[target]
                    if target == "initial_candidate_generation" and result.get("phase") == "waiting_skill_approval":
                        domain_target = DomainState.SKILL_APPROVAL
                    self._advance_domain(data, domain_target)
                # Waiting is a successful recoverable boundary, not a failed state.
                self.store.checkpoint(target, data)
            except Exception as exc:
                category = classify_error(exc)
                actions = recovery_actions(category)
                can_retry = any(action in {"retry", "retry_after_confirmation"} for action in actions)
                self.store.fail_step(target, {"code": type(exc).__name__, "message": str(exc),
                                               "category": category, "retryable": can_retry,
                                               "recovery_actions": list(actions)})
                raise
            if only_state or data.get("waiting") or target == "final_approval": return data
            target = self.next_state(data)

    def _load_category_skill(self, task: ImageTaskCard, *, excluded: set[str] | None = None):
        """Resolve the category library before clarification, with safe generic fallback."""
        from skills.category_library_adapter import CategoryLibraryAdapter
        from skills.category_loader import CategorySkillLoader

        lib_path = Path(__file__).parent.parent / "skills/category_libraries/advertising_category_library_v2.json"
        generic_index = Path(__file__).parent.parent / "skills/category_skills/index.json"
        # An explicit task-card category is authoritative. Advertising-library
        # inference is only used when no category has been selected yet.
        if task.category_ref is not None:
            return CategorySkillLoader(generic_index).load_for_task(task), 0
        try:
            match = CategoryLibraryAdapter(lib_path).load_for_task(
                task,
                exclude_category_ids=excluded or set(),
                allow_unmatched=bool(excluded),
            )
        except (FileNotFoundError, UnicodeError, json.JSONDecodeError) as exc:
            from uuid import uuid4
            raise ResourceError(
                "RESOURCE_MISSING" if isinstance(exc, FileNotFoundError) else "RESOURCE_CORRUPT",
                str(lib_path), f"trace_{uuid4().hex}",
            ) from exc
        if match:
            return match.skill, match.score
        return CategorySkillLoader(generic_index).load_for_task(task), 0

    @staticmethod
    def _has_fact_value(value: Any) -> bool:
        return not isinstance(value, dict) and str(value or "").strip().casefold() not in {
            "", "unknown", "pending", "待确认", "待补充", "未提供",
        }

    @classmethod
    def _apply_category_unknowns(cls, task: ImageTaskCard, skill: Any) -> ImageTaskCard:
        """Inject category requirements and reconcile answers to internal ids."""

        unknowns = dict(task.unknowns)
        known_facts = dict(task.known_facts)
        alias_owners: dict[str, set[str]] = {}
        for item in skill.required_questions:
            field = str(item.field)
            existing = unknowns.get(field)
            aliases = {field, str(item.question or "")}
            if isinstance(existing, dict):
                aliases.update(str(existing.get(key) or "") for key in ("label", "question"))
            for alias in aliases:
                key = "".join(alias.casefold().split())
                if key:
                    alias_owners.setdefault(key, set()).add(field)
        for item in skill.required_questions:
            if str(item.field) == "asset_rules" and not task.asset_inputs:
                continue
            field = str(item.field)
            existing = unknowns.get(field)
            aliases = {field, str(item.question or "")}
            if isinstance(existing, dict):
                aliases.update(str(existing.get(key) or "") for key in ("label", "question"))
            normalized_aliases = {
                key for alias in aliases
                if (key := "".join(alias.casefold().split()))
                and alias_owners.get(key) == {field}
            }
            answer = next((
                value for key, value in known_facts.items()
                if "".join(str(key).casefold().split()) in normalized_aliases
                and cls._has_fact_value(value)
            ), None)
            if answer is not None:
                known_facts[field] = answer
                for unknown_key, details in list(unknowns.items()):
                    candidate_aliases = {str(unknown_key)}
                    if isinstance(details, dict):
                        candidate_aliases.update(
                            str(details.get(key) or "") for key in ("label", "question")
                        )
                    if any("".join(alias.casefold().split()) in normalized_aliases
                           for alias in candidate_aliases if alias):
                        unknowns.pop(unknown_key, None)
                continue
            unknowns.setdefault(field, {
                "question": item.question,
                "label": item.question,
                "blocking": bool(item.blocks_generation),
                "has_safe_default": not bool(item.blocks_generation),
                "impact": "该品类的制作、交付或验收依赖此信息。",
                "evidence": f"广告品类库：{skill.display_name or skill.category_id}",
                "default_handling": item.default_handling,
                "options": [
                    {"label": "现在补充（请注明）", "description": f"提供“{item.question}”的可执行内容"},
                    {"label": "采用明确默认（请注明）", "description": "写明经人工确认的保守默认值"},
                ],
            })
        return task.model_copy(update={"known_facts": known_facts, "unknowns": unknowns})

    def _category_constraint(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        """Match, version and optionally approve category constraints before clarification."""
        from agent_core.models import CategorySkill

        task = ImageTaskCard.model_validate(data["task_card"])
        action = options.get("category_action")
        actor = options.get("actor")
        current = data.get("category_constraint_current") or {}
        history = [dict(item) for item in data.get("category_constraint_history", [])]

        if data.get("phase") == "waiting_category_approval" and action == "approve":
            if not actor:
                raise ValueError("品类约束放行需要操作者身份。")
            history[-1] = {**history[-1], "decision": "approved", "decided_by": actor}
            current = history[-1]
            task = self._apply_category_unknowns(
                task, CategorySkill.model_validate(current["skill"]),
            )
            self.store.events.append("category_constraint_approved", actor=actor,
                                     version_id=current["version_id"])
            return {"category_constraint_current": current, "category_constraint_history": history,
                    "category_constraint_approval": {"version_id": current["version_id"], "actor": actor},
                    "task_card": task.model_dump(mode="json"), "waiting": False,
                    "phase": "category_approved"}
        if data.get("phase") == "waiting_category_approval" and action is None:
            return {"waiting": True, "phase": "waiting_category_approval"}
        if action not in {None, "retry"}:
            raise ValueError("当前品类约束处置动作无效。")
        if action == "retry" and not actor:
            raise ValueError("品类约束换版需要操作者身份。")

        excluded = {str(current.get("category_id"))} if action == "retry" and current.get("category_id") else set()
        skill, score = self._load_category_skill(task, excluded=excluded)
        if action == "retry" and history:
            history[-1] = {**history[-1], "decision": "rejected", "decided_by": actor}
        version_number = len(history) + 1
        decision = "auto_approved" if self.policy.category_constraint.release == "auto" else "pending"
        version = {
            "version_id": f"category-constraint-v{version_number}", "version": version_number,
            "category_id": skill.category_id, "category_name": skill.display_name or "通用视觉交付",
            "score": score, "decision": decision, "skill": skill.model_dump(mode="json"),
            "constraint_hash": content_hash(skill.model_dump(mode="json")),
        }
        history.append(version)
        self.store.events.append("category_constraint_matched", version_id=version["version_id"],
                                 category_id=skill.category_id, score=score,
                                 release=self.policy.category_constraint.release)
        if self.policy.category_constraint.release == "manual":
            return {"category_constraint_current": version, "category_constraint_history": history,
                    "category_constraint_approval": None, "waiting": True,
                    "phase": "waiting_category_approval"}
        task = self._apply_category_unknowns(task, skill)
        return {"category_constraint_current": version, "category_constraint_history": history,
                "category_constraint_approval": {"version_id": version["version_id"], "actor": "system:auto"},
                "task_card": task.model_dump(mode="json"), "waiting": False,
                "phase": "category_approved"}

    @staticmethod
    def _advance_domain(data: dict[str, Any], target: DomainState) -> None:
        """Advance every production handler through the canonical graph."""
        from agent_core.unified_workflow import FLOW, require_transition
        current = DomainState(data["domain_state"])
        if FLOW.index(target) < FLOW.index(current):
            return
        while current != target:
            following = FLOW[FLOW.index(current) + 1]
            require_transition(current, following, data)
            current = following
        data["domain_state"] = current.value

    @staticmethod
    def _clarification_asked_fields(task: ImageTaskCard, data: dict[str, Any]) -> set[str]:
        fields = {str(field) for field in data.get("clarification_asked_fields", []) if field}
        cards = [item.get("question_card", {}) for item in data.get("clarification_transcript", [])]
        if data.get("phase") == "waiting_clarification":
            cards.append(data.get("question_card") or {})
        for card in cards:
            for question in card.get("questions", []) if isinstance(card, dict) else []:
                raw_field = question.get("field") if isinstance(question, dict) else None
                canonical = resolve_unknown_field(task, raw_field)
                if canonical:
                    fields.add(canonical)
                elif raw_field and (str(raw_field) in task.known_facts
                                    or str(raw_field).startswith("library_required_input_")):
                    fields.add(str(raw_field))
        return fields

    @classmethod
    def _apply_safe_defaults(cls, task: ImageTaskCard) -> tuple[ImageTaskCard, list[str]]:
        facts = dict(task.known_facts)
        unknowns = dict(task.unknowns)
        applied: list[str] = []
        for field, details in list(unknowns.items()):
            if not isinstance(details, dict) or not bool(details.get("has_safe_default")):
                continue
            value = details.get("default_value")
            handling = str(details.get("default_handling") or "")
            if not value and not any(marker in handling for marker in ("保持未确认", "不得", "禁止")):
                value = handling
            value = value or "采用经人工确认的保守默认值"
            if not cls._has_fact_value(value):
                continue
            facts[field] = str(value)
            unknowns.pop(field, None)
            applied.append(field)
        return task.model_copy(update={"known_facts": facts, "unknowns": unknowns}), applied

    def _clarification_review_result(
        self, task: ImageTaskCard, *, transcript: list[dict[str, Any]],
        fingerprints: set[str], asked_fields: set[str], asked_count: int,
        invalidated: dict[str, Any],
    ) -> dict[str, Any]:
        blockers = self._blocking_unknowns(task)
        safe_defaults = [
            field for field, details in task.unknowns.items()
            if isinstance(details, dict) and bool(details.get("has_safe_default"))
        ]
        # Manual supplementation is intentionally outside the automatic budget.
        # It remains bounded to three fields per card and never skips a blocker.
        card = generate_question_card(
            task, previous_fingerprints=set(), already_asked=0,
            total_budget=3, max_auto_questions=3,
        )
        recovery_actions = ["supplement_remaining", "increase_budget"]
        if safe_defaults:
            recovery_actions.insert(1, "apply_safe_defaults")
        asked = max(len(asked_fields), asked_count)
        return {
            **invalidated,
            "question_card": card.model_dump(mode="json"),
            "waiting": True,
            "phase": "waiting_clarification_review",
            "task_card": task.model_dump(mode="json"),
            "clarification_transcript": transcript,
            "previous_fingerprints": sorted(fingerprints),
            "clarification_asked_fields": sorted(asked_fields),
            "clarification_asked_count": asked,
            "clarification_remaining_budget": max(
                0, self.policy.clarification_total_budget - asked
            ),
            "clarification_blocking_fields": blockers,
            "clarification_safe_default_fields": safe_defaults,
            "clarification_recovery_actions": recovery_actions,
            "clarification_review_reason": "自动澄清预算已耗尽，仍有阻塞项需要人工处理。",
        }

    def _clarify(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        task = ImageTaskCard.model_validate(data["task_card"])
        asked_fields = self._clarification_asked_fields(task, data)
        category = (data.get("category_constraint_current") or {}).get("skill")
        if category:
            from agent_core.models import CategorySkill
            task = self._apply_category_unknowns(task, CategorySkill.model_validate(category))
        fingerprints = set(data.get("previous_fingerprints", []))
        legacy_count = int(data.get("clarification_asked_count", 0))
        already_asked = len(asked_fields) if asked_fields else legacy_count
        transcript = list(data.get("clarification_transcript", []))
        invalidated: dict[str, Any] = {}
        phase = data.get("phase")
        if phase in {"waiting_clarification", "waiting_clarification_review"}:
            answers = options.get("clarification_answers")
            action = options.get("clarification_action")
            if answers:
                card = QuestionCard.model_validate(data["question_card"])
                record, resolved = self._answer_record(task, card, answers)
                facts = {**task.known_facts, **resolved}
                task = task.model_copy(update={
                    "known_facts": facts,
                    "unknowns": {key: value for key, value in task.unknowns.items()
                                 if key not in resolved},
                })
                transcript.append({"question_card": card.model_dump(mode="json"),
                                   "answer_record": record.model_dump(mode="json")})
                invalidated = {"task_specification": None, "task_markdown": None,
                               "task_revision": None, "task_approval": None}
            elif phase == "waiting_clarification":
                return {"waiting": True, "phase": "waiting_clarification"}
            elif action == "apply_safe_defaults":
                task, applied = self._apply_safe_defaults(task)
                if applied:
                    self.store.events.append("clarification_safe_defaults_applied", fields=applied)
                invalidated = {"task_specification": None, "task_markdown": None,
                               "task_revision": None, "task_approval": None}
            elif action != "continue_after_budget_change":
                return self._clarification_review_result(
                    task, transcript=transcript, fingerprints=fingerprints,
                    asked_fields=asked_fields, asked_count=already_asked,
                    invalidated=invalidated,
                )

        remaining = max(0, self.policy.clarification_total_budget - already_asked)
        if remaining == 0 and self._blocking_unknowns(task):
            return self._clarification_review_result(
                task, transcript=transcript, fingerprints=fingerprints,
                asked_fields=asked_fields, asked_count=already_asked,
                invalidated=invalidated,
            )
        if self.offline_mode:
            card = generate_question_card(
                task, previous_fingerprints=fingerprints, already_asked=already_asked,
                total_budget=self.policy.clarification_total_budget,
                max_auto_questions=self.policy.max_auto_questions,
            )
        else:
            card = self.gateway.call(
                "intake_clarify", ModelRole.REASONING_LLM,
                lambda route: generate_question_card(
                    task, self._text(route), previous_fingerprints=fingerprints,
                    already_asked=already_asked,
                    error_recorder=lambda error: self.store.events.append(
                        **{"event_type": "model_parse_failed", **error}
                    ),
                ),
                messages=[{"role": "user", "content": "结合已批准的广告品类约束与完整多轮问答重新分析；品类阻塞项未闭合时必须提问，只有完整时才能返回 0 问。"}],
                variables={"task": task.model_dump(mode="json"),
                           "clarification_transcript": transcript,
                           "category_constraint": data.get("category_constraint_current")},
                template_id="clarification", template_version="2",
                input_refs=[ref.ref_id for ref in task.source_refs],
            )
        # The model cannot waive deterministic category blockers. If it returns
        # zero questions, synthesize the next bounded question card locally.
        if not card.questions and self._blocking_unknowns(task):
            card = generate_question_card(
                task, previous_fingerprints=fingerprints, already_asked=already_asked,
                total_budget=self.policy.clarification_total_budget,
                max_auto_questions=self.policy.max_auto_questions,
            )
        if card.questions:
            fingerprints.update(question.semantic_fingerprint for question in card.questions)
            asked_fields.update(question.field for question in card.questions)
            asked = len(asked_fields) if asked_fields else already_asked + len(card.questions)
            return {
                **invalidated,
                "question_card": card.model_dump(mode="json"),
                "waiting": True,
                "phase": "waiting_clarification",
                "task_card": task.model_dump(mode="json"),
                "clarification_transcript": transcript,
                "previous_fingerprints": sorted(fingerprints),
                "clarification_asked_fields": sorted(asked_fields),
                "clarification_asked_count": asked,
                "clarification_remaining_budget": max(
                    0, self.policy.clarification_total_budget - asked
                ),
                "clarification_blocking_fields": None,
                "clarification_safe_default_fields": None,
                "clarification_recovery_actions": None,
                "clarification_review_reason": None,
            }
        if self._blocking_unknowns(task):
            return self._clarification_review_result(
                task, transcript=transcript, fingerprints=fingerprints,
                asked_fields=asked_fields, asked_count=already_asked,
                invalidated=invalidated,
            )
        asked = len(asked_fields) if asked_fields else already_asked
        return {
            **invalidated,
            "question_card": card.model_dump(mode="json"),
            "waiting": False,
            "phase": "ready_to_draft",
            "task_card": task.model_dump(mode="json"),
            "clarification_transcript": transcript,
            "previous_fingerprints": sorted(fingerprints),
            "clarification_asked_fields": sorted(asked_fields),
            "clarification_asked_count": asked,
            "clarification_remaining_budget": max(
                0, self.policy.clarification_total_budget - asked
            ),
            "clarification_blocking_fields": None,
            "clarification_safe_default_fields": None,
            "clarification_recovery_actions": None,
            "clarification_review_reason": None,
        }

    def _confirmation(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        task = ImageTaskCard.model_validate(data["task_card"])
        if self._blocking_unknowns(task):
            raise ValueError("仍有阻塞未知项，禁止生成可批准任务书。")
        if data.get("task_specification"):
            spec = TaskSpecification.model_validate(data["task_specification"])
            # The first pass is authored by the reasoning model.  Re-entering the
            # approval gate must preserve that exact document instead of silently
            # replacing it with the deterministic schema renderer.
            markdown = str(data.get("task_markdown") or specification_to_markdown(spec))
        elif self.offline_mode:
            spec = specification_from_task(task)
            markdown = specification_to_markdown(spec)
        else:
            transcript = list(data.get("clarification_transcript", []))
            last = transcript[-1] if transcript else None
            card = QuestionCard.model_validate(last["question_card"]) if last else QuestionCard(task_id=task.task_id, questions=[])
            record = QuestionAnswerRecord.model_validate(last["answer_record"]) if last else QuestionAnswerRecord(question_card_id=card.question_card_id, task_id=task.task_id, answers=[])
            doc = self.gateway.call("confirmation_build", ModelRole.REASONING_LLM,
                lambda route: build_confirmation_doc(task, card, record, self._text(route), allow_fallback=False),
                messages=[{"role":"user","content":"基于任务卡、来源材料和完整问答，重新理解并撰写创作任务书。"}],
                variables={"task":task.model_dump(mode="json"), "clarification_transcript":transcript},
                template_id="confirmation_build", template_version="3", input_refs=[r.ref_id for r in task.source_refs])
            if any(u.risk_level.value == "blocking" for u in doc.default_handling_for_unknowns):
                raise ValueError("推理模型生成的任务书仍包含阻塞待决项，禁止进入人工批准。")
            if self._markdown_has_unresolved_items(doc.markdown_body):
                raise ValueError("任务书正文仍包含待确认或待补充事项，禁止进入人工批准。")
            facts = [SpecificationFact(label=f.field, value=str(f.value), provenance=f.source_ref, status="confirmed") for f in doc.confirmed_facts]
            facts.extend(SpecificationFact(label=u.field, value=u.handling, provenance="reasoning_llm",
                status="blocking" if u.risk_level.value == "blocking" else "tentative") for u in doc.default_handling_for_unknowns)
            spec = TaskSpecification(task_id=task.task_id, facts=facts).finalized()
            markdown = doc.markdown_body
        if options.get("edited_markdown") is not None:
            spec = update_specification_from_markdown(spec, options["edited_markdown"])
            markdown = specification_to_markdown(spec)
        if self._markdown_has_unresolved_items(markdown):
            raise ValueError("任务书正文仍包含待确认或待补充事项，禁止批准。")
        history = list(data.get("task_revision_history", []))
        raw_task = json.dumps(data.get("task_card"), ensure_ascii=False, sort_keys=True)
        actor = options.get("actor") or "manual-user"
        revisions = [TaskRevision.model_validate(item) for item in history]
        candidate = revise_task(revisions, raw_task, markdown, actor)
        if history and history[-1]["raw_task"] == raw_task and history[-1]["task_markdown"] == markdown:
            revision = history[-1]
        else:
            revision = candidate.model_dump(mode="json")
            history.append(revision)
            self.store.events.append("task_revision_created", revision=revision)
        revision_hash = revision["revision_hash"]
        approved = bool(options.get("task_approved") and options.get("actor"))
        return {"task_specification": spec.model_dump(mode="json"), "task_markdown": markdown,
                "readiness": {"ready": True, "blocking_items": [],
                              "category_constraint_hash": (data.get("category_constraint_current") or {}).get("constraint_hash")},
                "task_revision": revision,
                "task_revision_history": history,
                "task_approval": ({"revision_hash": revision_hash, "actor": options["actor"]} if approved else None),
                "waiting": not approved, "phase": "task_approved" if approved else "waiting_human_approval"}

    @staticmethod
    def _blocking_unknowns(task: ImageTaskCard) -> list[str]:
        return [field for field, value in task.unknowns.items()
                if isinstance(value, dict) and bool(value.get("blocking")) and not bool(value.get("has_safe_default"))]

    @staticmethod
    def _markdown_has_unresolved_items(markdown: str) -> bool:
        """Reject prose that contradicts an empty structured readiness result."""
        marker = re.compile(r"待确认|待补充|未提供|需后续|尚未明确")
        negated = re.compile(r"(?:无|没有|不存在).{0,8}(?:待确认|待补充|待决)|(?:待确认|待补充|待决).{0,4}(?:无|：无)")
        return any(marker.search(line) and not negated.search(line)
                   for line in str(markdown or "").splitlines())

    @staticmethod
    def _answer_record(task: ImageTaskCard, card: QuestionCard, payload: dict[str, Any]) -> tuple[QuestionAnswerRecord, dict[str, str]]:
        raw_answers = payload.get("answers") if isinstance(payload, dict) else None
        if isinstance(raw_answers, list) and payload.get("question_card_id") != card.question_card_id:
            raise ValueError("回答的问题卡已失效，请刷新后重新填写。")
        if not isinstance(raw_answers, list):
            # Temporary compatibility for older API clients; new clients must send structured answers.
            raw_answers = [{"question_id": q.question_id, "selected_option_id": None,
                            "free_text": payload.get(q.field)} for q in card.questions if q.field in payload]
        by_id = {q.question_id: q for q in card.questions}
        answers: list[QuestionAnswer] = []
        resolved: dict[str, str] = {}
        for raw in raw_answers:
            answer = QuestionAnswer.model_validate(raw)
            question = by_id.get(answer.question_id)
            if question is None:
                raise ValueError(f"回答引用了未知问题：{answer.question_id}")
            option = next((o for o in question.options if o.option_id == answer.selected_option_id), None)
            if answer.selected_option_id is not None and option is None:
                raise ValueError(f"回答引用了未知选项：{answer.selected_option_id}")
            free_text = (answer.free_text or "").strip()
            if option and option.requires_free_text and not free_text:
                raise ValueError(f"选项“{option.label}”必须填写具体内容。")
            if not answer.skipped:
                value = free_text or (option.description if option else "")
                if not value:
                    raise ValueError(f"问题“{question.question}”缺少有效回答。")
                canonical_field = resolve_unknown_field(task, question.field)
                if canonical_field is None and not task.unknowns:
                    # Compatibility for old checkpoints that omitted the
                    # structured unknown entry but retained a valid question.
                    canonical_field = question.field
                if canonical_field is None:
                    raise ValueError(f"问题字段无法对应当前任务卡：{question.question}")
                resolved[canonical_field] = value
            answers.append(answer)
        if len(answers) != len(card.questions) or {a.question_id for a in answers} != set(by_id):
            raise ValueError("必须逐项提交当前问题卡的结构化回答。")
        return QuestionAnswerRecord(question_card_id=card.question_card_id,
            task_id=task.task_id, answers=answers), resolved

    def _prepare_skill_invocations(self, data: dict[str, Any], *, retry_actor: str | None = None) -> dict[str, Any]:
        """Prepare the post-taskbook style direction; category is already frozen."""
        spec = TaskSpecification.model_validate(data["task_specification"])
        task_card = ImageTaskCard.model_validate(data["task_card"])
        previous = data.get("skill_invocation_current") or {}
        previous_invocations = previous.get("skill_invocations") or data.get("skill_invocations") or {}
        previous_style_ids = [
            str(item.get("style_id"))
            for item in previous_invocations.get("style_library", {}).get("selections", [])
            if item.get("style_id")
        ]
        avoidance_context = None
        if retry_actor:
            avoidance_context = {
                "instruction": "上一版艺术风格结果已被人工否决；重新检索时必须避开上一版五张风格卡。",
                "previous_version_id": previous.get("version_id"),
                "excluded_category_ids": [],
                "excluded_style_ids": previous_style_ids,
                "actor": retry_actor,
            }

        # New projects freeze this before clarification. Legacy checkpoints are
        # lazily upgraded here so historical branches remain executable.
        from agent_core.models import CategorySkill
        category_version = data.get("category_constraint_current") or {}
        if category_version.get("skill"):
            approval = data.get("category_constraint_approval") or {}
            if (not str(category_version.get("version_id", "")).startswith("category-constraint-legacy")
                    and approval.get("version_id") != category_version.get("version_id")):
                raise ValueError("品类约束修改后必须重新放行，才能进入艺术风格阶段。")
            category_skill = CategorySkill.model_validate(category_version["skill"])
        else:
            category_skill, score = self._load_category_skill(task_card)
            category_version = {
                "version_id": "category-constraint-legacy-v1", "version": 1,
                "category_id": category_skill.category_id,
                "category_name": category_skill.display_name or "通用视觉交付",
                "score": score, "decision": "legacy_auto_approved",
                "skill": category_skill.model_dump(mode="json"),
                "constraint_hash": content_hash(category_skill.model_dump(mode="json")),
            }

        # 2. 唯一风格入口：图片只在 VLM 提取边界出现，渲染侧只收到文字。
        from agent_core.models import ConfirmedFact, RiskLevel, SignStatus, TaskConfirmationDoc, UnknownHandling
        from agent_core.style_pipeline import StyleRenderPlanner
        from skills.style_library import StyleExtractor, StyleLibrary, select_five

        if not data.get("task_approval") or data["task_approval"].get("revision_hash") != data.get("task_revision", {}).get("revision_hash"):
            raise ValueError("任务书修改后必须重新人工确认，才能进入付费步骤。")
        style_root = Path(self.policy.style_library_root)
        if not style_root.is_absolute():
            configured = Path.cwd() / style_root
            if configured.exists():
                style_root = configured
            else:
                from skills.builtin_style_library import ensure_builtin_style_library
                style_root = ensure_builtin_style_library(self.store.root / "runtime" / "style-library-v1")
        library = StyleLibrary(style_root)
        records = library.records()
        extractor = StyleExtractor(style_root, self._extract_style, model_id="offline-style-vlm" if self.offline_mode else "runtime-style-vlm")
        def extraction_for(record):
            try:
                return library.extraction(record)
            except Exception:
                return extractor.extract(record)
        task_markdown = specification_to_markdown(spec)
        retrieval_text = task_markdown
        if avoidance_context:
            retrieval_text += "\n\n重试检索上下文：" + avoidance_context["instruction"]
        selected_styles = select_five(
            records,
            extraction_for,
            retrieval_text,
            exclude_style_ids=avoidance_context["excluded_style_ids"] if avoidance_context else (),
        )
        confirmed_facts = [
            ConfirmedFact(field=fact.label, value=fact.value, source_ref=fact.provenance)
            for fact in spec.facts if fact.status in {"confirmed", "extracted"}
        ]
        unknown_handling = [
            UnknownHandling(
                field=fact.label,
                handling=fact.value,
                risk_level=RiskLevel.BLOCKING if fact.status == "blocking" else RiskLevel.MEDIUM,
            )
            for fact in spec.facts if fact.status in {"tentative", "blocking"}
        ]
        forbidden_items = [
            item.strip()
            for fact in spec.facts if fact.label == "forbidden_items"
            for item in fact.value.replace("，", "；").split("；") if item.strip()
        ]
        doc = TaskConfirmationDoc(task_id=task_card.task_id, summary="已批准任务书全文见下方。",
                                  confirmed_facts=confirmed_facts,
                                  default_handling_for_unknowns=unknown_handling,
                                  forbidden_items=forbidden_items,
                                  markdown_body=task_markdown, sign_status=SignStatus.APPROVED,
                                  signed_by=data["task_approval"]["actor"])
        plans = StyleRenderPlanner().plan(confirmation=doc, category=category_skill, styles=selected_styles,
            deliverable_goal=specification_value(spec, "deliverable_goal", task_card.deliverable_goal),
            usage_context=specification_value(spec, "usage_context", task_card.usage_context),
            task_revision_hash=data["task_revision"]["revision_hash"], config_hash=self.policy.sha256())

        # 保存本次技能调用的可读结果。风格原图仅作为工程内只读展示资产持久化，
        # 不进入生图 payload；最终渲染边界仍由 assert_reference_isolated 兜底。
        style_invocations = []
        for selected in selected_styles:
            reference_path = style_root / selected.style.image
            reference_asset = self.store.artifacts.save_bytes(
                reference_path.read_bytes(),
                suffix=reference_path.suffix,
                metadata={"kind": "style_reference", "style_id": selected.style.style_id},
            )
            extraction = selected.extraction
            style_invocations.append({
                "style_id": selected.style.style_id,
                "style_name": selected.style.title,
                "description": selected.style.describe,
                "reference_asset": reference_asset,
                "reason": selected.reason,
                "task_fit": selected.task_fit,
                "mechanism": selected.mechanism,
                "risk": selected.risk,
                "artistic_interpretation": (
                    f"画面以{extraction.composition}组织视觉重心，结合{extraction.material}与"
                    f"{extraction.lighting}形成质感；叙事倾向{extraction.narrative}，"
                    f"图形语言为{extraction.graphic_language}，色彩特征为{extraction.color}。"
                    f"{extraction.prompt_supplement}"
                ),
                "analysis": {
                    "composition": extraction.composition,
                    "material": extraction.material,
                    "lighting": extraction.lighting,
                    "narrative": extraction.narrative,
                    "graphic_language": extraction.graphic_language,
                    "color": extraction.color,
                },
            })
        skill_invocations = {
            "category_library": {
                "source": "广告品类库",
                "category_id": category_skill.category_id,
                "category_name": category_skill.display_name or "通用视觉交付",
                "version": category_skill.version,
                "description": category_skill.prompt_injection.category_description,
                "production_constraints": category_skill.prompt_injection.production_constraints,
                "visual_rules": category_skill.prompt_injection.visual_rules,
                "forbidden_elements": category_skill.prompt_injection.forbidden_elements,
                "review_checks": category_skill.review_checks,
            },
            "style_library": {
                "source": "艺术风格库",
                "selections": style_invocations,
            },
            "avoidance_context": avoidance_context,
        }

        # 3. 终端输出这 5 张文本卡片给用户
        self.output("\n=================== 🎨 筛选出 5 种艺术风格方向 ===================")
        for i, selected in enumerate(selected_styles, 1):
            self.output(f"\n【方向 {i}：{selected.style.title}】")
            self.output(f"  • 主导机制：{selected.mechanism}")
            self.output(f"  • 推荐理由：{selected.reason}")
            self.output(f"  • 主要风险：{selected.risk}")
        self.output("\n=================================================================")
        self.output("保持主体内容、品牌色彩与空间条件一致；技能调用结果已准备完成。\n")

        style_selections = [
            {"style_id": item.style.style_id, "extraction_key": item.extraction.extraction_key,
             "reason": item.reason, "task_fit": item.task_fit, "mechanism": item.mechanism, "risk": item.risk}
            for item in selected_styles
        ]
        render_plans = [
            {"slot": plan.slot, "style_id": plan.style_id, "extraction_key": plan.extraction_key,
             "prompt_version_id": plan.prompt_version_id, "prompt_text": plan.prompt_text,
             "provenance": plan.provenance}
            for plan in plans
        ]
        history = [dict(item) for item in data.get("skill_invocation_history", [])]
        if retry_actor and history:
            history[-1] = {**history[-1], "decision": "rejected", "decided_by": retry_actor}
        version_number = len(history) + 1
        version = {
            "version_id": f"skill-invocation-v{version_number}",
            "version": version_number,
            "decision": "auto_approved" if self._style_release() == "auto" else "pending",
            "skill_invocations": skill_invocations,
            "style_selections": style_selections,
            "render_plans": render_plans,
            "avoidance_context": avoidance_context,
        }
        history.append(version)
        self.store.events.append(
            "skill_invocation_completed",
            version_id=version["version_id"],
            release=self._style_release(),
            previous_version_id=(avoidance_context or {}).get("previous_version_id"),
            excluded_category_ids=(avoidance_context or {}).get("excluded_category_ids", []),
            excluded_style_ids=(avoidance_context or {}).get("excluded_style_ids", []),
        )
        return {
            "skill_invocations": skill_invocations,
            "style_selections": style_selections,
            "render_plans": render_plans,
            "skill_invocation_current": version,
            "skill_invocation_history": history,
            "category_constraint_current": category_version,
        }

    def _style_release(self) -> str:
        """Prefer the new style gate while honoring persisted legacy manual gates."""
        if self.policy.style_direction.release == "auto" and self.policy.skill_invocation.release == "manual":
            return "manual"
        return self.policy.style_direction.release

    def _render_candidates(self, data: dict[str, Any], prepared: dict[str, Any]) -> dict[str, Any]:
        """Cross the paid five-render boundary only after the skill gate is released."""
        spec = TaskSpecification.model_validate(data["task_specification"])
        plans = list(prepared.get("render_plans") or [])
        if len(plans) != 5 or len({plan.get("style_id") for plan in plans}) != 5:
            raise ValueError("技能调用结果必须包含五个不同且可生成的风格方案。")
        revision_hash = data.get("task_revision", {}).get("revision_hash")
        for plan in plans:
            provenance = plan.get("provenance") or {}
            if provenance.get("task_revision_hash") != revision_hash or provenance.get("config_hash") != self.policy.sha256():
                raise ValueError("技能调用结果已过期，请重新调用两库后再生成。")

        # 生图逻辑：固定内容与品牌色，只注入已通过门禁的五个文本方案。
        from render_clients.payload_mapper import validate_render_size
        image_binding = self.gateway.router.binding_for_state("initial_candidate_generation")
        validate_render_size(image_binding.model, self.policy.default_output_size)
        style_names = {
            item.get("style_id"): item.get("style_name")
            for item in prepared.get("skill_invocations", {}).get("style_library", {}).get("selections", [])
        }

        def render(index: int) -> dict[str, Any]:
            plan = plans[index]
            result = self._image_call("initial_candidate_generation", plan["prompt_text"], [], index=index)
            return {**normalize_image_asset(result), "candidate_index": index, "id": f"candidate-{index + 1}",
                    "style_name": style_names.get(plan["style_id"]) or f"风格方向 {index + 1}",
                    "style_id": plan["style_id"], "extraction_key": plan["extraction_key"],
                    "prompt_version_id": plan["prompt_version_id"], "provenance": plan["provenance"]}

        version_id = prepared.get("skill_invocation_current", {}).get("version_id")
        render_plan_hash = content_hash(plans)
        cache_scope = {"skill_version_id": version_id, "render_plan_hash": render_plan_hash}
        expected_assets = [
            {"style_id": plan["style_id"], "prompt_version_id": plan["prompt_version_id"],
             "provenance": plan["provenance"]}
            for plan in plans
        ]
        batch = CandidateBatchGenerator(self.store, render, attempts=1,
                                        max_workers=self.policy.candidate_concurrency).generate(
                                            spec.content_hash,
                                            cache_scope=cache_scope,
                                            expected_assets=expected_assets,
                                        )
        if batch["failed"]:
            raise CandidateBatchError(batch["failed"])
        return {**prepared, "candidates": batch["succeeded"], "waiting": False,
                "phase": "candidate_generation_completed"}

    def _candidates(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        phase = data.get("phase")
        action = options.get("skill_action")
        actor = options.get("actor")
        if phase == "skill_approved_pending_render":
            prepared = {
                "skill_invocations": data.get("skill_invocations") or {},
                "style_selections": data.get("style_selections") or [],
                "render_plans": data.get("render_plans") or [],
                "skill_invocation_current": data.get("skill_invocation_current") or {},
                "skill_invocation_history": [dict(item) for item in data.get("skill_invocation_history", [])],
                "skill_invocation_approval": data.get("skill_invocation_approval") or {},
            }
            return self._render_candidates(data, prepared)
        if action and phase != "waiting_skill_approval":
            raise ValueError("当前不在技能调用人工确认阶段。")

        if phase == "waiting_skill_approval":
            if self._style_release() == "manual" and not action:
                return {"waiting": True, "phase": "waiting_skill_approval"}
            if action not in {None, "approve", "retry"}:
                raise ValueError("技能调用处置动作无效。")
            if action in {"approve", "retry"} and not actor:
                raise ValueError("技能调用处置需要操作者身份。")
            if action == "retry":
                prepared = self._prepare_skill_invocations(data, retry_actor=actor)
                self.store.events.append(
                    "skill_invocation_retried",
                    actor=actor,
                    previous_version_id=data.get("skill_invocation_current", {}).get("version_id"),
                    version_id=prepared["skill_invocation_current"]["version_id"],
                )
                return {**prepared, "waiting": True, "phase": "waiting_skill_approval",
                        "skill_invocation_approval": None}

            prepared = {
                "skill_invocations": data.get("skill_invocations") or {},
                "style_selections": data.get("style_selections") or [],
                "render_plans": data.get("render_plans") or [],
                "skill_invocation_current": data.get("skill_invocation_current") or {},
                "skill_invocation_history": [dict(item) for item in data.get("skill_invocation_history", [])],
            }
            decision_actor = actor or "system:auto"
            if prepared["skill_invocation_history"]:
                prepared["skill_invocation_history"][-1] = {
                    **prepared["skill_invocation_history"][-1], "decision": "approved", "decided_by": decision_actor,
                }
            prepared["skill_invocation_current"] = {
                **prepared["skill_invocation_current"], "decision": "approved", "decided_by": decision_actor,
            }
            prepared["skill_invocation_approval"] = {
                "version_id": prepared["skill_invocation_current"].get("version_id"), "actor": decision_actor,
            }
            self.store.events.append(
                "skill_invocation_approved",
                actor=decision_actor,
                version_id=prepared["skill_invocation_current"].get("version_id"),
            )
            approved_boundary = {**data, **prepared, "waiting": False,
                                 "phase": "skill_approved_pending_render"}
            self.store.checkpoint("initial_candidate_generation", approved_boundary)
            return self._render_candidates(approved_boundary, prepared)

        prepared = self._prepare_skill_invocations(data)
        if self._style_release() == "manual":
            return {**prepared, "waiting": True, "phase": "waiting_skill_approval",
                    "skill_invocation_approval": None}
        prepared["skill_invocation_approval"] = {
            "version_id": prepared["skill_invocation_current"].get("version_id"), "actor": "system:auto",
        }
        return self._render_candidates(data, prepared)

    def _extract_style(self, image_path: str, prompt: str) -> Any:
        if self.offline_mode:
            return {"composition":"稳定构图", "material":"可控材质", "lighting":"均衡光影",
                    "narrative":"清晰叙事", "graphic_language":"差异化图形语言", "color":"任务色彩",
                    "prompt_supplement":"使用抽象视觉机制，不复制参考作品。"}
        return self.gateway.call("self_check_inspection", ModelRole.VISION_LANGUAGE_MODEL,
            lambda route: self._vlm(route).inspect(image_path, prompt),
            messages=[{"role":"user","content":prompt},{"role":"image","content":"[LOCAL_STYLE_ASSET]"}],
            variables={"style_asset_sha256": content_hash(Path(image_path).read_bytes().hex())},
            template_id="style-extraction", template_version="1", input_refs=[], needs_images=1)

    def _selection(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        selected = options.get("selected_id")
        if not selected: return {"waiting": True, "phase": "waiting_master_selection"}
        master = self.workflow.select_master(data["candidates"], selected)
        selection = {"candidate_id": selected, "artifact_id": master.get("artifact_id"),
                     "actor": options.get("actor") or "manual-user"}
        self.store.events.append("master_selected", selection=selection, asset=master)
        return {"master_asset": master, "selected_master": selection,
                "waiting": False, "phase": "master_selected"}

    @staticmethod
    def _fallback_style_cards():
        """Minimal policy-approved directions used only when explicit degradation is enabled."""
        from agent_core.models import SkillStatus, StyleCard, VisualLanguage
        compositions = ("centered", "diagonal", "grid", "asymmetric", "panoramic")
        return [StyleCard(style_id=f"degraded-{index}", version="1", style_name=f"安全方向 {index}",
                          composition=composition, visual_language=VisualLanguage(materiality=["neutral"]),
                          risk_notes=["外部风格资源不可用，需人工复核"], status=SkillStatus.APPROVED)
                for index, composition in enumerate(compositions, 1)]

    def _self_check(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        spec = TaskSpecification.model_validate(data["task_specification"])
        policy_data = data.get("self_check_policy", self.policy.self_check.model_dump(mode="json"))
        action = options.get("manual_action")
        at_round_limit = (data.get("phase") == "waiting_human_approval" and
                          "round_limit" in str(data.get("termination_reason")))
        if at_round_limit and action:
            asset = data.get("best_asset") or data.get("asset") or data["master_asset"]
            round_number = int(data.get("round", 0))
            if action.action == "accept_current":
                checked = data.get("asset") or asset
                self.store.events.append("quality_disposition", action="accept_current", selected_asset=checked)
                self.store.events.append("calibration_current_asset_accepted", round=round_number,
                                         asset_hash=checked["sha256"], decision=(data.get("inspection") or {}).get("decision"),
                                         policy=policy_data)
                return {"waiting": False, "phase": "calibration_completed", "round": round_number,
                        "asset": checked, "current_asset": checked, "calibration_status": "human_accepted",
                        "termination_satisfied": True, "termination_reason": "human_accepted_current_asset",
                        "latest_checked_asset_hash": checked["sha256"], "selected_policy": policy_data}
            if action.action == "end":
                self.store.events.append("quality_disposition", action="end", selected_asset=asset)
                self.store.events.append("calibration_terminated_without_delivery", round=round_number,
                                         asset_hash=asset["sha256"], decision=(data.get("inspection") or {}).get("decision"))
                return {"waiting": True, "phase": "terminated_without_delivery", "round": round_number,
                        "asset": asset, "current_asset": asset, "calibration_status": "terminated_without_delivery",
                        "termination_satisfied": False, "termination_reason": "human_ended_without_delivery",
                        "latest_checked_asset_hash": data.get("latest_checked_asset_hash"), "selected_policy": policy_data}
            if action.action == "human_tune_best":
                prompt = options.get("human_prompt")
                if not prompt:
                    raise ValueError("human_tune_best 必须同时提供 --human-prompt。")
                self.store.events.append("quality_disposition", action="human_tune_best", selected_asset=asset)
                return self._human_rework({**data, "asset": asset, "current_asset": asset}, options)
            if action.action == "add_rounds":
                if not action.cost_confirmed or action.additional_rounds < 1:
                    raise ValueError("add_rounds 必须提供正整数 --additional-rounds 并使用 --confirm-cost。")
                policy_data = dict(policy_data)
                policy_data["termination"] = "solo"
                policy_data["max_rounds"] = round_number + action.additional_rounds
                policy_data["fixed_rounds"] = min(int(policy_data.get("fixed_rounds", 1)), policy_data["max_rounds"])
                self.store.events.append("quality_disposition", action="add_rounds",
                                         additional_rounds=action.additional_rounds, cost_confirmed=True,
                                         selected_asset=asset)
                data = {**data, "asset": asset, "current_asset": asset, "round": round_number + 1,
                        "phase": "additional_rounds_approved", "available_actions": [],
                        "best_asset": None, "inspection": None, "termination_reason": None,
                        "termination_satisfied": False}
            elif action.action not in {"execute", "edit_and_execute", "skip"}:
                raise ValueError(f"轮次上限不支持处置动作：{action.action}")
        loop = CalibrationLoop(self.store, SelfCheckPolicy(**policy_data), inspector=self._inspect,
            reworker=lambda assembled: self._image_call("self_check_rework", assembled["text"], [r["uri"] for r in assembled["references"]]),
            presenter=lambda number, result: self._present_inspection(number, result))
        result = loop.run(current_asset=data.get("asset") or data["master_asset"], stable_specification=specification_to_markdown(spec),
                          constraints=[], approve=(lambda _: action) if action else None,
                          start_round=int(data.get("round", 1)), snapshot_context=data)
        result["inspection_asset"] = result.get("inspection_asset") or result.get("asset")
        if "round_limit" not in str(result.get("termination_reason")):
            result["available_actions"] = []
            result["best_asset"] = None
        return {**result, "current_asset": result.get("asset", data.get("master_asset"))}
    
    def _human_rework(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        # Human tuning is an optional graph stage. Automatic inspection success
        # traverses it without creating a synthetic waiting_human_tune gate;
        # only an explicit human-tune disposition activates the interactive
        # branch below.
        if data.get("phase") == "calibration_completed" and not data.get("human_tune_mode"):
            return {"asset": data.get("current_asset") or data.get("asset"),
                    "waiting": False, "phase": "calibration_completed"}
        action = options.get("manual_action")
        if action and action.action == "accept_current":
            current = data.get("current_asset") or data["asset"]
            self.store.events.append("human_tune_final_accepted", asset_hash=current["sha256"])
            return {"asset": current, "current_asset": current, "waiting": False,
                    "phase": "calibration_completed", "calibration_status": "human_accepted",
                    "termination_satisfied": True, "termination_reason": "human_tune_final_accepted",
                    "latest_checked_asset_hash": current["sha256"],
                    "selected_policy": data.get("selected_policy") or data.get("self_check_policy") or {"release": "human"}}
        prompt = options.get("human_prompt")
        if not prompt: return {"asset": data.get("current_asset") or data.get("asset"), "waiting": True,
                               "phase": "waiting_human_tune", "human_tune_mode": True}
        current = data.get("current_asset") or data["asset"]
        result = self._image_call("human_prompt_rework", prompt, [str(current["uri"])])
        asset = normalize_image_asset(result)
        self.store.events.append("calibration_invalidated", reason="human_rework", previous_checked_asset_hash=data.get("latest_checked_asset_hash"), new_asset_hash=asset["sha256"])
        return {"asset": asset, "current_asset": asset, "waiting": True, "phase": "waiting_human_tune", "human_tune_mode": True,
                "calibration_status": "waiting_human_tune", "termination_satisfied": False, "termination_reason": "human_tune_in_progress",
                "latest_checked_asset_hash": None, "inspection": None}

    def _present_inspection(self, number: int, result: VisualCheckResult) -> None:
        self.store.events.append("inspection_presented", round=number, result=result.model_dump(mode="json"))
        self.output(f"第 {number} 轮画面质检\n{self.presenter.inspection(result)}")

    def _final(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        asset = data.get("asset") or data.get("current_asset")
        checked_hash = data.get("latest_checked_asset_hash")
        satisfied = bool(data.get("termination_satisfied"))
        if data.get("calibration_status") not in {"completed", "human_accepted"}:
            satisfied = False
        if not checked_hash or checked_hash != asset.get("sha256"):
            satisfied = False
        if not data.get("selected_policy") or not data.get("termination_reason"):
            satisfied = False
        actor = options.get("actor") or (data.get("task_approval") or {}).get("actor")
        if self.offline_mode and asset.get("mock") is True and "domain_state" in data:
            if not options.get("final_approved") or not actor:
                raise ValueError("离线演练最终验收需要操作人人工确认。")
            if not satisfied:
                raise ValueError("离线演练最终验收前必须完成并通过质检处置。")
            self.store.events.append("offline_rehearsal_completed", actor=actor,
                                     asset_sha256=asset["sha256"])
            return {"rehearsal_asset": asset, "offline_rehearsal_completed": True,
                    "completed": True, "phase": "offline_rehearsal_completed"}
        if "domain_state" not in data:
            # Read-only compatibility for checkpoints created before the unified
            # graph. All newly created task projects use the frozen contract.
            self.workflow.validate_final_asset(asset, human_approved=bool(options.get("final_approved")), self_check_complete=satisfied)
            return {"final_asset": asset, "completed": True}
        self.workflow.validate_final_asset(asset, human_approved=bool(options.get("final_approved") and actor), self_check_complete=satisfied)
        frozen = freeze_delivery({**data, "quality_asset_sha256": checked_hash, "quality_passed": satisfied},
                                 asset=asset, quality_version=str(data.get("quality_version", "visual-check-v2")), actor=actor)
        delivery = frozen.model_dump(mode="json")
        trace_ref = f"project:{self.store.project_id}:asset:{asset['sha256']}"
        envelope = build_delivery(data, self.store.project_id, asset, trace_ref)
        delivery_files = persist_delivery(self.store.root, envelope)
        self.store.events.append("delivery_frozen", delivery=delivery)
        self.store.events.append("delivery_exported", envelope=envelope.model_dump(mode="json"), files=delivery_files)
        return {"final_asset": asset, "frozen_delivery": delivery, "delivery_envelope": envelope.model_dump(mode="json"),
                "delivery_files": delivery_files, "completed": True}

    def _text(self, route: ModelRoute):
        client = build_text_client(route.binding, timeout=self.policy.model_timeout_seconds)
        if client is None: raise RuntimeError("文本模型不可用。")
        return client

    def _inspect(self, image_uri: str, prompt: str) -> dict[str, Any]:
            if self.offline_mode:
                return {"passed": False, "decision": "continue", "rework_prompt_delta": "提高主体清晰度",
                        "overall_score": 72, "dimension_scores": {"任务符合度": 75, "文字正确性": 70,
                        "构图": 72, "视觉质量": 72, "安全合规": 100}, "confidence": 0.8}
            
            # 👇 构造明确要求返回 JSON 的质检 Prompt
            inspection_prompt = (
                f"你是一个专业的视觉质量审查员。请对比任务书与图片，检查图片是否符合要求。\n"
                f"只能输出一个纯 JSON 对象，格式要求如下：\n"
                f"{{\n"
                f'  "passed": true或false,\n'
                f'  "decision": "pass"（符合）或 "continue"（需微调）或 "blocked"（严重不符）,\n'
                f'  "deviations": ["发现的问题或偏差描述"],\n'
                f'  "rework_prompt_delta": "如果不符合，给出具体的改图提示词建议",\n'
                f'  "overall_score": 0到100的整体画面质量分,\n'
                f'  "dimension_scores": {{"任务符合度": 0到100, "文字正确性": 0到100, "构图": 0到100, "视觉质量": 0到100, "安全合规": 0到100}},\n'
                f'  "confidence": 0.95\n'
                f"}}\n\n"
                f"设计任务书要求：\n{prompt}"
            )
            
            provider_image = self.provider_assets.resolve(image_uri)
            raw = self.gateway.call("self_check_inspection", ModelRole.VISION_LANGUAGE_MODEL,
                lambda route: self._vlm(route).inspect(provider_image, inspection_prompt),
                messages=[{"role":"user","content":inspection_prompt},{"role":"image","content":image_uri}],
                variables={"image":image_uri}, template_id="visual-check", template_version="2", input_refs=[image_uri], needs_images=1)
            def repair(response: str, errors: str):
                repair_prompt = f"只修复为指定 JSON Schema，不改变语义。校验错误：{errors}\n原响应：{response}"
                return self.gateway.call("self_check_inspection", ModelRole.VISION_LANGUAGE_MODEL,
                    lambda route: self._vlm(route).inspect(provider_image, repair_prompt), messages=[{"role":"user","content":repair_prompt}],
                    variables={"repair":True}, template_id="visual-check-repair", template_version="1", input_refs=[image_uri], needs_images=1)
            try:
                return parse_with_one_repair(raw, repair).model_dump(mode="json")
            except InspectionOutputError as exc:
                self.store.events.append("inspection_schema_failed", safe_raw=exc.safe_raw, errors=exc.errors, recoverable=True)
                raise

    def _vlm(self, route: ModelRoute):
        client = build_vlm_client(route.binding, timeout=self.policy.model_timeout_seconds)
        if client is None: raise RuntimeError("视觉检查模型不可用。")
        return client

    def _image_call(self, state: str, prompt: str, references: list[str], *, index: int | None = None) -> dict[str, Any]:
        if state == "initial_candidate_generation":
            from agent_core.style_pipeline import assert_reference_isolated
            assert_reference_isolated({"prompt": prompt, "candidate_index": index})
            if references:
                raise ValueError("STYLE_REFERENCE_LEAK:reference_images")
        if self.offline_mode:
            # A real decodable fixture crosses the same persistence boundary;
            # mock/provider URLs never enter successful events or checkpoints.
            fixture = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            result = self.gateway.call(state, ModelRole.TEXT_TO_IMAGE_MODEL,
                lambda route: {"content": fixture, "mock": True, "provider": "offline", "model": route.binding.model},
                messages=[{"role":"user","content":prompt}], variables={"candidate_index":index, "reference_images":references},
                template_id=state, template_version="2", input_refs=references, needs_images=len(references))
            from storage.image_ingest import persist_image_response
            saved = persist_image_response(self.store.artifacts, result,
                                           metadata={"state": state, "candidate_index": index, "mock": True})
            return normalize_image_asset({**saved, "mock": True})
        provider_references = self.provider_assets.resolve_all(references)
        result = self.gateway.call(state, ModelRole.TEXT_TO_IMAGE_MODEL,
            lambda route: ArkImageRenderClient(base_url=self.policy.image_api_base_url or "https://ark.cn-beijing.volces.com/api/v3",
                model=route.binding.model, timeout=self.policy.model_timeout_seconds, max_retries=0,
                idempotency_key=str(route.binding.parameters.get("_idempotency_key", ""))).render(build_render_payload(route.binding.model, prompt,
                self.policy.default_output_size, {"state":state}, response_format=self.policy.response_format,
                watermark=self.policy.watermark, reference_images=provider_references)),
            messages=[{"role":"user","content":prompt}], variables={"candidate_index":index, "reference_images":references},
            template_id=state, template_version="2", input_refs=references, needs_images=len(references))
        from storage.image_ingest import persist_image_response
        return persist_image_response(self.store.artifacts, result, metadata={"state": state, "candidate_index": index})
