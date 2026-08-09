"""Load external style cards without embedding style rules in code."""

from __future__ import annotations

import json
from pathlib import Path

from agent_core.models import SkillStatus, StyleCard


def load_style_card(path: str | Path) -> StyleCard:
    """Load and validate one style card JSON file."""

    return StyleCard.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_style_card_index(path: str | Path) -> dict[str, list[dict[str, str | int]]]:
    """Load a style card index as plain JSON."""

    return json.loads(Path(path).read_text(encoding="utf-8"))


class StyleCardLoader:
    """Load and select approved style cards from an external index."""

    def __init__(self, index_path: str | Path) -> None:
        self.index_path = Path(index_path)
        self.base_dir = self.index_path.parent
        self.index = load_style_card_index(self.index_path)

    def select_distinct(self, count: int = 5) -> list[StyleCard]:
        """Return approved style cards ordered by index priority.

        The index owns the style vocabulary and selection order. This function
        only enforces count, approval status, and different composition values.
        """

        selected: list[StyleCard] = []
        seen_compositions: set[str] = set()
        items = sorted(self.index.get("items", []), key=lambda item: int(item.get("priority", 1000)))
        for item in items:
            card = load_style_card(self.base_dir / str(item["path"]))
            if card.status is not SkillStatus.APPROVED:
                continue
            if card.composition in seen_compositions:
                continue
            selected.append(card)
            seen_compositions.add(card.composition)
            if len(selected) == count:
                return selected
        raise ValueError(f"Style index does not contain {count} approved distinct style cards.")
