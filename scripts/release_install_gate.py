"""Run release installation gates in isolated target directories."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def target_env(target: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target)
    return env


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="image-agent-release-") as raw_tmp:
        tmp = Path(raw_tmp)
        wheel_dir, wheel_target, lock_target, outside = (
            tmp / "wheel", tmp / "wheel-target", tmp / "lock-target", tmp / "outside"
        )
        for path in (wheel_dir, wheel_target, lock_target, outside):
            path.mkdir()

        run(sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "--wheel-dir", str(wheel_dir), cwd=outside)
        wheel = next(wheel_dir.glob("image_agent_mvp-*.whl"))
        with ZipFile(wheel) as archive:
            metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
            metadata = archive.read(metadata_name).decode()
        require("Requires-Dist: portalocker" in metadata, "wheel metadata is missing portalocker")
        require(
            "Requires-Dist: pywin32" in metadata and "platform_system == \"Windows\"" in metadata,
            "wheel metadata is missing the Windows portalocker backend",
        )
        require("Requires-Dist: fastapi" in metadata, "wheel metadata is missing fastapi")

        run(sys.executable, "-m", "pip", "install", str(wheel), "--target", str(wheel_target), cwd=outside)
        runner_e2e = r'''
from pathlib import Path
import configs
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from storage.project_store import ProjectStore
import model_router.executor

config = Path(configs.__file__).with_name("model_config.yaml")
task = {"task_id":"wheel", "project_id":"installed", "source_refs":[{"ref_id":"brief","ref_type":"text"}],
        "deliverable_goal":"海报", "usage_context":"手机", "known_facts":{"主体":"产品"}, "unknowns":{}}

provider_calls = []
def forbidden_provider_call(*args, **kwargs):
    provider_calls.append((args, kwargs))
    raise RuntimeError("offline release fixture attempted a real provider call")
model_router.executor.ModelExecutor.execute = forbidden_provider_call

def execute_fixture(project_id):
    root = Path.cwd() / "projects"
    store = ProjectStore(root, project_id); store.create()
    runner = WorkflowRunner(store, config, offline_mode=True)
    state = runner.run({"task_card": {**task, "project_id": project_id}}, RunnerOptions(), only_state="intake_clarify")
    state = runner.run(state, RunnerOptions(task_approved=True, actor="release"), only_state="confirmation_build")
    state = runner.run(state, RunnerOptions(), only_state="initial_candidate_generation")
    candidates = state.get("candidates", [])
    if len(candidates) != 5:
        raise RuntimeError(f"offline fixture returned {len(candidates)} candidates instead of 5")
    return [(item.get("candidate_index"), item.get("id"), item.get("style_id")) for item in candidates]

first = execute_fixture("installed-first")
second = execute_fixture("installed-second")
if first != second:
    raise RuntimeError(f"offline fixture identifiers are unstable: {first!r} != {second!r}")
if provider_calls:
    raise RuntimeError(f"offline fixture made {len(provider_calls)} real provider calls")
print("WHEEL_RUNNER_E2E_OK")
'''
        run(sys.executable, "-c", runner_e2e, cwd=outside, env=target_env(wheel_target))

        run(sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.lock"),
            "--target", str(lock_target), cwd=outside)
        run(sys.executable, "-m", "pytest", "-q", cwd=ROOT, env=target_env(lock_target))


if __name__ == "__main__":
    main()
