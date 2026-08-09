"""Generic review checks shared by gates and tests."""

from __future__ import annotations

from agent_core.models import RiskLevel, TaskConfirmationDoc


def contains_blocking_unknowns(doc: TaskConfirmationDoc) -> bool:
    """Return true when unresolved information blocks downstream generation."""

    return any(item.risk_level is RiskLevel.BLOCKING for item in doc.default_handling_for_unknowns)
