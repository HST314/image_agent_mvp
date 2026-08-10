from pathlib import Path

import pytest

from configs.runtime_policy import RuntimePolicy
from main_front import _capabilities
from render_clients.payload_mapper import build_render_payload, validate_render_size


@pytest.mark.parametrize("size", ["1024x1024", "2560x1400"])
def test_seedream_rejects_too_small_dimensions_before_render(size: str) -> None:
    with pytest.raises(ValueError, match="图片尺寸不合法"):
        validate_render_size("doubao-seedream-5-0-260128", size)


def test_seedream_accepts_minimum_dimensions() -> None:
    payload = build_render_payload("doubao-seedream-5-0-260128", "prompt", "2560x1440", {})
    assert payload["size"] == "2560x1440"


def test_runtime_default_is_provider_valid() -> None:
    policy = RuntimePolicy.from_file(Path(__file__).parents[1] / "configs/runtime.yaml")
    assert policy.default_output_size == "2560x1440"


def test_parameter_failure_does_not_offer_retry() -> None:
    manifest = {"failed_step": {"error": {"category": "invalid_input", "retryable": False}}}
    assert _capabilities(manifest, {}) == []
