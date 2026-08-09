"""Prompt version helpers."""

from __future__ import annotations

from typing import Any

from agent_core.models import PromptVersion


def create_prompt_version(
    prompt_text: str,
    task_id: str,
    confirmation_doc_id: str,
    style_id: str,
    category_id: str,
    variables: dict[str, Any] | None = None,
    template_version: str = "render_prompt_v1",
) -> PromptVersion:
    """Create a prompt version record."""

    return PromptVersion(
        task_id=task_id,
        confirmation_doc_id=confirmation_doc_id,
        style_id=style_id,
        category_id=category_id,
        template_version=template_version,
        prompt_text=prompt_text,
        variables=variables or {},
    )
