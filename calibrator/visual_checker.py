"""VLM-backed visual self-calibration checks with offline fallback."""

from __future__ import annotations

from agent_core.models import TaskConfirmationDoc, VisualCheckResult
from model_router.clients import VisionLanguageModelClient


class DeterministicVLMClient:
    """Offline VLM substitute used when no external key or client is configured."""

    def inspect(self, image_url: str, prompt: str) -> dict[str, object]:
        """Return a deterministic check result based on URL metadata."""

        if "needs-rework" in image_url or "fail" in image_url:
            return {
                "passed": False,
                "deviations": ["候选图未完全符合已锁定的确认书约束。"],
                "rework_prompt_delta": "收紧画面，使其更贴近已确认事实，并移除未被确认的信息。",
            }
        return {"passed": True, "deviations": [], "rework_prompt_delta": ""}


class VisualSelfCalibrator:
    """Check a locked master candidate against a signed confirmation document."""

    def __init__(
        self,
        client: VisionLanguageModelClient | None = None,
        model_name: str = "deterministic-visual-checker",
    ) -> None:
        self.client = client or DeterministicVLMClient()
        self.model_name = model_name

    def check(self, image_url: str, doc: TaskConfirmationDoc) -> VisualCheckResult:
        """Inspect an image URL and normalize the VLM response."""

        prompt = self._build_check_prompt(doc)
        payload = self.client.inspect(image_url=image_url, prompt=prompt)
        deviations = [str(item) for item in payload.get("deviations", [])]
        delta = str(payload.get("rework_prompt_delta", ""))
        return VisualCheckResult(
            passed=bool(payload.get("passed", False)),
            deviations=deviations,
            rework_prompt_delta=delta,
            model_name=self.model_name,
        )

    @staticmethod
    def _build_check_prompt(doc: TaskConfirmationDoc) -> str:
        """Build a generic VLM checklist from locked facts and constraints."""

        facts = "\n".join(f"- {fact.field}: {fact.value}" for fact in doc.confirmed_facts)
        forbidden = "\n".join(f"- {item}" for item in doc.forbidden_items) or "- 未提供"
        return (
            "请根据已签署的任务确认书检查这张图片。\n\n"
            f"已锁定事实：\n{facts or '- 未提供'}\n\n"
            f"负向约束：\n{forbidden}\n\n"
            "只返回 JSON，字段包含 passed、deviations、rework_prompt_delta。"
            "deviations 和 rework_prompt_delta 必须使用中文。"
        )
