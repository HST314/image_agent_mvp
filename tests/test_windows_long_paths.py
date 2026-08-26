"""Windows MAX_PATH regressions for atomic workspace writes.

A managed workspace nests ``tasks/<task>/instances/<instance>/work/<instance>``,
and checkpoint temp files append a ``.{name}.{uuid32}.tmp`` suffix.  Combined,
the temp path can cross the legacy Windows 260-character limit and surface as
``FileNotFoundError`` while the final path itself still fits — exactly the
"艺术风格失败" failure seen after confirming a task book on a config branch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import storage.project_store as project_store_module
from configs.managed_runtime import ManagedRuntime
from storage.project_store import ProjectStore, atomic_json, long_path

MODEL_CONFIG = Path(__file__).resolve().parent / "fixtures" / "model_config.yaml"
RUNTIME_POLICY = Path(__file__).resolve().parent / "fixtures" / "runtime.yaml"


def test_long_path_prefixes_only_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = Path("/plain/absolute/path")

    if os.name == "nt":
        assert str(long_path(plain)).startswith("\\\\?\\")
    else:
        assert long_path(plain) == plain

    monkeypatch.setattr(project_store_module, "_WINDOWS", True)
    prefixed = long_path(plain)
    assert str(prefixed).startswith("\\\\?\\")
    # Already-extended paths stay untouched (idempotent prefixing).
    assert long_path(prefixed) == prefixed
    # UNC shares use the UNC form of the extended-length prefix.
    unc = long_path(Path(r"\\server\share\root"))
    assert str(unc) == "\\\\?\\UNC\\server\\share\\root"
    monkeypatch.setattr(project_store_module, "_WINDOWS", os.name == "nt")


def test_atomic_json_round_trips_when_temp_name_exceeds_max_path(
    tmp_path: Path,
) -> None:
    # Keep the final path under the legacy limit while the temp file
    # (``.{name}.{uuid32}.tmp`` adds 38 characters) crosses it.
    branch = "config-000002-2e533892"
    name = "000006-initial_candidate_generation.json"
    prefix = tmp_path / ("d" * 128) / "checkpoints" / branch
    target = prefix / name
    assert len(str(target)) + 38 > 260

    atomic_json(target, {"format_version": 1, "state": "initial_candidate_generation"})

    assert (
        json.loads(target.read_text(encoding="utf-8"))["state"]
        == "initial_candidate_generation"
    )
    assert not list(prefix.glob(".*.tmp"))


def test_checkpoint_save_survives_max_path_temp_names(tmp_path: Path) -> None:
    runtime = ManagedRuntime.from_paths(MODEL_CONFIG, RUNTIME_POLICY)
    final_relative = (
        Path("long-path-project")
        / "checkpoints"
        / "main"
        / "000001-initial_candidate_generation.json"
    )
    padding = 250 - len(str(tmp_path)) - len(str(final_relative)) - 1
    deep = tmp_path / ("d" * max(padding, 1))
    final_path = deep / final_relative
    # The final checkpoint path fits the legacy limit; its temp sibling does not.
    assert 240 < len(str(final_path)) < 260
    assert len(str(final_path)) + 38 > 260

    store = ProjectStore(deep, "long-path-project")
    store.create(runtime.policy.snapshot(), config_binding=runtime.branch_binding())
    checkpoint_id = store.checkpoint(
        "initial_candidate_generation",
        {
            "state": "initial_candidate_generation",
            "waiting": True,
            "task_card": {"task_id": "long-path-project"},
        },
    )

    loaded = store.checkpoints.load(checkpoint_id)
    assert loaded["state"] == "initial_candidate_generation"
