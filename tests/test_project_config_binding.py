"""ProjectStore configuration association transaction regressions."""

from __future__ import annotations

import json
from pathlib import Path

import main_front
import pytest
import storage.project_store as project_store_module
from agent_core.models import ModelRole
from configs.managed_runtime import ManagedRuntime
from model_router.gateway import RuntimeModelGateway
from model_router.router import ModelRouter
from storage.project_store import CorruptProjectError, ProjectStore, atomic_json

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


def test_owner_supplied_initial_revision_keeps_exact_file_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_front, "CONFIG_ROOT", None)
    runtime = main_front._base_runtime()
    store = ProjectStore(tmp_path, "managed-initial-project")
    store.create(
        runtime.policy.snapshot(), config_binding=runtime.branch_binding()
    )

    restored = main_front._project_runtime(store)

    assert restored.revision_id == "cfg-inst-r000001"
    assert restored.runtime_config_sha256 == runtime.runtime_config_sha256
    assert restored.model_config_sha256 == runtime.model_config_sha256
    assert restored.config_hash == runtime.config_hash


def test_project_store_rejects_internally_inconsistent_config_hashes(
    tmp_path: Path,
) -> None:
    runtime = ManagedRuntime.from_paths(MODEL_CONFIG, RUNTIME_POLICY)
    invalid = runtime.branch_binding()
    invalid["config_hash"] = "f" * 64

    with pytest.raises(ValueError, match="总哈希"):
        ProjectStore(tmp_path, "invalid-binding-project").create(
            runtime.policy.snapshot(), config_binding=invalid
        )


@pytest.mark.parametrize(
    ("field", "tampered"),
    (
        ("runtime_config_revision_id", "cfg-inst-r999999"),
        ("runtime_policy_hash", "f" * 64),
        ("model_config_hash", "f" * 64),
        ("config_hash", "f" * 64),
    ),
)
def test_active_config_binding_drift_fails_closed_with_field_diagnostic(
    tmp_path: Path, field: str, tampered: str
) -> None:
    runtime = ManagedRuntime.from_paths(MODEL_CONFIG, RUNTIME_POLICY)
    binding = runtime.branch_binding()
    store = ProjectStore(tmp_path, f"binding-drift-{field.replace('_', '-')}")
    store.create(runtime.policy.snapshot(), config_binding=binding)
    branches_path = store.root / "branches.json"
    branches = json.loads(branches_path.read_text(encoding="utf-8"))
    branches["branches"]["main"][field] = tampered
    atomic_json(branches_path, branches)

    with pytest.raises(CorruptProjectError, match=rf"field={field},"):
        store.ensure_active_config_binding(binding)


def test_project_runner_keeps_legacy_binding_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_front, "CONFIG_ROOT", None)
    runtime = main_front._base_runtime()
    store = ProjectStore(tmp_path, "legacy-binding-project")
    store.create(runtime.policy.snapshot())

    runner = main_front._project_runner(store)
    store.ensure_active_config_binding(runner.config_binding)

    binding = store.active_config_binding()
    assert binding["runtime_config_revision_id"] == runtime.revision_id
    assert binding["effective_from_state"] == "initial"


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


def test_recovery_finishes_committed_before_start_config_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ManagedRuntime.from_paths(MODEL_CONFIG, RUNTIME_POLICY)
    store = ProjectStore(tmp_path, "before-start-crash-project")
    store.create(
        runtime.policy.snapshot(), config_binding=runtime.branch_binding()
    )
    revised_runtime = runtime.with_policy(
        runtime.policy.model_copy(update={"candidate_concurrency": 2})
    )
    revised = revised_runtime.branch_binding(effective_from_state="initial")
    revised["runtime_config_revision_id"] = "cfg-inst-r000002"
    real_atomic_json = project_store_module.atomic_json

    def crash_after_control_commit(path: Path, value: object) -> None:
        real_atomic_json(path, value)
        if path == store.root / "branches.json" and isinstance(value, dict):
            if value.get("config_applies"):
                raise SystemExit("simulated exit after config control commit")

    monkeypatch.setattr(
        project_store_module, "atomic_json", crash_after_control_commit
    )
    with pytest.raises(SystemExit, match="config control commit"):
        with store.lock():
            store.apply_config_before_start(
                revised,
                idempotency_key="before-start-crash-0001",
                request_hash="d" * 64,
            )

    assert store.pending_transaction() is not None
    monkeypatch.setattr(project_store_module, "atomic_json", real_atomic_json)
    store.recover_pending_transaction()

    assert store.pending_transaction() is None
    assert store.active_config_binding()["runtime_config_revision_id"] == (
        "cfg-inst-r000002"
    )
    receipt = store.find_config_apply("before-start-crash-0001")
    assert receipt is not None
    assert receipt["status"] == "APPLIED_BEFORE_START"
    assert len(
        [
            event
            for event in store.history()
            if event.get("type") == "config_revision_applied"
        ]
    ) == 1
