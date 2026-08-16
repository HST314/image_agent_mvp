"""Regression tests for non-UTF-8 platform default encodings.

On Chinese Windows the default text encoding is GBK (cp936).  Every control
file in a project workspace is written as UTF-8 by ``atomic_json``; any read
that forgets ``encoding="utf-8"`` therefore misdecodes Chinese branch names
(mojibake paths) or crashes outright, which used to make branch creation /
switch / rollback fail on every stage.  Rollback itself must also never abort
midway on an unreadable index, otherwise the project deadlocks in a
half-rolled-back state with ``pending.json`` left behind.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from storage.project_store import ProjectStore, atomic_json


@pytest.fixture()
def gbk_default_encoding(monkeypatch):
    """Simulate Chinese Windows: text opened without an explicit encoding
    defaults to GBK.  pathlib resolves default encodings through the
    ``io.text_encoding`` attribute, so patching it reproduces cp936 behavior
    for every ``read_text()`` / ``Path.open()`` call site."""
    real = io.text_encoding

    def gbk(encoding, stacklevel=1):
        return "gbk" if encoding is None else real(encoding, stacklevel)

    monkeypatch.setattr(io, "text_encoding", gbk)
    # sanity: UTF-8 bytes read without an encoding now surface as GBK mojibake
    assert "艺术".encode("utf-8").decode("gbk") == "鑹烘湳"


def _store(tmp_path: Path) -> tuple[ProjectStore, str]:
    store = ProjectStore(tmp_path, "gbk_project")
    store.create({"offline_mode": True})
    source = store.checkpoint("confirmation_build", {"state": "confirmation_build", "phase": "waiting"})
    return store, source


def test_chinese_branch_create_and_switch_under_gbk_default(tmp_path, gbk_default_encoding):
    store, source = _store(tmp_path)

    branch = store.branch_from(source, name="艺术风格-0816-121540", mode="rerun_stage")
    assert branch == "艺术风格-0816-121540"

    main_head = next(item["checkpoint_id"] for item in store.checkpoints.list() if item["branch"] == "main")
    store.switch_branch(main_head)
    chinese_head = next(item["checkpoint_id"] for item in store.checkpoints.list() if item["branch"] == branch)
    store.switch_branch(chinese_head)
    assert store.manifest()["current_branch"] == branch


def test_failed_branch_rolls_back_without_garbling_index_under_gbk_default(tmp_path, gbk_default_encoding):
    store, source = _store(tmp_path)
    branch = store.branch_from(source, name="艺术风格-0816-121540")
    chinese_head = store.manifest()["current_checkpoint"]["checkpoint_id"]
    previous_manifest = store.manifest()
    index_bytes = store.checkpoints.index_path.read_bytes()

    def fail_verify() -> None:
        raise FileNotFoundError("simulated projection failure")

    with pytest.raises(FileNotFoundError):
        store.branch_from(chinese_head, name="主图选择-0816-122000", verify=fail_verify)

    assert store.manifest() == previous_manifest
    assert not (store.root / "transactions/pending.json").exists()
    assert not (store.root / "checkpoints" / "主图选择-0816-122000").exists()
    assert "主图选择-0816-122000" not in {item["branch"] for item in store.checkpoints.list()}
    # rollback must not rewrite the index through the wrong encoding
    assert store.checkpoints.index_path.read_bytes() == index_bytes

    # the project is not deadlocked: another Chinese branch can still be created
    follow_up = store.branch_from(chinese_head, name="艺术风格-0816-123111")
    assert store.manifest()["current_branch"] == follow_up


def test_rollback_with_unreadable_index_still_clears_pending(tmp_path):
    """A checkpoint index that cannot be decoded at all (the legacy failure
    mode of the GBK bug) must not abort rollback: cleanup falls back to the
    paths/ids recorded in the transaction intent and pending.json is cleared."""
    store, source = _store(tmp_path)
    previous_manifest = store.manifest()
    source_envelope = store.checkpoints.load(source)
    branch = "乱码索引分支"
    prepared = store.checkpoints.prepare(branch, 1, source_envelope["state"], source_envelope["data"])
    checkpoint_id, relative, checksum = store.checkpoints.save(
        branch, 1, source_envelope["state"], source_envelope["data"], prepared=prepared,
    )
    branches_path = store.root / "branches.json"
    branches = json.loads(branches_path.read_text(encoding="utf-8"))
    branches["branches"][branch] = {"parent": "main", "from_checkpoint": source}
    atomic_json(branches_path, branches)
    atomic_json(store.root / "transactions/pending.json", {
        "format_version": 1, "kind": "branch", "status": "prepared", "branch": branch,
        "sequence": 1, "state": source_envelope["state"], "data": source_envelope["data"],
        "from_checkpoint": source, "checkpoint_id": checkpoint_id, "path": relative,
        "checksum": checksum, "previous_manifest": previous_manifest,
    })
    # make the index undecodable in any encoding
    store.checkpoints.index_path.write_bytes(b"\xff\xfe\xff" + store.checkpoints.index_path.read_bytes())

    store.recover_pending_transaction()  # must not raise

    assert store.manifest() == previous_manifest
    assert not (store.root / "transactions/pending.json").exists()
    assert not (store.root / relative).exists()
    assert branch not in json.loads(branches_path.read_text(encoding="utf-8"))["branches"]
