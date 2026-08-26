"""Stage D: task-book timeout raised 180 -> 360 seconds.

The policy default and the OpenAI-compatible text client default must agree,
and a response that lands after 200s of model thinking must no longer
surface as a ``ModelCallError``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from configs.runtime_policy import RuntimePolicy
from model_router.clients import OpenAICompatibleTextClient
from model_router.executor import ModelExecutor


def test_runtime_policy_default_model_timeout_is_360() -> None:
    policy = RuntimePolicy()
    assert policy.model_timeout_seconds == 360
    # The upper bound is untouched by this stage.
    assert RuntimePolicy(model_timeout_seconds=3600).model_timeout_seconds == 3600


def test_text_client_default_timeout_matches_policy() -> None:
    client = OpenAICompatibleTextClient(
        base_url="https://example.test", api_key="key", model="model"
    )
    assert client.timeout == RuntimePolicy().model_timeout_seconds == 360


def test_slow_200s_completion_no_longer_raises_model_call_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200s provider response fits the new 360s budget.

    The fake SDK enforces the timeout it was constructed with, exactly like
    the real OpenAI client would; under the old 180s default this raised a
    timeout that ``ModelExecutor`` re-raised as ``ModelCallError``.
    """
    constructed: dict[str, float] = {}

    class SlowCompletions:
        def create(self, **request):
            elapsed = 200.0
            if elapsed > constructed["timeout"]:
                raise TimeoutError("request timed out")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="任务书正文"))],
                usage=None,
            )

    class FakeOpenAI:
        def __init__(self, *, api_key, base_url, timeout, max_retries):
            constructed["timeout"] = timeout
            self.chat = SimpleNamespace(completions=SlowCompletions())

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    client = OpenAICompatibleTextClient(
        base_url="https://example.test", api_key="key", model="model"
    )
    executor = ModelExecutor(sleeper=lambda _: None)
    result = executor.run(lambda: client.complete("写任务书"))

    assert result == "任务书正文"
    assert constructed["timeout"] == 360
