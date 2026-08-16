"""Adapt an external hierarchical category library into runtime skills."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_core.models import (
    AppliesWhen,
    CategorySkill,
    ImageTaskCard,
    PromptInjection,
    RequiredQuestion,
    SkillStatus,
)


@dataclass(frozen=True, slots=True)
class CategoryLibraryMatch:
    """Best-effort category library match result."""

    skill: CategorySkill
    score: int
    source_record: dict[str, Any]


class CategoryLibraryAdapter:
    """Resolve category-like records from a generic external JSON library."""

    def __init__(self, library_path: str | Path) -> None:
        self.library_path = Path(library_path)
        self.payload = json.loads(self.library_path.read_text(encoding="utf-8"))
        self.records = list(self._iter_records(self.payload))

    def load_for_task(self, task_card: ImageTaskCard, *, exclude_category_ids: set[str] | None = None,
                      allow_unmatched: bool = False) -> CategoryLibraryMatch | None:
        """Return the best matching category skill for the task, if any."""

        excluded = exclude_category_ids or set()
        candidates = [record for record in self.records if self._record_category_id(record) not in excluded]
        if not candidates:
            return None
        query_text = self._task_text(task_card)
        best_record = max(candidates, key=lambda record: self._score_record(record, query_text))
        score = self._score_record(best_record, query_text)
        if score <= 0 and not allow_unmatched:
            return None
        return CategoryLibraryMatch(
            skill=self._to_skill(best_record),
            score=score,
            source_record=best_record,
        )

    @staticmethod
    def _record_category_id(record: dict[str, Any]) -> str:
        return f"library_{record.get('id', 'unknown')}"

    def refresh_skill_policies(self, skill: CategorySkill) -> CategorySkill:
        """Backfill未知项策略元数据 onto a legacy-frozen skill from the current library.

        冻结的品类约束保持问题集与阻塞判定不变；仅按当前库中同类别记录的
        input_policies 刷新每个问题的 handling_strategy / default_value /
        default_handling。库中不存在该类别时原样返回。
        """

        record = next(
            (item for item in self.records if self._record_category_id(item) == skill.category_id),
            None,
        )
        if record is None:
            return skill
        current_by_question = {q.question: q for q in self._to_skill(record).required_questions}
        questions = []
        for question in skill.required_questions:
            current = current_by_question.get(question.question)
            if current is None or current.handling_strategy is None:
                questions.append(question)
                continue
            questions.append(question.model_copy(update={
                "handling_strategy": current.handling_strategy,
                "default_value": current.default_value,
                "default_handling": current.default_handling,
            }))
        return skill.model_copy(update={"required_questions": questions})

    @staticmethod
    def _iter_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten supported hierarchy records without assuming business terms."""

        records: list[dict[str, Any]] = []
        for category in payload.get("categories", []):
            for family in category.get("product_families", []):
                for deliverable in family.get("deliverables", []):
                    records.append(
                        {
                            **deliverable,
                            "_level_1": {"id": category.get("id"), "name": category.get("name")},
                            "_level_2": {"id": family.get("id"), "name": family.get("name")},
                        }
                    )
        return records

    @staticmethod
    def _task_text(task_card: ImageTaskCard) -> str:
        """Collect task-card text fields for external-library matching."""

        source_text = " ".join(ref.excerpt or "" for ref in task_card.source_refs)
        known_text = json.dumps(task_card.known_facts, ensure_ascii=False)
        unknown_text = json.dumps(task_card.unknowns, ensure_ascii=False)
        return " ".join(
            [
                task_card.deliverable_goal,
                task_card.usage_context,
                source_text,
                known_text,
                unknown_text,
            ]
        )

    @staticmethod
    def _score_record(record: dict[str, Any], query_text: str) -> int:
        """Score a library record by exact external-name occurrences."""

        names = [
            str(record.get("name", "")),
            str(record.get("_level_1", {}).get("name", "")),
            str(record.get("_level_2", {}).get("name", "")),
        ]
        score = 0
        for name in names:
            if name and name in query_text:
                score += max(1, len(name))
        return score

    @staticmethod
    def _to_skill(record: dict[str, Any]) -> CategorySkill:
        """Convert one external record into the existing CategorySkill contract."""

        project_inputs = record.get("project_inputs", {}) if isinstance(record.get("project_inputs"), dict) else {}
        production = record.get("production", {}) if isinstance(record.get("production"), dict) else {}
        risks = record.get("risks", {}) if isinstance(record.get("risks"), dict) else {}
        definition = record.get("product_definition", {}) if isinstance(record.get("product_definition"), dict) else {}

        blocking_inputs = [str(value) for value in project_inputs.get("blocking_if_missing", [])]
        raw_policies = project_inputs.get("input_policies", {})
        input_policies = raw_policies if isinstance(raw_policies, dict) else {}

        def blocks(item: Any) -> bool:
            text = str(item)
            for blocker in blocking_inputs:
                if blocker in text or text in blocker:
                    return True
                if blocker == "交付范围" and "范围" in text:
                    return True
                if blocker == "材料性能要求" and ("环境" in text or "寿命" in text):
                    return True
            return False

        def policy_for(item: Any) -> dict[str, Any]:
            policy = input_policies.get(str(item))
            return dict(policy) if isinstance(policy, dict) else {}

        def handling_for(item: Any, *, is_blocking: bool, policy: dict[str, Any]) -> str:
            if is_blocking:
                return "缺失时保持未确认，不得在生成阶段自行补全。"
            if policy.get("strategy") == "safe_default":
                return str(policy.get("default_handling") or "采用明确默认值作为执行基线，任务书确认前仍可修改。")
            return "本轮交付不包含该项，任务书按范围边界说明，不进入生成假设。"

        required_questions = []
        for index, item in enumerate(project_inputs.get("required", [])[:8], start=1):
            policy = policy_for(item)
            is_blocking = blocks(item)
            required_questions.append(
                RequiredQuestion(
                    field=f"library_required_input_{index}",
                    question=str(item),
                    blocks_generation=is_blocking,
                    default_handling=handling_for(item, is_blocking=is_blocking, policy=policy),
                    handling_strategy=None if is_blocking else policy.get("strategy"),
                    default_value=policy.get("default_value"),
                )
            )
        visual_rules = _string_list(production.get("route_selection_rules"))
        constraints = _string_list(project_inputs.get("required")) + _string_list(
            production.get("materials", {}).get("required_variant_fields")
            if isinstance(production.get("materials"), dict)
            else []
        )
        review_checks = _flatten_risk_checks(risks) + _string_list(
            record.get("supplier_verification", {}).get("checklist")
            if isinstance(record.get("supplier_verification"), dict)
            else []
        )
        return CategorySkill(
            category_id=f"library_{record.get('id', 'unknown')}",
            version=str(record.get("build_status", "external_library")),
            display_name=str(record.get("name")) if record.get("name") else None,
            applies_when=AppliesWhen(
                keywords=[
                    str(item)
                    for item in [
                        record.get("name"),
                        record.get("_level_1", {}).get("name"),
                        record.get("_level_2", {}).get("name"),
                    ]
                    if item
                ],
                semantic_rules=[str(record.get("classification_note", ""))] if record.get("classification_note") else [],
            ),
            required_questions=required_questions,
            prompt_injection=PromptInjection(
                category_description=str(definition.get("use") or record.get("classification_note") or ""),
                production_constraints=constraints[:12],
                visual_rules=visual_rules[:8],
                forbidden_elements=[],
            ),
            review_checks=review_checks[:12],
            status=SkillStatus.APPROVED,
        )


def _string_list(value: Any) -> list[str]:
    """Normalize unknown external list-like values into strings."""

    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _flatten_risk_checks(risks: dict[str, Any]) -> list[str]:
    """Flatten external risk buckets into generic review checks."""

    checks: list[str] = []
    for values in risks.values():
        checks.extend(_string_list(values))
    return checks
