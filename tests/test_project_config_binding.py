"""ProjectStore configuration association transaction regressions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.models import ModelRole
from configs.managed_runtime import ManagedRuntime
from model_router.gateway import RuntimeModelGateway
from model_router.router import ModelRouter
from storage.project_store import ProjectStore


MODEL_CONFIG = Path(__file__).resolve().parent / "fixtures" / "model_config.yaml"
RUNTIME_POLICY = Path(__file__).resolve().parent / "fixtures" / "runtime.yaml"


def _checkpoint(store: ProjectStore, state: str = "intake_clarify") -> str:
    return store.checkpoint(
        state,
        {
            "state": state,
            "phase": "waiting_clarification",
            "waiting": True,
            "task_card": {"task_id": store.project_id},
        },
    )


def test_config_binding_follows_branch_switch_and_failed_fork_rolls_back(
    tmp_path: Path,
) -> None:
    runtime = ManagedRuntime.from_paths(MODEL_CONFIG, RUNTIME_POLICY)
    store = ProjectStore(tmp_path, "config-branch-project")
    store.create(
        runtime.policy.snapshot(), config_binding=runtime.branch_binding()
    )
    source = _checkpoint(store)
    revised = runtime.with_policy(
        runtime.policy.model_copy(update={"candidate_concurrency": 3})
    ).branch_binding(effective_from_state="confirmation_build")
    revised["runtime_config_revision_id"] = "cfg-inst-r000002"

    with store.lock():
        branch = store.branch_from(
            source,
            name="config-000002-store",
            config_binding=revised,
            config_apply_idempotency_key="store-apply-0002",
            config_apply_request_hash="a" * 64,
        )
    assert branch == "config-000002-store"
    assert store.active_config_binding()["runtime_config_revision_id"] == (
        "cfg-inst-r000002"
    )
    revised_head = _checkpoint(store)

    with store.lock():
        store.switch_branch(source)
    assert store.active_config_binding()["runtime_config_revision_id"] == (
        "cfg-inst-r000001"
    )
    with store.lock():
        store.switch_branch(revised_head)
    before_manifest = store.read_manifest()
    before_policy = json.loads(
        (store.root / "runtime_policy.json").read_text(encoding="utf-8")
    )

    failed = dict(revised)
    failed["runtime_config_revision_id"] = "cfg-inst-r000003"

    def stop_after_persist(_receipt: dict) -> None:
        raise OSError("simulated publication failure")

    with pytest.raises(OSError, match="publication failure"):
        with store.lock():
            store.branch_from(
                revised_head,
                name="config-000003-store",
                config_binding=failed,
                config_apply_idempotency_key="store-apply-0003",
                config_apply_request_hash="b" * 64,
                after_persist=stop_after_persist,
            )

    assert store.pending_transaction() is None
    assert store.read_manifest() == before_manifest
    assert "config-000003-store" not in {
        item["name"] for item in store.branches()["items"]
    }
    assert json.loads(
        (store.root / "runtime_policy.json").read_text(encoding="utf-8")
    ) == before_policy


def test_model_call_idempotency_is_scoped_to_revision_and_branch(
    tmp_path: Path,
) -> None:
    store = ProjectStore(tmp_path, "idempotency-project")
    store.create()
    calls = []
    for config_hash, revision_id, branch_id in (
        ("a" * 64, "cfg-inst-r000001", "main"),
        ("b" * 64, "cfg-inst-r000002", "config-000002-test"),
    ):
        router = ModelRouter.from_file(
            MODEL_CONFIG,
            config_hash=config_hash,
            revision_id=revision_id,
            branch_id=branch_id,
        )
        gateway = RuntimeModelGateway(store, router, offline_mode=True)
        gateway.call(
            "intake_clarify",
            ModelRole.REASONING_LLM,
            lambda _route: {"ok": True},
            messages=[{"role": "user", "content": "same-input"}],
            variables={},
            template_id="same-template",
            template_version="1",
            input_refs=[],
        )
        calls.append(
            [
                event
                for event in store.history()
                if event.get("type") == "model_call_started"
            ][-1]
        )
    assert calls[0]["idempotency_key"] != calls[1]["idempotency_key"]
    assert calls[1]["runtime_config_revision_id"] == "cfg-inst-r000002"
    assert calls[1]["branch_id"] == "config-000002-test"


def test_recovery_rolls_back_branch_when_publication_did_not_finish(
    tmp_path: Path,
) -> None:
    runtime = ManagedRuntime.from_paths(MODEL_CONFIG, RUNTIME_POLICY)
    store = ProjectStore(tmp_path, "config-crash-project")
    store.create(
        runtime.policy.snapshot(), config_binding=runtime.branch_binding()
    )
    source = _checkpoint(store)
    revised = runtime.with_policy(
        runtime.policy.model_copy(update={"candidate_concurrency": 2})
    ).branch_binding(effective_from_state="confirmation_build")
    revised["runtime_config_revision_id"] = "cfg-inst-r000002"

    def simulate_process_exit(_receipt: dict) -> None:
        raise SystemExit("simulated process exit")

    with pytest.raises(SystemExit, match="process exit"):
        with store.lock():
            store.branch_from(
                source,
                name="config-000002-crash",
                config_binding=revised,
                config_apply_idempotency_key="store-apply-crash",
                config_apply_request_hash="c" * 64,
                after_persist=simulate_process_exit,
            )

    assert store.pending_transaction() is not None
    store.recover_pending_transaction()
    assert store.pending_transaction() is None
    assert store.read_manifest()["current_branch"] == "main"
    assert store.find_config_apply("store-apply-crash") is None
