"""Stage F: concurrent branch readers must not deadlock the project lock.

On Windows the msvcrt backend has no shared-lock primitive, so concurrent
in-process readers used to pile onto one blocking exclusive file lock and
fail with EDEADLK (Errno 36).  The store now single-flights readers per
project on Windows, and the HTTP endpoint single-flights the whole read
per project with a short-TTL cache.
"""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import main_front
import storage.project_store as project_store_module
from storage.project_store import ProjectStore


def _project(tmp_path: Path, project_id: str = "concurrency") -> ProjectStore:
    store = ProjectStore(tmp_path, project_id)
    store.create({"offline_mode": True})
    store.checkpoint("confirmation_build", {"state": "confirmation_build", "phase": "waiting"})
    return store


def test_windows_read_lock_single_flights_50_concurrent_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """50 threads x branches() in Windows mode: no deadlock, no Errno 36,
    and never more than one thread inside the kernel lock."""
    # Create the project before flipping the Windows flag: the flag also
    # drives the MAX_PATH write shim, which only makes sense on a real
    # Windows kernel.  branches() itself is a pure read.
    store = _project(tmp_path)
    monkeypatch.setattr(project_store_module, "_WINDOWS", True)

    guard = threading.Lock()
    active = 0
    peak = 0
    real_lock = project_store_module.portalocker.lock

    def counting_lock(stream, mode, *args, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            # Widen the race window so ungated contention would show up.
            time.sleep(0.01)
            return real_lock(stream, mode, *args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(project_store_module.portalocker, "lock", counting_lock)

    errors: list[BaseException] = []
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=50) as pool:
        futures = [pool.submit(store.branches) for _ in range(50)]
        for future in futures:
            try:
                results.append(future.result(timeout=30))
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

    assert not errors
    assert len(results) == 50
    assert all(view["current_branch"] == "main" for view in results)
    assert peak == 1


def test_posix_read_lock_keeps_shared_reads(tmp_path: Path) -> None:
    store = _project(tmp_path)
    with store.read_lock():
        assert (store.root / ".lock").exists()


def test_list_project_branches_single_flights_a_50_request_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A burst of 50 concurrent GETs collapses to one store read; every
    caller still receives the same healthy branch projection."""
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    store = _project(tmp_path)

    calls = 0
    real_branches = ProjectStore.branches

    def counting_branches(self: ProjectStore) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        time.sleep(0.02)  # keep the leader busy so followers really queue
        return real_branches(self)

    monkeypatch.setattr(ProjectStore, "branches", counting_branches)

    async def burst() -> list[dict[str, Any]]:
        return await asyncio.gather(
            *[main_front.list_project_branches(store.project_id) for _ in range(50)]
        )

    results = asyncio.run(burst())

    assert len(results) == 50
    assert all(view["current_branch"] == "main" for view in results)
    assert calls == 1


def test_branch_view_cache_respects_ttl_and_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    store = _project(tmp_path)

    calls = 0
    real_branches = ProjectStore.branches

    def counting_branches(self: ProjectStore) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return real_branches(self)

    monkeypatch.setattr(ProjectStore, "branches", counting_branches)

    async def two_reads() -> None:
        await main_front.list_project_branches(store.project_id)
        await main_front.list_project_branches(store.project_id)

    asyncio.run(two_reads())
    assert calls == 1  # second read served from the short-TTL cache

    main_front._invalidate_branch_view(store.project_id)
    asyncio.run(main_front.list_project_branches(store.project_id))
    assert calls == 2
