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

    def load_for_task(self, task_card: ImageTaskCard) -> CategoryLibraryMatch | None:
        """Return the best matching category skill for the task, if any."""

        if not self.records:
            return None
        query_text = self._task_text(task_card)
        best_record = max(self.records, key=lambda record: self._score_record(record, query_text))
        score = self._score_record(best_record, query_text)
        if score <= 0:
            return None
        return CategoryLibraryMatch(
            skill=self._to_skill(best_record),
            score=score,
            source_record=best_record,
        )

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

        required_questions = [
            RequiredQuestion(
                field=f"library_required_input_{index}",
                question=str(item),
                blocks_generation=str(item) in {str(value) for value in project_inputs.get("blocking_if_missing", [])},
                default_handling="缺失时保持未确认，不得在生成阶段自行补全。",
            )
            for index, item in enumerate(project_inputs.get("required", [])[:8], start=1)
        ]
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
