"""Load external category skill cards without domain rules in code."""

from __future__ import annotations

import json
from pathlib import Path

from agent_core.models import CategorySkill, ImageTaskCard, SkillStatus


def load_category_skill(path: str | Path) -> CategorySkill:
    """Load and validate one category skill JSON file."""

    return CategorySkill.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_category_skill_index(path: str | Path) -> dict[str, list[dict[str, str | bool]]]:
    """Load a category skill index as plain JSON."""

    return json.loads(Path(path).read_text(encoding="utf-8"))


class CategorySkillLoader:
    """Resolve approved category skills from an index file."""

    def __init__(self, index_path: str | Path) -> None:
        self.index_path = Path(index_path)
        self.base_dir = self.index_path.parent
        self.index = load_category_skill_index(self.index_path)

    def load_for_task(self, task_card: ImageTaskCard) -> CategorySkill:
        """Load the skill referenced by ``task_card.category_ref``.

        If no category reference is supplied, the first approved default item in
        the index is used. This keeps the engine runnable while still moving all
        domain behavior into external data files.
        """

        requested_id = task_card.category_ref.category_id if task_card.category_ref else None
        items = self.index.get("items", [])
        selected = self._select_item(items, requested_id)
        skill = load_category_skill(self.base_dir / str(selected["path"]))
        if skill.status is not SkillStatus.APPROVED:
            raise ValueError(f"Category skill '{skill.category_id}' is not approved.")
        return skill

    @staticmethod
    def _select_item(items: list[dict[str, str | bool]], requested_id: str | None) -> dict[str, str | bool]:
        """Select a skill index item by id or approved default marker."""

        for item in items:
            if requested_id and item.get("category_id") == requested_id:
                return item
        for item in items:
            if item.get("is_default") is True:
                return item
        if items:
            return items[0]
        raise ValueError("Category skill index does not contain any items.")
