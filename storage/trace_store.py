"""Append-only JSONL trace storage."""

from __future__ import annotations

from pathlib import Path

from agent_core.models import TraceLog


class TraceStore:
    """Persist trace records as newline-delimited JSON."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, trace: TraceLog) -> None:
        """Append one trace record without overwriting existing history."""

        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(trace.model_dump_json() + "\n")

    def read_all(self) -> list[TraceLog]:
        """Read all trace records from the JSONL file."""

        if not self.path.exists():
            return []
        records: list[TraceLog] = []
        with self.path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    records.append(TraceLog.model_validate_json(line))
        return records
