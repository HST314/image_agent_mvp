"""Windows-only release gate for portalocker's real Win32 backend."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires a real Windows kernel")


def test_fresh_install_serves_branches_with_real_windows_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Importing these modules is part of the dependency gate. Portalocker 3.2
    # instantiates Win32Locker even for an exclusive msvcrt lock.
    import pywintypes  # noqa: F401
    import portalocker.portalocker as portalocker_backend
    from fastapi.testclient import TestClient

    import main_front
    from storage.project_store import ProjectStore

    assert portalocker_backend.LOCKER.__name__ == "MsvcrtLocker"
    projects = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", projects)
    store = ProjectStore(projects, "windows-lock-gate")
    store.create({"offline_mode": True})

    response = TestClient(main_front.app).get("/api/projects/windows-lock-gate/branches")

    assert response.status_code == 200, response.text
    assert response.json()["current_branch"] == "main"
