"""Generic prompt composition helpers for rendering phases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_core.gates import require_approved_confirmation
from agent_core.models import CategorySkill, PromptVersion, StyleCard, StyleIdeaCard, TaskConfirmationDoc
from prompt_engine.versioning import create_prompt_version


TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "render_prompt.md"


def compose_render_prompt(doc: TaskConfirmationDoc, style_card_text: str = "", skill_text: str = "") -> str:
    """Compose a generic render prompt after enforcing approval."""

    require_approved_confirmation(doc, "prompt_compose")
    facts = "\n".join(f"- {fact.field}: {fact.value}" for fact in doc.confirmed_facts)
    unknowns = "\n".join(
        f"- {item.field}: {item.handling} ({item.risk_level.value})"
        for item in doc.default_handling_for_unknowns
    )
    forbidden = "\n".join(f"- {item}" for item in doc.forbidden_items) or "- 未提供"
    return (
        "渲染一张完整候选图片。\n\n"
        f"任务确认摘要：\n{doc.summary}\n\n"
        f"已确认事实：\n{facts}\n\n"
        f"未明确信息处理：\n{unknowns}\n\n"
        f"技能注入：\n{skill_text}\n\n"
        f"风格注入：\n{style_card_text}\n\n"
        f"禁止项：\n{forbidden}\n\n"
        "不得编造未确认的标识符、文案、尺寸、素材或交付事实。"
    )


class RenderPromptComposer:
    """Compose versioned render prompts from confirmation, skill, and style data."""

    def __init__(self, template_path: str | Path = TEMPLATE_PATH) -> None:
        self.template_path = Path(template_path)

    def compose(
        self,
        doc: TaskConfirmationDoc,
        category_skill: CategorySkill,
        style_card: StyleCard,
        deliverable_goal: str,
        usage_context: str,
        asset_usage_rules: list[str] | None = None,
        locked_elements: list[str] | None = None,
        style_idea_card: StyleIdeaCard | None = None,
        render_stage: str = "primary",
    ) -> PromptVersion:
        """Create a traceable prompt version for one style candidate."""

        require_approved_confirmation(doc, "prompt_compose")
        variables = {
            "deliverable_goal": deliverable_goal,
            "usage_context": usage_context,
            "confirmed_facts": self._facts(doc),
            "default_handling_for_unknowns": self._unknowns(doc),
            "category_skill_injection": self._skill_injection(category_skill),
            "style_card_injection": self._style_injection(style_card, style_idea_card),
            "locked_elements": self._list(locked_elements or self._locked_fact_fields(doc)),
            "negative_constraints": self._list(self._negative_constraints(doc, category_skill, style_card)),
            "asset_usage_rules": self._list(asset_usage_rules or ["仅遵循已验证的素材使用规则。"]),
            "render_stage": render_stage,
        }
        prompt_text = self._render_template(variables)
        prompt_version = create_prompt_version(
            prompt_text=prompt_text,
            task_id=doc.task_id,
            confirmation_doc_id=doc.confirmation_doc_id,
            style_id=style_card.style_id,
            category_id=category_skill.category_id,
            variables=variables,
        )
        prompt_version.style_idea_id = style_idea_card.idea_id if style_idea_card is not None else None
        return prompt_version

    def _render_template(self, variables: dict[str, str]) -> str:
        """Render the markdown template with simple exact placeholders."""

        text = self.template_path.read_text(encoding="utf-8")
        for key, value in variables.items():
            text = text.replace("{{" + key + "}}", value)
        return text

    @staticmethod
    def _facts(doc: TaskConfirmationDoc) -> str:
        """Format confirmed facts for prompt injection."""

        return "\n".join(f"- {item.field}: {item.value}" for item in doc.confirmed_facts) or "- 未提供"

    @staticmethod
    def _unknowns(doc: TaskConfirmationDoc) -> str:
        """Format unknown handling policies for prompt injection."""

        return "\n".join(
            f"- {item.field}: {item.handling} ({item.risk_level.value})"
            for item in doc.default_handling_for_unknowns
        ) or "- 未提供"

    @staticmethod
    def _skill_injection(skill: CategorySkill) -> str:
        """Serialize category skill injection fields."""

        payload: dict[str, Any] = {
            "description": skill.prompt_injection.category_description,
            "production_constraints": skill.prompt_injection.production_constraints,
            "visual_rules": skill.prompt_injection.visual_rules,
            "review_checks": skill.review_checks,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _style_injection(style_card: StyleCard, style_idea_card: StyleIdeaCard | None = None) -> str:
        """Serialize style-card fields relevant to image generation."""

        payload = {
            "style_id": style_card.style_id,
            "composition": style_card.composition,
            "visual_language": style_card.visual_language.model_dump(),
            "risk_notes": style_card.risk_notes,
        }
        if style_idea_card is not None:
            payload["style_idea"] = {
                "idea_id": style_idea_card.idea_id,
                "title": style_idea_card.title,
                "composition": style_idea_card.composition,
                "material": style_idea_card.material,
                "fit_reason": style_idea_card.fit_reason,
                "major_risk": style_idea_card.major_risk,
                "prompt_supplement": style_idea_card.prompt_supplement,
            }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _negative_constraints(
        doc: TaskConfirmationDoc,
        skill: CategorySkill,
        style_card: StyleCard,
    ) -> list[str]:
        """Merge negative constraints without duplicates."""

        merged: list[str] = []
        for item in [
            *doc.forbidden_items,
            *skill.prompt_injection.forbidden_elements,
            *style_card.negative_elements,
        ]:
            if item not in merged:
                merged.append(item)
        return merged

    @staticmethod
    def _locked_fact_fields(doc: TaskConfirmationDoc) -> list[str]:
        """List locked confirmed fact fields."""

        return [fact.field for fact in doc.confirmed_facts if fact.locked]

    @staticmethod
    def _list(items: list[str]) -> str:
        """Format a string list for prompt sections."""

        return "\n".join(f"- {item}" for item in items) or "- 未提供"
