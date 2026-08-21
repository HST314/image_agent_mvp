"""Provider usage observations that can cross the Image Agent API boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Generic, TypeVar


T = TypeVar("T")
_SENSITIVE_KEYS = ("api_key", "apikey", "authorization", "access_token", "secret", "cookie")


@dataclass(frozen=True, slots=True)
class ProviderUsageObservation:
    """Safe accounting facts returned alongside one successful provider call."""

    provider_request_id: str | None
    token_usage: dict[str, int] | None
    billing_units: tuple[dict[str, Any], ...]
    raw_usage: dict[str, Any]

    def __post_init__(self) -> None:
        if self.token_usage is not None:
            required = {
                "input_tokens", "output_tokens", "cached_input_tokens",
                "reasoning_tokens", "total_tokens",
            }
            if set(self.token_usage) != required or any(
                _nonnegative_int(value) is None for value in self.token_usage.values()
            ):
                raise ValueError("Provider token usage is malformed.")
            if self.token_usage["total_tokens"] != (
                self.token_usage["input_tokens"] + self.token_usage["output_tokens"]
            ):
                raise ValueError("Provider token totals are inconsistent.")
            if self.token_usage["cached_input_tokens"] > self.token_usage["input_tokens"]:
                raise ValueError("Cached provider tokens exceed input tokens.")
            if self.token_usage["reasoning_tokens"] > self.token_usage["output_tokens"]:
                raise ValueError("Reasoning provider tokens exceed output tokens.")
        safe_units = []
        for item in self.billing_units[:64]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("unit"), str)
                or _nonnegative_int(item.get("quantity")) is None
            ):
                raise ValueError("Provider billing units are malformed.")
            safe_item = _bounded_json_object(item)
            if not safe_item:
                raise ValueError("Provider billing units exceed the accounting boundary.")
            safe_units.append(safe_item)
        object.__setattr__(self, "billing_units", tuple(safe_units))
        object.__setattr__(self, "raw_usage", _bounded_json_object(self.raw_usage))


@dataclass(frozen=True, slots=True)
class ProviderCallResult(Generic[T]):
    """Keep provider accounting attached until the gateway persists it."""

    value: T
    usage: ProviderUsageObservation


def observation_from_response(
    response: Any,
    *,
    billing_units: tuple[dict[str, Any], ...] = (),
) -> ProviderUsageObservation | None:
    """Extract bounded usage facts from an OpenAI-compatible SDK response."""

    dumped = _model_dump(response)
    raw_usage = _model_dump(getattr(response, "usage", None))
    if not raw_usage and isinstance(dumped.get("usage"), dict):
        raw_usage = dict(dumped["usage"])
    safe_usage = _bounded_json_object(raw_usage)
    token_usage = _token_usage(safe_usage)
    if token_usage is None and not billing_units:
        return None
    request_id = getattr(response, "_request_id", None) or getattr(response, "id", None)
    if not request_id:
        request_id = dumped.get("id") or dumped.get("request_id")
    if request_id is not None:
        request_id = str(request_id)[:256] or None
    return ProviderUsageObservation(
        provider_request_id=request_id,
        token_usage=token_usage,
        billing_units=billing_units,
        raw_usage=safe_usage,
    )


def _model_dump(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(mode="json")
        return dict(result) if isinstance(result, dict) else {}
    return {}


def _token_usage(raw: dict[str, Any]) -> dict[str, int] | None:
    input_tokens = _nonnegative_int(raw.get("prompt_tokens", raw.get("input_tokens")))
    output_tokens = _nonnegative_int(raw.get("completion_tokens", raw.get("output_tokens")))
    if input_tokens is None or output_tokens is None:
        return None
    prompt_details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or raw.get("output_tokens_details") or {}
    cached = _nonnegative_int(prompt_details.get("cached_tokens")) or 0
    reasoning = _nonnegative_int(completion_details.get("reasoning_tokens")) or 0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": min(cached, input_tokens),
        "reasoning_tokens": min(reasoning, output_tokens),
        "total_tokens": input_tokens + output_tokens,
    }


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _bounded_json_object(value: Any) -> dict[str, Any]:
    """Whitelist JSON values and cap untrusted provider metadata."""

    if not isinstance(value, dict):
        return {}

    def sanitize(item: Any, depth: int) -> Any:
        if depth > 4:
            return None
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, str):
            return item[:256]
        if isinstance(item, list):
            return [sanitize(child, depth + 1) for child in item[:64]]
        if isinstance(item, dict):
            return {
                str(key)[:128]: sanitize(child, depth + 1)
                for key, child in list(item.items())[:64]
                if not any(word in str(key).lower() for word in _SENSITIVE_KEYS)
            }
        return str(item)[:256]

    safe = sanitize(value, 0)
    if not isinstance(safe, dict):
        return {}
    if len(json.dumps(safe, ensure_ascii=False).encode("utf-8")) > 16 * 1024:
        return {}
    return safe
