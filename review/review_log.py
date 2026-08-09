"""Review log records for later phases."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReviewLogEntry:
    """Minimal review log entry."""

    state: str
    summary: str
    passed: bool
