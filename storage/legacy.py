"""Read-only legacy import; removed stages never enter the active workflow."""
from __future__ import annotations
from typing import Any

def migrate_legacy_context(payload: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(payload)
    assets = list(migrated.get("candidate_assets", []))
    migrated["candidate_assets"] = [asset for asset in assets if asset.get("metadata", {}).get("render_stage") != "secondary_formal"][:5]
    migrated.pop("direction_selection", None)
    migrated["legacy_migrated"] = True
    return migrated
