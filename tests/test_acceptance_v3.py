from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_core.models import ImageTaskCard
from agent_core.workflow_runner import WorkflowRunner
from configs.runtime_policy import RuntimePolicy
from interaction.confirmation_builder import specification_from_task
from render_clients.ark_client import ArkImageRenderClient
from skills.errors import ResourceError
from storage.project_store import ProjectStore
from model_router.gateway import RuntimeModelGateway
from model_router.router import ModelRouter
from model_router.usage import capture_provider_usage
from workspace_cli import parser


def _task() -> ImageTaskCard:
    return ImageTaskCard.model_validate({
        "task_id": "t", "project_id": "p", "source_refs": [
            {"ref_id": "brief", "ref_type": "brief", "excerpt": "广告海报", "source_hash": None}],
        "deliverable_goal": "广告 海报", "usage_context": "内部审核",
        "category_ref": {"category_id": "generic", "version": "1"},
        "known_facts": {}, "unknowns": {}, "asset_inputs": [], "status": "draft",
    })


def test_t04_production_candidate_path_returns_structured_resource_error(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    policy = RuntimePolicy(offline_mode=True, allow_skill_degradation=False)
    store = ProjectStore(tmp_path, "p"); store.create(policy.snapshot())
    runner = WorkflowRunner(store, Path("tests/fixtures/model_config.yaml"), offline_mode=True)
    monkeypatch.setattr("skills.category_library_adapter.CategoryLibraryAdapter.__init__",
                        Mock(side_effect=FileNotFoundError("missing")))
    # No explicit category: the new front-loaded category matcher must surface
    # a structured resource error before any paid candidate work can begin.
    task = _task().model_copy(update={"category_ref": None}); spec = specification_from_task(task)
    with pytest.raises(ResourceError) as caught:
        runner._candidates({"task_card": task.model_dump(mode="json"),
                            "task_specification": spec.model_dump(mode="json")}, {})
    assert caught.value.code == "RESOURCE_MISSING"
    assert caught.value.trace_id.startswith("trace_")


def test_runtime_skill_degradation_switch_controls_category_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skills.category_library_adapter.CategoryLibraryAdapter.__init__",
                        Mock(side_effect=FileNotFoundError("missing")))

    blocked_store = ProjectStore(tmp_path, "category-blocked")
    blocked_store.create(RuntimePolicy(
        offline_mode=True, allow_skill_degradation=False,
    ).snapshot())
    blocked = WorkflowRunner(blocked_store, Path("tests/fixtures/model_config.yaml"), offline_mode=True)
    with pytest.raises(ResourceError):
        blocked._load_category_skill(_task().model_copy(update={"category_ref": None}))

    fallback_store = ProjectStore(tmp_path, "category-fallback")
    fallback_store.create(RuntimePolicy(
        offline_mode=True, allow_skill_degradation=True,
    ).snapshot())
    fallback = WorkflowRunner(fallback_store, Path("tests/fixtures/model_config.yaml"), offline_mode=True)
    skill, score = fallback._load_category_skill(
        _task().model_copy(update={"category_ref": None})
    )
    assert skill.category_id and score == 0
    assert any(
        event["type"] == "resource_degraded" and event["degradation"] == "fallback"
        for event in fallback_store.history()
    )


def test_runtime_skill_degradation_switch_controls_style_fallback(tmp_path: Path) -> None:
    missing = tmp_path / "missing-style-library"

    blocked_store = ProjectStore(tmp_path, "style-blocked")
    blocked_store.create(RuntimePolicy(
        offline_mode=True, allow_skill_degradation=False,
        style_library_root=str(missing),
    ).snapshot())
    blocked = WorkflowRunner(blocked_store, Path("tests/fixtures/model_config.yaml"), offline_mode=True)
    with pytest.raises(ResourceError):
        blocked._runtime_style_library()

    fallback_store = ProjectStore(tmp_path, "style-fallback")
    fallback_store.create(RuntimePolicy(
        offline_mode=True, allow_skill_degradation=True,
        style_library_root=str(missing),
    ).snapshot())
    fallback = WorkflowRunner(fallback_store, Path("tests/fixtures/model_config.yaml"), offline_mode=True)
    library, root = fallback._runtime_style_library()
    assert root != missing and len(library.records()) >= 5
    assert any(
        event["type"] == "resource_degraded" and event["resource"] == str(missing)
        for event in fallback_store.history()
    )


def test_t07_paid_image_sdk_receives_timeout_zero_retry_and_idempotency(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = Mock(); sdk.images.generate.return_value.data = [Mock(url="https://asset")]
    sdk.images.generate.return_value.model_dump.return_value = {"id": "image-provider-1"}
    constructor = Mock(return_value=sdk)
    monkeypatch.setattr("openai.OpenAI", constructor)
    client = ArkImageRenderClient(api_key="secret", timeout=12.5, max_retries=0,
                                  idempotency_key="idem-1")
    observed = []
    with capture_provider_usage(observed.append):
        result = client.render({"prompt": "x", "model": "m", "size": "2560x1440"})
    kwargs = constructor.call_args.kwargs
    assert kwargs["timeout"] == 12.5 and kwargs["max_retries"] == 0
    assert kwargs["default_headers"] == {"Idempotency-Key": "idem-1"}
    assert result["url"] == "https://asset"
    assert observed[0].billing_units == ({
        "unit": "image", "quantity": 1,
        "attributes": {"resolution": "2560x1440", "model_tier": "m"},
    },)


def test_t07_cli_exposes_unknown_query_and_manual_resolution() -> None:
    query = parser().parse_args(["unknown", "p"])
    resolve = parser().parse_args(["unknown", "p", "--idempotency-key", "k",
                                   "--action", "abandon", "--actor", "owner"])
    assert query.action is None
    assert (resolve.idempotency_key, resolve.action, resolve.actor) == ("k", "abandon", "owner")


def test_t07_unknown_resolution_survives_refresh_and_rejects_double_click(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "p"); store.create(RuntimePolicy(offline_mode=True).snapshot())
    store.events.append("model_call_unknown", idempotency_key="k", trace_id="trace", possible_charge=True)
    gateway = RuntimeModelGateway(store, ModelRouter.from_file(Path("tests/fixtures/model_config.yaml")), offline_mode=True)
    assert gateway.unknown_actions()[0]["idempotency_key"] == "k"
    gateway.resolve_unknown("k", "abandon", "owner")
    assert gateway.unknown_actions() == []
    with pytest.raises(ValueError, match="已经处置"):
        gateway.resolve_unknown("k", "abandon", "owner")
