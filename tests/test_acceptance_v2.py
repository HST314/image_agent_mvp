import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from configs.runtime_policy import RuntimePolicy
from model_router.executor import ModelCallError, ModelExecutor
from skills.errors import ResourceError
from skills.resource_loader import load_with_policy
from skills.style_loader import load_style_card
from storage.asset_store import AssetStoreProtocol
from storage.asset_store import AssetStore
from agent_core.models import CandidateAsset
from storage.project_store import ProjectStore, atomic_json


def test_t02_every_policy_field_has_consumer_and_revision_needs_confirmation(tmp_path: Path):
    policy = RuntimePolicy.from_file("configs/runtime.yaml")
    assert set(RuntimePolicy.model_fields) == set(policy.consumer_matrix())
    store = ProjectStore(tmp_path, "p"); store.create(policy.snapshot())
    store.checkpoint("safe", {"ok": True})
    with pytest.raises(PermissionError):
        store.revise_policy(policy.snapshot(), confirmed=False, actor="owner")
    branch = store.revise_policy({**policy.snapshot(), "watermark": True}, confirmed=True, actor="owner")
    assert branch.startswith("policy-")
    assert any(e["type"] == "runtime_policy_revised" for e in store.history())


def test_t03_installed_resources_are_declared():
    manifest = Path("MANIFEST.in").read_text()
    for resource in ("configs", "prompt_engine/templates", "skills", "schemas", "examples/contracts"):
        assert resource in manifest


def test_t04_resource_error_and_explicit_degradation(tmp_path: Path):
    with pytest.raises(ResourceError) as caught:
        load_style_card(tmp_path / "missing.json", trace_id="trace_test")
    assert caught.value.as_dict() == {
        "code": "RESOURCE_MISSING", "resource": str(tmp_path / "missing.json"),
        "trace_id": "trace_test", "degradation": "blocked", "detail": ""}
    emitted = []
    fallback = object()
    assert load_with_policy(lambda: load_style_card(tmp_path / "missing.json"),
                            resource="style", allow_degradation=True,
                            fallback=fallback, emit=emitted.append) is fallback
    assert emitted[0]["degradation"] == "fallback"


def test_t05_store_interface_and_reference_protocol(tmp_path: Path):
    assert hasattr(AssetStoreProtocol, "append") and hasattr(AssetStoreProtocol, "read_all")
    store = AssetStore(tmp_path)
    for forbidden in ("mock://x", "http://x", "https://x", "file:///x"):
        asset = CandidateAsset(task_id="t", project_id="p", prompt_version_id="v",
                               style_id="s", category_id="c", url=forbidden)
        with pytest.raises(ValueError, match="ASSET_REF_UNSTABLE"):
            store.append(asset)


@pytest.mark.parametrize("exc,category", [
    (TimeoutError("x"), "timeout_unknown"),
    (ConnectionError("x"), "transport_unknown"),
])
def test_t07_unknown_failures_never_retry(exc, category):
    call = Mock(side_effect=exc)
    with pytest.raises(ModelCallError) as caught:
        ModelExecutor(max_attempts=9).run(call)
    assert caught.value.category == category and call.call_count == 1


def test_t10_crash_after_checkpoint_file_before_index_recovers(tmp_path: Path):
    store = ProjectStore(tmp_path, "p"); store.create(); store.checkpoint("safe", {"v": 1})
    prepared = store.checkpoints.prepare("main", 2, "next", {"v": 2})
    pending = store.root / "transactions/pending.json"
    atomic_json(pending, {"format_version": 1, "kind": "checkpoint", "status": "intent",
                         "branch": "main", "sequence": 2, "state": "next", "data": {"v": 2},
                         "checkpoint_id": prepared["checkpoint_id"], "path": prepared["path"],
                         "checksum": prepared["checksum"]})
    target = store.root / prepared["path"]
    atomic_json(target, prepared["envelope"])
    assert target.exists()
    assert store.resume() == {"v": 1}
    assert target.exists() and pending.exists(), "只读恢复不得改写正在进行的事务"
    store.recover_pending_transaction()
    assert not target.exists() and not pending.exists()
    assert store.checkpoint("next", {"v": 2}).startswith("checkpoint_")


def test_t10_branch_crash_rolls_back_and_path_names_are_rejected(tmp_path: Path):
    store = ProjectStore(tmp_path, "p"); store.create(); source = store.checkpoint("safe", {"v": 1})
    with pytest.raises(ValueError):
        store.branch_from(source, name="../escape")
    prepared = store.checkpoints.prepare("repair", 1, "safe", {"v": 1})
    branches_path = store.root / "branches.json"
    branches = json.loads(branches_path.read_text())
    branches["branches"]["repair"] = {"parent": "main", "from_checkpoint": source}
    atomic_json(branches_path, branches)
    atomic_json(store.root / "transactions/pending.json", {
        "format_version": 1, "kind": "branch", "status": "intent", "branch": "repair",
        "sequence": 1, "state": "safe", "data": {"v": 1}, "from_checkpoint": source,
        "checkpoint_id": prepared["checkpoint_id"], "path": prepared["path"],
        "checksum": prepared["checksum"]})
    assert store.resume() == {"v": 1}
    assert "repair" in json.loads(branches_path.read_text())["branches"]
    store.recover_pending_transaction()
    assert "repair" not in json.loads(branches_path.read_text())["branches"]
