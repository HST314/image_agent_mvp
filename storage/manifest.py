"""Validated archive-manifest value object."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    """List of artifacts included in an archive."""

    manifest_id: str
    artifact_refs: list[str] = field(default_factory=list)
