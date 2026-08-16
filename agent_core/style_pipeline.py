"""Only supported bridge from task/category/style analysis to five render calls."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from agent_core.models import CategorySkill, StyleCard, StyleIdeaCard, TaskConfirmationDoc
from prompt_engine.composer import RenderPromptComposer
from skills.style_library import SelectedStyle, safe_render_supplement
from storage.project_store import content_hash


REFERENCE_KEYS = {"image", "reference", "reference_asset", "reference_images", "uri", "path", "bytes", "content"}


def assert_reference_isolated(payload: Any) -> None:
    """Reject image-like reference material at the final renderer boundary."""
    def walk(value: Any, key: str = "") -> None:
        if key.lower() in REFERENCE_KEYS and value not in (None, [], ""):
            raise ValueError(f"STYLE_REFERENCE_LEAK:{key}")
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
    walk(payload)


@dataclass(frozen=True)
class RenderPlanItem:
    slot: int
    style_id: str
    extraction_key: str
    prompt_version_id: str
    prompt_text: str
    provenance: dict[str, str]


class StyleRenderPlanner:
    def __init__(self, composer: RenderPromptComposer | None = None) -> None:
        self.composer = composer or RenderPromptComposer()

    def plan(self, *, confirmation: TaskConfirmationDoc, category: CategorySkill, styles: list[SelectedStyle],
             deliverable_goal: str, usage_context: str, task_revision_hash: str, config_hash: str) -> list[RenderPlanItem]:
        if len(styles) != 5 or len({s.style.style_id for s in styles}) != 5:
            raise ValueError("exactly five distinct styles required")
        plans: list[RenderPlanItem] = []
        for slot, selected in enumerate(styles):
            extraction = selected.extraction
            idea = StyleIdeaCard(task_id=confirmation.task_id, source_style_id=selected.style.style_id,
                                 title=selected.style.title, composition=extraction.composition,
                                 material=extraction.material, fit_reason=selected.reason, major_risk=selected.risk,
                                 prompt_supplement=safe_render_supplement(selected), reference_asset=None,
                                 generated_by=extraction.model_id)
            card = StyleCard(style_id=selected.style.style_id, version=extraction.extraction_key,
                             style_name=selected.style.title, composition=extraction.composition,
                             visual_language={"materiality":[extraction.material], "lighting":extraction.lighting,
                                              "scheme":extraction.graphic_language},
                             negative_elements=["复制参考图主体", "复制具体构图", "复制文字或标识", "复制独特表达"], status="approved")
            prompt = self.composer.compose(confirmation, category, card, deliverable_goal, usage_context,
                                           style_idea_card=idea)
            provenance = {"task_revision_hash": task_revision_hash, "category_id": category.category_id,
                          "style_id": selected.style.style_id, "extraction_key": extraction.extraction_key,
                          "prompt_version_id": prompt.prompt_version_id, "config_hash": config_hash}
            plans.append(RenderPlanItem(slot, selected.style.style_id, extraction.extraction_key,
                                        prompt.prompt_version_id, prompt.prompt_text, provenance))
        hard_sections = [p.prompt_text.split("风格卡注入：", 1)[0] for p in plans]
        if len(set(hard_sections)) != 1:
            raise ValueError("hard task constraints differ between slots")
        return plans

    def plan_free(self, *, confirmation: TaskConfirmationDoc, category: CategorySkill,
                  count: int, deliverable_goal: str, usage_context: str,
                  task_revision_hash: str, config_hash: str) -> list[RenderPlanItem]:
        """艺术风格库「不使用数据库」：按 candidate_concurrency 直接由任务书合成候选提示词。"""

        if count < 1:
            raise ValueError("自由候选数量必须为正整数。")
        plans: list[RenderPlanItem] = []
        for slot in range(count):
            style_id = f"free-{slot + 1}"
            prompt = self.composer.compose_free(
                confirmation, category, slot=slot, count=count, style_id=style_id,
                deliverable_goal=deliverable_goal, usage_context=usage_context,
            )
            provenance = {"task_revision_hash": task_revision_hash, "category_id": category.category_id,
                          "style_id": style_id, "extraction_key": "free",
                          "prompt_version_id": prompt.prompt_version_id, "config_hash": config_hash}
            plans.append(RenderPlanItem(slot, style_id, "free",
                                        prompt.prompt_version_id, prompt.prompt_text, provenance))
        return plans

    @staticmethod
    def render(plans: list[RenderPlanItem], invoke: Callable[[dict[str, Any]], Any]) -> list[Any]:
        results = []
        for plan in plans:
            payload = {"prompt": plan.prompt_text, "style_id": plan.style_id,
                       "extraction_key": plan.extraction_key, "candidate_index": plan.slot}
            assert_reference_isolated(payload)
            results.append(invoke(payload))
        return results
