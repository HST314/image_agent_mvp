"""Append-only candidate asset persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from agent_core.models import CandidateAsset


@runtime_checkable
class AssetStoreProtocol(Protocol):
    def append(self, asset: CandidateAsset) -> None: ...
    def read_all(self) -> list[CandidateAsset]: ...

class AssetStore:
    """Persist rendered asset records as newline-delimited JSON."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "assets.jsonl"

    def append(self, asset: CandidateAsset) -> None:
        """Append one asset record without overwriting history."""

        uri = str(getattr(asset, "uri", None) or getattr(asset, "url", ""))
        if uri.startswith(("mock://", "http://", "https://", "file://")):
            raise ValueError("ASSET_REF_UNSTABLE: 成功资产必须使用内部 URI 或内容寻址 ID。")
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(asset.model_dump_json() + "\n")

    def read_all(self) -> list[CandidateAsset]:
        """Read all asset records from storage."""

        if not self.path.exists():
            return []
        records: list[CandidateAsset] = []
        with self.path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    records.append(CandidateAsset.model_validate_json(line))
        return records
