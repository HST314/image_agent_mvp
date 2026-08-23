"""Release-package regression tests added for need acceptance v4."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_wheel_serves_ui_outside_source_tree(tmp_path: Path) -> None:
    wheel_dir = tmp_path / "wheel"
    target = tmp_path / "installed"
    outside = tmp_path / "outside"
    wheel_dir.mkdir()
    target.mkdir()
    outside.mkdir()

    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "--wheel-dir", str(wheel_dir)],
        check=True,
        cwd=outside,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("image_agent_mvp-*.whl"))
    subprocess.run(
        [sys.executable, "-m", "pip", "install", str(wheel), "--no-deps", "--target", str(target)],
        check=True,
        cwd=outside,
        capture_output=True,
        text=True,
    )
    script = """
from fastapi.testclient import TestClient
import main_front
page = TestClient(main_front.app, raise_server_exceptions=False).get('/')
assert page.status_code == 200, page.text
assert 'data-view="settings"' not in page.text
assert 'data-unknown' in page.text
print('INSTALLED_UI_OK')
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(target), env.get("PYTHONPATH")]))
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        cwd=outside,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "INSTALLED_UI_OK"
