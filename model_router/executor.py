"""Timed, classified and fully audited model-call execution."""
from __future__ import annotations
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar
from uuid import uuid4
from storage.prompt_store import PromptStore

T = TypeVar("T")

@dataclass
class ModelCallError(RuntimeError):
    message: str
    retryable: bool
    category: str
    request_id: str
    trace_id: str
    def __str__(self) -> str: return self.message

class ModelExecutor(Generic[T]):
    def __init__(self, *, max_attempts: int = 2, base_delay: float = .1, timeout: float = 180, sleeper: Callable[[float], None] = time.sleep, randomizer: Callable[[float, float], float] = random.uniform) -> None:
        if max_attempts < 1 or timeout <= 0: raise ValueError("max_attempts 和 timeout 必须为正数。")
        self.max_attempts, self.base_delay, self.timeout = max_attempts, base_delay, timeout
        self.sleeper, self.randomizer = sleeper, randomizer

    def run(self, call: Callable[[], T], *, request_id: str | None = None, trace_id: str | None = None) -> T:
        req, trace = request_id or f"req_{uuid4().hex}", trace_id or f"trace_{uuid4().hex}"
        last: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                # Timeout belongs in the SDK/HTTP client. An outer worker timeout
                # cannot cancel a paid request and creates an unobservable thread.
                return call()
            except BaseException as exc:
                last = exc
                category, retryable = self.classify(exc)
            if not retryable or attempt == self.max_attempts:
                message = ("输入文案触发模型内容审核，请修改质检建议或任务文案后再执行。"
                           if category == "content_moderation" else (str(last) or category))
                raise ModelCallError(message, retryable, category, req, trace) from last
            self.sleeper(self.base_delay * 2 ** (attempt - 1) + self.randomizer(0, self.base_delay))
        raise AssertionError("unreachable")

    def audited_run(self, call: Callable[[], T], *, prompts: PromptStore, audit: dict[str, Any], parser: Callable[[T], Any] | None = None) -> T:
        prompt_id = prompts.begin(audit)
        try:
            result = self.run(call, request_id=str(audit.get("request_id") or "") or None, trace_id=str(audit["trace_id"]))
            prompts.complete(prompt_id, output_raw=result, output_parsed=parser(result) if parser else None)
            return result
        except Exception as exc:
            prompts.fail(prompt_id, {"code": type(exc).__name__, "message": str(exc)})
            raise

    @staticmethod
    def classify(exc: BaseException) -> tuple[str, bool]:
        if isinstance(exc, (ValueError, TypeError, PermissionError)): return "validation_or_refusal", False
        provider_text = ModelExecutor._provider_error_text(exc).upper()
        if any(code in provider_text for code in (
            "INPUTTEXTSENSITIVECONTENTDETECTED", "CONTENT_FILTER", "CONTENTFILTER",
            "MODERATION", "SAFETY_VIOLATION", "CONTENT_POLICY",
        )):
            return "content_moderation", False
        status = getattr(exc, "status_code", None)
        if status in {400, 401, 403, 404, 422}: return "request_rejected", False
        if status == 429: return "rate_limited_unknown", False
        if isinstance(status, int) and status >= 500: return "provider_unavailable_unknown", False
        if isinstance(exc, TimeoutError): return "timeout_unknown", False
        if isinstance(exc, ConnectionError): return "transport_unknown", False
        return "provider_error_unknown", False

    @staticmethod
    def _provider_error_text(exc: BaseException) -> str:
        """Collect provider codes/bodies before generic HTTP status handling."""
        values: list[str] = [str(exc), str(getattr(exc, "code", "")), str(getattr(exc, "body", ""))]
        response = getattr(exc, "response", None)
        if response is not None:
            values.extend([str(getattr(response, "text", "")), str(getattr(response, "content", ""))])
            try:
                values.append(str(response.json()))
            except Exception:
                pass
        return " ".join(values)
