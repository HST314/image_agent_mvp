"""Side-effect-free public health diagnostics with stable failure codes."""
from __future__ import annotations

import base64
import hashlib
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from model_router.router import ModelRouter
from storage.project_store import EventStore

LOGGER = logging.getLogger(__name__)


def _probe(name: str, code: str, trace_id: str, check: Callable[[], Any]) -> dict[str, Any]:
    try:
        details = check()
        result: dict[str, Any] = {"name": name, "status": "ok"}
        if isinstance(details, dict):
            result["metrics"] = details
        return result
    except Exception:
        LOGGER.exception("diagnostic probe failed trace_id=%s probe=%s", trace_id, name)
        return {"name": name, "status": "error", "error_code": code}


def run_diagnostics(*, projects_root: Path, model_config: Path, app_root: Path,
                    job_registry: object) -> dict[str, object]:
    """Run local readiness probes without exposing paths, credentials, or exceptions."""
    trace_id = f"trace_{uuid4().hex}"

    def model_route() -> None:
        router = ModelRouter.from_file(model_config)
        router.validate_required_bindings()

    def jobs() -> dict[str, Any]:
        if not getattr(job_registry, "is_ready")():
            raise RuntimeError("job executor is stopped")
        metrics = getattr(job_registry, "metrics", None)
        return metrics() if callable(metrics) else {}

    def storage() -> None:
        projects_root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(projects_root)
        if usage.free < 10 * 1024 * 1024:
            raise OSError("insufficient free space")

    def events() -> None:
        projects_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".health-", dir=projects_root) as raw:
            store = EventStore(Path(raw) / "events.jsonl")
            store.append("health_probe", trace_id=trace_id)
            if len(store.read_all()) != 1:
                raise OSError("event verification failed")

    def assets() -> None:
        from storage.project_store import ArtifactStore
        # Exercise the same persistence and integrity boundary used by the asset API.
        fixture = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory(prefix=".health-asset-", dir=projects_root) as raw:
            store = ArtifactStore(Path(raw))
            saved = store.save_bytes(fixture, suffix=".png", metadata={"probe": True})
            path, resolved = store.resolve(saved["artifact_id"])
            digest = hashlib.sha256(fixture).hexdigest()
            if path.read_bytes() != fixture or saved["sha256"] != digest or resolved["sha256"] != digest:
                raise OSError("asset verification failed")

    def resources() -> None:
        required = (
            app_root / "configs" / "runtime.yaml",
            app_root / "skills" / "category_skills" / "index.json",
            app_root / "skills" / "style_cards" / "index.json",
            app_root / "prompt_engine" / "templates" / "render_prompt.md",
        )
        if not all(item.is_file() and item.stat().st_size > 0 for item in required):
            raise FileNotFoundError("required runtime resource unavailable")

    checks = (
        ("model_router", "MODEL_ROUTER_UNAVAILABLE", model_route),
        ("job_executor", "JOB_EXECUTOR_UNAVAILABLE", jobs),
        ("storage", "STORAGE_UNAVAILABLE", storage),
        ("event_writer", "EVENT_WRITER_UNAVAILABLE", events),
        ("asset_api", "ASSET_API_UNAVAILABLE", assets),
        ("runtime_resources", "RUNTIME_RESOURCES_UNAVAILABLE", resources),
    )
    probes = [_probe(name, code, trace_id, check) for name, code, check in checks]
    ready = all(item["status"] == "ok" for item in probes)
    return {"status": "ok" if ready else "degraded", "trace_id": trace_id, "checks": probes}
