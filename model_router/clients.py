"""Client protocol definitions and OpenAI-compatible model integrations."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, Protocol

from agent_core.models import StateBinding
from model_router.router import PROVIDER_KEY_ENV


DEFAULT_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

PROVIDER_BASE_URL_ENV: dict[str, str] = {
    "ark": "ARK_BASE_URL",
    "volcengine": "ARK_BASE_URL",
    "openai": "OPENAI_BASE_URL",
    "vlm": "VLM_BASE_URL",
}

PROVIDER_DEFAULT_BASE_URL: dict[str, str] = {
    "ark": DEFAULT_ARK_BASE_URL,
    "volcengine": DEFAULT_ARK_BASE_URL,
    "openai": DEFAULT_OPENAI_BASE_URL,
    "vlm": DEFAULT_ARK_BASE_URL,
}


class TextModelClient(Protocol):
    """Protocol for text-capable model clients."""

    def complete(self, prompt: str, stream_handler: Callable[[str], None] | None = None) -> str:
        """Return text completion for a prompt."""


class VisionLanguageModelClient(Protocol):
    """Protocol for VLM visual inspection clients."""

    def inspect(self, image_url: str, prompt: str) -> dict[str, object]:
        """Return a structured visual inspection payload."""


class OpenAICompatibleTextClient:
    """Synchronous chat-completion client for reasoning LLM states."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.parameters = parameters or {}

    def complete(self, prompt: str, stream_handler: Callable[[str], None] | None = None) -> str:
        """Call an OpenAI-compatible chat-completions endpoint."""

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai SDK is required for remote reasoning LLM calls.") from exc

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        request.update(self.parameters)
        if stream_handler is not None:
            request["stream"] = True
            chunks: list[str] = []
            completion = client.chat.completions.create(**request)
            with completion:
                for chunk in completion:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None)
                    if content is None:
                        content = getattr(delta, "reasoning_content", None)
                    if content:
                        chunks.append(content)
                        stream_handler(content)
            text = "".join(chunks)
            if not text:
                raise RuntimeError("推理模型返回了空响应。")
            return text

        response = client.chat.completions.create(**request)
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("推理模型返回了空响应。")
        return content


class OpenAICompatibleVisionLanguageClient:
    """Synchronous multimodal chat client for visual inspection states."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.parameters = parameters or {}

    def inspect(self, image_url: str, prompt: str) -> dict[str, object]:
        """Call a VLM and parse the expected JSON inspection object."""

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai SDK is required for remote VLM calls.") from exc

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        }
        request.update(self.parameters)
        response = client.chat.completions.create(**request)
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("Vision language model returned an empty response.")
        payload = json.loads(_extract_json_object(content))
        if not isinstance(payload, dict):
            raise RuntimeError("视觉语言模型响应必须是 JSON 对象。")
            
        # 保底容错归一化
        if "passed" in payload and "decision" not in payload:
            payload["decision"] = "pass" if payload["passed"] else "continue"
        if "confidence" not in payload:
            payload["confidence"] = 0.9
        else:
            try:
                payload["confidence"] = float(payload["confidence"])
            except Exception:
                payload["confidence"] = 0.9
        return payload


def build_text_client(binding: StateBinding) -> TextModelClient | None:
    """Create a reasoning client for a binding, or ``None`` when mock/offline."""

    api_key = _api_key_for_binding(binding)
    if not api_key:
        return None
    return OpenAICompatibleTextClient(
        base_url=_base_url_for_binding(binding),
        api_key=api_key,
        model=binding.model,
        parameters=binding.parameters,
    )


def build_vlm_client(binding: StateBinding) -> VisionLanguageModelClient | None:
    """Create a VLM client for a binding, or ``None`` when mock/offline."""

    api_key = _api_key_for_binding(binding)
    if not api_key:
        return None
    return OpenAICompatibleVisionLanguageClient(
        base_url=_base_url_for_binding(binding),
        api_key=api_key,
        model=binding.model,
        parameters=binding.parameters,
    )


def _api_key_for_binding(binding: StateBinding) -> str | None:
    """Resolve the provider API key from environment variables."""

    key_env = PROVIDER_KEY_ENV.get(binding.provider)
    if not key_env:
        return None
    return os.getenv(key_env)


def _base_url_for_binding(binding: StateBinding) -> str:
    """Resolve the provider base URL from env or provider defaults."""

    base_env = PROVIDER_BASE_URL_ENV.get(binding.provider)
    if base_env and os.getenv(base_env):
        return str(os.getenv(base_env))
    return PROVIDER_DEFAULT_BASE_URL.get(binding.provider, DEFAULT_OPENAI_BASE_URL)


def _extract_json_object(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    start = text.find("{")
    if start < 0:
        raise ValueError("模型响应中未找到 JSON 对象。")

    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text[start:])
        return json.dumps(obj, ensure_ascii=False)
    except Exception as exc:
        raise ValueError(f"无法解析 JSON 对象: {exc}") from exc