"""Strict VLM inspection parsing with one targeted repair attempt."""
from __future__ import annotations
import json
import re
from typing import Any, Callable
from pydantic import ValidationError
from agent_core.models import VisualCheckResult

SECRET = re.compile(r"(?i)(api[_-]?key|authorization|token)(\s*[:=]\s*)[^\s,}\"]+")

class InspectionOutputError(ValueError):
    def __init__(self, raw: str, errors: Any):
        self.safe_raw = SECRET.sub(r"\1\2[REDACTED]", raw)[:20_000]
        self.errors = errors
        super().__init__("INSPECTION_SCHEMA_INVALID_AFTER_REPAIR")

def _decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict): return raw
    text = str(raw).strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match: raise ValueError("JSON object not found")
    value = json.loads(match.group(0))
    if not isinstance(value, dict): raise ValueError("inspection must be an object")
    return value

def parse_with_one_repair(raw: Any, repair: Callable[[str, str], Any]) -> VisualCheckResult:
    original = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    try:
        return VisualCheckResult.model_validate(_decode(raw))
    except (ValueError, ValidationError, json.JSONDecodeError) as first:
        fixed = repair(original, str(first))
        try:
            return VisualCheckResult.model_validate(_decode(fixed))
        except (ValueError, ValidationError, json.JSONDecodeError) as second:
            raise InspectionOutputError(str(fixed), str(second)) from second
