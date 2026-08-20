"""Regressions for the distributable wheel's dependency metadata."""
from __future__ import annotations

from email.parser import BytesParser
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]


def test_wheel_metadata_contains_all_runtime_dependencies(tmp_path: Path) -> None:
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "--wheel-dir", str(wheel_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("image_agent_mvp-*.whl"))
    with ZipFile(wheel) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_name))

    requirements = metadata.get_all("Requires-Dist", [])
    for package in ("pydantic", "PyYAML", "openai", "Pillow", "fastapi", "portalocker", "pywin32"):
        assert any(item.lower().startswith(package.lower()) for item in requirements), requirements
    windows_backend = next(item for item in requirements if item.lower().startswith("pywin32"))
    assert 'platform_system == "Windows"' in windows_backend


def test_setup_adapter_does_not_duplicate_dependency_metadata() -> None:
    adapter = (ROOT / "setup.py").read_text(encoding="utf-8")
    assert 'install_requires=PROJECT["dependencies"]' in adapter
    assert "portalocker>=" not in adapter
    assert "pywin32>=" not in adapter
    assert "fastapi>=" not in adapter
