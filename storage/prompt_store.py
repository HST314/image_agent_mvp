"""Append-only prompt audit chain used by every model call."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from storage.project_store import FORMAT_VERSION, ImmutableRecordError, content_hash, _now


class PromptStore:
    """Persist immutable call-start and call-result records in JSONL."""

    SECRET_WORDS = ("api_key", "apikey", "authorization", "token", "secret")
    REQUIRED = {"messages", "template_id", "template_version", "template_hash", "variables",
                "input_refs", "model", "parameters", "config_hash", "state", "trace_id"}

    def __init__(self, path: Path) -> None:
        self.path = path if path.suffix == ".jsonl" else path / "prompts.jsonl"

    def begin(self, record: dict[str, Any]) -> str:
        missing = self.REQUIRED - record.keys()
        if missing:
            raise ValueError(f"Prompt 审计记录缺少必填项：{', '.join(sorted(missing))}")
        prompt_id = str(record.get("prompt_id") or f"prompt_{uuid4().hex}")
        if self._find(prompt_id, "started") is not None:
            raise ImmutableRecordError("Prompt 记录不可覆盖。")
        data = {"format_version": FORMAT_VERSION, "prompt_id": prompt_id, "created_at": _now(),
                "status": "started", **self._redact(record)}
        data["record_hash"] = content_hash(data)
        self._append(data)
        return prompt_id

    def complete(self, prompt_id: str, *, output_raw: Any, output_parsed: Any = None,
                 output_ref: str | None = None) -> str:
        original = self.get(prompt_id)
        result_id = f"{prompt_id}.result"
        if self._find(result_id) is not None:
            raise ImmutableRecordError("Prompt 输出审计记录不可覆盖。")
        record = {"format_version": FORMAT_VERSION, "prompt_id": result_id,
                  "call_prompt_id": prompt_id, "parent_record_hash": original["record_hash"],
                  "status": "completed", "completed_at": _now(),
                  "output_raw": self._redact(output_raw), "output_parsed": self._redact(output_parsed),
                  "output_ref": output_ref}
        record["record_hash"] = content_hash(record)
        self._append(record)
        return result_id

    def fail(self, prompt_id: str, error: dict[str, Any]) -> str:
        original = self.get(prompt_id)
        result_id = f"{prompt_id}.failed"
        record = {"format_version": FORMAT_VERSION, "prompt_id": result_id,
                  "call_prompt_id": prompt_id, "parent_record_hash": original["record_hash"],
                  "status": "failed", "completed_at": _now(), "error": self._redact(error)}
        record["record_hash"] = content_hash(record)
        self._append(record)
        return result_id

    def save(self, record: dict[str, Any]) -> str:
        return self.begin(record)

    def get(self, prompt_id: str) -> dict[str, Any]:
        data = self._find(prompt_id)
        if data is None:
            raise FileNotFoundError(prompt_id)
        checksum = data.pop("record_hash", None)
        if data.get("format_version") != FORMAT_VERSION or checksum != content_hash(data):
            raise ValueError("Prompt 审计链版本或完整性校验失败。")
        data["record_hash"] = checksum
        return data

    def _find(self, prompt_id: str, status: str | None = None) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        for line in reversed(self.path.read_text(encoding="utf-8").splitlines()):
            item = json.loads(line)
            if item.get("prompt_id") == prompt_id and (status is None or item.get("status") == status):
                return item
        return None

    def _append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    @classmethod
    def _redact(cls, value: Any) -> Any:
        # 新加这 2 行：如果是 Pydantic 对象，自动转为字典
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")

        if isinstance(value, dict):
            return {k: "[REDACTED]" if any(w in k.lower() for w in cls.SECRET_WORDS) else cls._redact(v)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        return value
