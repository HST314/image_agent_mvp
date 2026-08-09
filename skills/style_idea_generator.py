"""Generate style idea cards from style references."""

from __future__ import annotations

import json
from typing import Any

from agent_core.models import ImageTaskCard, StyleCard, StyleIdeaCard, TaskConfirmationDoc
from model_router.clients import VisionLanguageModelClient


class StyleIdeaGenerator:
    """Create human-readable style direction cards before image rendering."""

    def __init__(self, client: VisionLanguageModelClient | None = None, model_name: str | None = None, *, offline_mode: bool = False) -> None:
        self.client = client
        self.offline_mode = offline_mode
        self.model_name = model_name or ("offline_style_builder" if offline_mode else "style_vlm")

    def generate(
        self,
        *,
        task_card: ImageTaskCard,
        confirmation_doc: TaskConfirmationDoc,
        style_cards: list[StyleCard],
        count: int = 5,
    ) -> list[StyleIdeaCard]:
        """Generate one idea card for each selected style card."""

        cards: list[StyleIdeaCard] = []
        for style_card in style_cards[:count]:
            cards.append(
                self._generate_one(
                    task_card=task_card,
                    confirmation_doc=confirmation_doc,
                    style_card=style_card,
                )
            )
        return cards

    def _generate_one(
        self,
        *,
        task_card: ImageTaskCard,
        confirmation_doc: TaskConfirmationDoc,
        style_card: StyleCard,
    ) -> StyleIdeaCard:
        """Generate one style idea card through VLM when possible."""

        reference_asset = style_card.reference_assets[0] if style_card.reference_assets else None
        if self.client is not None and reference_asset:
            try:
                payload = self.client.inspect(reference_asset, self._prompt(task_card, confirmation_doc, style_card))
                return self._from_payload(task_card, style_card, reference_asset, payload)
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("风格模型输出不符合契约。") from exc
        if not self.offline_mode:
            raise RuntimeError("未配置风格模型；只有显式离线模式允许规则化风格卡。")
        return self._offline_card(task_card, style_card, reference_asset)

    def _from_payload(
        self,
        task_card: ImageTaskCard,
        style_card: StyleCard,
        reference_asset: str,
        payload: dict[str, Any],
    ) -> StyleIdeaCard:
        """Validate a model payload into a style idea card."""

        return StyleIdeaCard(
            task_id=task_card.task_id,
            source_style_id=style_card.style_id,
            title=str(payload["title"]),
            composition=str(payload["composition"]),
            material=str(payload["material"]),
            fit_reason=str(payload["fit_reason"]),
            major_risk=str(payload["major_risk"]),
            prompt_supplement=str(payload["prompt_supplement"]),
            reference_asset=reference_asset,
            generated_by=self.model_name,
        )

    def _offline_card(
        self,
        task_card: ImageTaskCard,
        style_card: StyleCard,
        reference_asset: str | None,
    ) -> StyleIdeaCard:
        """Build a deterministic idea card from approved style-card data."""

        material = "、".join(style_card.visual_language.materiality) or style_card.visual_language.scheme or "通用材质语言"
        risk = "；".join(style_card.risk_notes[:2]) or "主要风险来自未确认信息和风格过度延展。"
        return StyleIdeaCard(
            task_id=task_card.task_id,
            source_style_id=style_card.style_id,
            title=style_card.style_name or style_card.style_id,
            composition=style_card.composition,
            material=material,
            fit_reason="该方向与任务目标和使用场景保持一致，同时提供可区分的视觉机制。",
            major_risk=risk,
            prompt_supplement=(
                f"构图方向：{style_card.composition}\n"
                f"材质与视觉语言：{material}\n"
                "保持项目内容、已确认事实、颜色条件和空间条件一致；只改变风格机制。"
            ),
            reference_asset=reference_asset,
            generated_by=self.model_name,
        )

    @staticmethod
    def _prompt(
        task_card: ImageTaskCard,
        confirmation_doc: TaskConfirmationDoc,
        style_card: StyleCard,
    ) -> str:
        """Build a JSON-only VLM prompt for interpreting a style reference."""

        return (
            "请阅读参考图并为通用图片生成流程输出一个中文风格理念卡。"
            "不得加入任务卡和确认书以外的具体业务事实。只返回 JSON："
            '{"title":"string","composition":"string","material":"string",'
            '"fit_reason":"string","major_risk":"string","prompt_supplement":"string"}\n'
            f"任务卡：{json.dumps(task_card.model_dump(mode='json'), ensure_ascii=False)}\n"
            f"确认书：{json.dumps(confirmation_doc.model_dump(mode='json'), ensure_ascii=False)}\n"
            f"风格卡：{json.dumps(style_card.model_dump(mode='json'), ensure_ascii=False)}"
        )
