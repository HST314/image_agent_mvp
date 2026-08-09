"""Immutable human/controller decision contract persisted by ProjectStore events."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """Small immutable record for a human or controller decision."""

    state: str
    decision: str
    actor: str
