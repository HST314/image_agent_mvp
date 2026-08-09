"""Generic JSON index loading for skill and style registries."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RegistryIndex:
    """List of external card references loaded from an index file."""

    items: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: str | Path) -> "RegistryIndex":
        """Load a registry index from JSON."""

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = data.get("items", data if isinstance(data, list) else [])
        return cls(items=[dict(item) for item in items])
