from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.batch import CandidateBatchError
from agent_core.models import SpecificationFact, TaskSpecification
from agent_core.render_spec import resolve_render_size
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from storage.project_store import ProjectStore


MODEL = "doubao-seedream-5-0-260128"


def _spec(value: str, *, label: str = "output_spec") -> TaskSpecification:
    return TaskSpecification(
        task_id="task",
        facts=[SpecificationFact(label=label, value=value, provenance="brief", status="confirmed")],
    ).finalized()


def _prepared(runner: WorkflowRunner, revision_hash: str) -> dict:
    plans = [
        {
            "style_id": f"style-{index}",
            "prompt_text": f"square prompt {index}",
            "extraction_key": f"extract-{index}",
            "prompt_version_id": f"prompt-{index}",
            "provenance": {
                "task_revision_hash": revision_hash,
                "config_hash": runner.policy.sha256(),
            },
        }
        for index in range(5)
    ]
    return {
        "render_plans": plans,
        "skill_invocations": {
            "style_library": {
                "selections": [
                    {"style_id": f"style-{index}", "style_name": f"方向 {index + 1}"}
                    for index in range(5)
                ]
            }
        },
        "skill_invocation_current": {"version_id": "skill-square"},
        "skill_invocation_history": [{"version_id": "skill-square"}],
    }


@pytest.mark.parametrize("value", ["正方形图片", "1:1 方图", "1080 × 1080 PNG"])
def test_square_task_spec_resolves_to_provider_safe_square(value: str) -> None:
    decision = resolve_render_size(_spec(value), MODEL, "2560x1440")
    assert decision.size == "2048x2048"
    assert decision.source in {"task_aspect_ratio", "task_exact_size"}


def test_small_explicit_size_is_upscaled_without_losing_requested_ratio() -> None:
    decision = resolve_render_size(_spec("1080 × 1440 PNG"), MODEL, "2560x1440")
    assert decision.size == "1664x2240"
    width, height = map(int, decision.size.split("x"))
    assert width * height >= 3_686_400
    assert width / height == pytest.approx(3 / 4, rel=0.01)


def test_conflicting_task_dimensions_are_rejected_before_render() -> None:
    with pytest.raises(ValueError, match="像素尺寸与宽高比冲突"):
        resolve_render_size(_spec("2560x1440，同时要求 1:1 正方形"), MODEL, "2560x1440")


def test_unspecified_task_keeps_runtime_default() -> None:
    spec = TaskSpecification(task_id="task", facts=[]).finalized()
    decision = resolve_render_size(spec, MODEL, "2560x1440")
    assert decision.size == "2560x1440"
    assert decision.source == "runtime_default"


def test_reasoning_model_output_format_alias_is_resolved() -> None:
    decision = resolve_render_size(
        _spec("以正方形图片为交付基线，建议不低于1080×1080像素", label="output_format_details"),
        MODEL,
        "2560x1440",
    )
    assert decision.size == "2048x2048"


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("aspect_ratio", "3:4"),
        ("orientation", "竖向"),
        ("画面比例", "3:4 竖版"),
        ("比例", "3:4"),
    ],
)
def test_confirmation_fact_label_aliases_are_resolved(label: str, value: str) -> None:
    decision = resolve_render_size(_spec(value, label=label), MODEL, "2560x1440")
    assert decision.size == "1664x2240"
    assert decision.source == "task_aspect_ratio"


@pytest.mark.parametrize("value", ["竖版海报", "竖向海报", "竖构图海报"])
def test_portrait_direction_keywords_default_to_3x4(value: str) -> None:
    decision = resolve_render_size(_spec(value), MODEL, "2560x1440")
    assert decision.size == "1664x2240"
    assert decision.source == "task_aspect_ratio"


def test_phone_portrait_keyword_still_resolves_to_9x16() -> None:
    decision = resolve_render_size(_spec("竖版手机海报"), MODEL, "2560x1440")
    assert decision.size == "1472x2560"


def test_explicit_numeric_ratio_outranks_direction_keyword_in_long_text() -> None:
    decision = resolve_render_size(
        _spec("海报采用2:3（宽:高）的竖向比例，整体为竖构图", label="aspect_ratio"),
        MODEL,
        "2560x1440",
    )
    assert decision.size == "1600x2368"
    assert decision.source == "task_aspect_ratio"
    width, height = map(int, decision.size.split("x"))
    # Each side is rounded up to the 64-pixel grid, which tilts 2:3 slightly.
    assert width / height == pytest.approx(2 / 3, rel=0.02)


def test_explicit_numeric_ratio_outranks_opposing_direction_keyword() -> None:
    decision = resolve_render_size(_spec("16:9 竖版"), MODEL, "2560x1440")
    assert decision.size == "2560x1472"
    assert decision.source == "task_aspect_ratio"


def test_conflicting_direction_keywords_without_numbers_are_rejected() -> None:
    with pytest.raises(ValueError, match="互相冲突的宽高比"):
        resolve_render_size(_spec("竖版与横版混排"), MODEL, "2560x1440")


def test_candidate_generation_freezes_task_size_and_cache_scope(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "square-project")
    store.create({"offline_mode": True})
    runner = WorkflowRunner(store, Path("tests/fixtures/model_config.yaml"), offline_mode=True)
    revision_hash = "revision-square"
    prepared = _prepared(runner, revision_hash)
    boundary = runner._freeze_render_boundary({
        "task_specification": _spec("正方形图片").model_dump(mode="json"),
        "task_revision": {"revision_hash": revision_hash},
    }, prepared)
    result = runner._render_candidates(boundary, prepared)

    assert result["render_size"] == "2048x2048"
    assert result["render_size_source"] == "task_aspect_ratio"
    assert len(result["candidates"]) == 5
    generated = [event for event in store.history() if event["type"] == "candidate_succeeded"]
    assert len(generated) == 5
    assert all(event["cache_scope"]["render_size"] == "2048x2048" for event in generated)


def test_partial_failure_retry_reuses_persisted_size_and_successful_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ProjectStore(tmp_path, "square-retry")
    store.create({"offline_mode": True})
    runner = WorkflowRunner(store, Path("tests/fixtures/model_config.yaml"), offline_mode=True)
    revision_hash = "revision-square-retry"
    prepared = _prepared(runner, revision_hash)
    waiting = {
        "state": "initial_candidate_generation",
        "phase": "waiting_skill_approval",
        "task_specification": _spec("正方形图片").model_dump(mode="json"),
        "task_revision": {"revision_hash": revision_hash},
        **prepared,
    }
    original_image_call = runner._image_call
    calls: dict[int, list[str | None]] = {}
    fail_once = {2}

    class RetryableRenderError(RuntimeError):
        category = "transport_unknown"
        retryable = True

    def flaky_image_call(state, prompt, references, *, index=None, size=None):
        assert index is not None
        calls.setdefault(index, []).append(size)
        persisted = store.resume()
        assert persisted is not None
        assert persisted["phase"] == "skill_approved_pending_render"
        assert persisted["render_size"] == "2048x2048"
        if index in fail_once:
            fail_once.remove(index)
            raise RetryableRenderError("temporary candidate failure")
        return original_image_call(state, prompt, references, index=index, size=size)

    monkeypatch.setattr(runner, "_image_call", flaky_image_call)
    with pytest.raises(CandidateBatchError):
        runner.run(
            waiting,
            RunnerOptions(skill_action="approve", actor="reviewer"),
            only_state="initial_candidate_generation",
        )

    frozen = store.resume()
    assert frozen is not None
    assert frozen["render_size"] == "2048x2048"
    assert frozen["render_size_source"] == "task_aspect_ratio"
    assert frozen["requested_output_spec"] == "正方形图片"
    assert len([event for event in store.history() if event["type"] == "candidate_succeeded"]) == 4

    # Mutable defaults and current bindings may change between attempts. The
    # approved paid boundary remains authoritative, and cached successes remain
    # in scope so only the failed slot is charged again.
    runner.policy = runner.policy.model_copy(update={"default_output_size": "4096x4096"})
    binding = runner.gateway.router.binding_for_state("initial_candidate_generation")
    runner.gateway.router._bindings["initial_candidate_generation"] = binding.model_copy(
        update={"model": "replacement-image-model"}
    )
    recovered = runner.run(frozen, RunnerOptions(), only_state="initial_candidate_generation")

    assert recovered["render_size"] == "2048x2048"
    assert len(recovered["candidates"]) == 5
    assert {index: len(sizes) for index, sizes in calls.items()} == {
        0: 1, 1: 1, 2: 2, 3: 1, 4: 1,
    }
    assert {size for sizes in calls.values() for size in sizes} == {"2048x2048"}
