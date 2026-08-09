"""Small, auditable in-process job executor used by the HTTP adapter."""
from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobRegistry:
    """Persist job facts while deliberately not promising restart recovery."""

    def __init__(self, root: Path, workers: int = 5) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="image-job")
        self._lock = threading.RLock()
        self._cancel: set[str] = set()
        self._interrupt_orphans()

    def _path(self, job_id: str) -> Path:
        if not job_id.startswith("job_") or not job_id[4:].isalnum():
            raise ValueError("JOB_ID_INVALID")
        return self.root / f"{job_id}.json"

    def _write(self, record: dict[str, Any]) -> None:
        path = self._path(record["job_id"])
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def _interrupt_orphans(self) -> None:
        for path in self.root.glob("job_*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if item.get("status") in {"queued", "running", "cancelling"}:
                    item.update(status="interrupted", finished_at=_now(), error={"code":"PROCESS_RESTARTED","message":"服务重启，在途任务未恢复且不会自动补调用。"})
                    self._write(item)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue

    def submit(self, project_id: str, idempotency_key: str, operation: str,
               execute: Callable[[], Any]) -> tuple[dict[str, Any], bool]:
        with self._lock:
            for path in self.root.glob("job_*.json"):
                item = json.loads(path.read_text(encoding="utf-8"))
                if item.get("project_id") == project_id and item.get("idempotency_key") == idempotency_key:
                    return item, False
            job_id = "job_" + uuid.uuid4().hex
            record = {"job_id":job_id, "project_id":project_id, "operation":operation,
                      "idempotency_key":idempotency_key, "status":"queued", "created_at":_now(),
                      "events":[{"seq":1,"type":"queued","timestamp":_now()}]}
            self._write(record)
            self._pool.submit(self._run, job_id, execute)
            return record, True

    def _event(self, record: dict[str, Any], kind: str, **payload: Any) -> None:
        record["events"].append({"seq":len(record["events"])+1,"type":kind,"timestamp":_now(),**payload})

    def _run(self, job_id: str, execute: Callable[[], Any]) -> None:
        with self._lock:
            record = self.get(job_id); record.update(status="running", started_at=_now())
            self._event(record, "running"); self._write(record)
        try:
            if job_id in self._cancel:
                raise InterruptedError("任务在开始前已取消。")
            result = execute()
            with self._lock:
                record = self.get(job_id)
                if job_id in self._cancel:
                    record.update(status="cancelled", finished_at=_now())
                    self._event(record, "cancelled")
                else:
                    record.update(status="succeeded", finished_at=_now(), result=result)
                    self._event(record, "succeeded")
                self._write(record)
        except InterruptedError:
            with self._lock:
                record = self.get(job_id); record.update(status="cancelled", finished_at=_now())
                self._event(record, "cancelled"); self._write(record)
        except Exception as exc:
            with self._lock:
                record = self.get(job_id); record.update(status="failed", finished_at=_now(), error={"code":exc.__class__.__name__,"message":str(exc)})
                self._event(record, "failed", error=record["error"]); self._write(record)

    def get(self, job_id: str) -> dict[str, Any]:
        return json.loads(self._path(job_id).read_text(encoding="utf-8"))

    def events(self, job_id: str, after: int = 0) -> list[dict[str, Any]]:
        return [event for event in self.get(job_id)["events"] if event["seq"] > after]

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            record = self.get(job_id)
            if record["status"] in {"queued", "running"}:
                self._cancel.add(job_id); record["status"] = "cancelling"
                self._event(record, "cancelling"); self._write(record)
            return record
