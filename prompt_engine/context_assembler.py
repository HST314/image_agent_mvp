"""Minimal multimodal context assembly with explicit capability handling."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal
from agent_core.models import ReferenceImage

class CapabilityMismatchError(RuntimeError):
    pass

@dataclass(frozen=True)
class ContextPolicy:
    kind: Literal["text", "vision", "image"]
    max_text_chars: int = 12000
    max_images: int = 3
    supports_multiple_images: bool = True
    allow_single_image_fallback: bool = False

class ContextAssembler:
    def __init__(self, policy: ContextPolicy) -> None:
        self.policy = policy

    def assemble(self, *, objective: str, specification: str, constraints: list[str], current_input: str, references: list[ReferenceImage] | None = None, feedback: str = "", optional_context: list[str] | None = None) -> dict[str, Any]:
        refs = sorted(references or [], key=lambda item: item.order)
        if len(refs) > 1 and not self.policy.supports_multiple_images:
            if not self.policy.allow_single_image_fallback:
                raise CapabilityMismatchError("当前模型不支持多参考图，且未配置允许的降级策略。")
            refs = refs[:1]
        refs = refs[:self.policy.max_images]
        required = [objective, specification, "\n".join(constraints), current_input, feedback]
        optional = optional_context or []
        text = "\n\n".join(item for item in required + optional if item)
        trimmed = len(text) > self.policy.max_text_chars
        if trimmed:
            base = "\n\n".join(item for item in required if item)
            text = (base + "\n\n" + "\n".join(optional))[:self.policy.max_text_chars]
        return {"text": text, "references": [item.model_dump(mode="json") for item in refs], "trimmed": trimmed, "policy": self.policy.kind}
