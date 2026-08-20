"""Small, auditable in-process job executor used by the HTTP adapter."""
from __future__ import annotations

import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobNotFoundError(FileNotFoundError):
    """The requested persisted job record does not exist."""


class JobRegistry:
    """Persist job facts and mark, but never replay, work interrupted by restart."""

    def __init__(self, root: Path, workers: int = 5, *,
                 queue_sla_seconds: float = 300,
                 heartbeat_interval_seconds: float = 5,
                 on_recover: Callable[[dict[str, Any]], None] | None = None) -> None:
        if workers < 1 or queue_sla_seconds <= 0 or heartbeat_interval_seconds <= 0:
            raise ValueError("JOB_EXECUTOR_CONFIG_INVALID")
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._workers = workers
        self._queue_sla_seconds = queue_sla_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="image-job")
        self._lock = threading.RLock()
        self._cancel: set[str] = set()
        self._callbacks: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._interrupt_orphans(on_recover)

    def _path(self, job_id: str) -> Path:
        if not job_id.startswith("job_") or not job_id[4:].isalnum():
            raise ValueError("JOB_ID_INVALID")
        return self.root / f"{job_id}.json"

    def is_ready(self) -> bool:
        """Return whether the in-process executor can accept work."""
        return not self._pool._shutdown

    @staticmethod
    def _age_seconds(timestamp: object, *, now: datetime | None = None) -> float:
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, ((now or datetime.now(timezone.utc)) - parsed).total_seconds())
        except (TypeError, ValueError):
            return 0.0

    def _read(self, job_id: str) -> dict[str, Any]:
        try:
            return json.loads(self._path(job_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise JobNotFoundError(f"任务不存在：{job_id}") from exc

    def _notify(self, record: dict[str, Any]) -> None:
        callback = self._callbacks.get(record["job_id"])
        if callback:
            try:
                callback(record)
            except Exception:
                # The job record is authoritative.  A secondary project
                # timeline write must not strand otherwise executable work.
                LOGGER.exception("job lifecycle callback failed job_id=%s", record["job_id"])

    def _expire_stalled(self, record: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        """Turn work that never started within the queue SLA into a safe terminal state."""
        if record.get("status") != "queued":
            return record
        submitted_at = record.get("submitted_at") or record.get("created_at")
        if self._age_seconds(submitted_at, now=now) < self._queue_sla_seconds:
            return record
        record.update(
            status="stalled",
            finished_at=_now(),
            error={
                "code": "QUEUE_START_TIMEOUT",
                "message": "任务排队超时且尚未开始，可安全重试。",
                "retry_safe": True,
            },
        )
        self._event(record, "stalled", error=record["error"])
        self._write(record)
        self._notify(record)
        self._callbacks.pop(record["job_id"], None)
        return record

    def _write(self, record: dict[str, Any]) -> None:
        # The projects directory is sometimes replaced while the web process is
        # kept alive (for example when restoring/exporting a project).  The
        # registry object survives that replacement, so do not assume its
        # persistence directory still exists.
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(record["job_id"])
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def _interrupt_orphans(self, on_recover: Callable[[dict[str, Any]], None] | None) -> None:
        for path in self.root.glob("job_*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if item.get("status") in {"queued", "running", "cancelling"}:
                    item.update(status="interrupted", finished_at=_now(), error={"code":"PROCESS_RESTARTED","message":"服务重启，在途任务未恢复且不会自动补调用。"})
                    self._event(item, "interrupted", error=item["error"])
                    self._write(item)
                    if on_recover:
                        on_recover(item)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue

    def submit(self, project_id: str, idempotency_key: str, operation: str,
               execute: Callable[[], Any], *, on_event: Callable[[dict[str, Any]], None] | None = None) -> tuple[dict[str, Any], bool]:
        with self._lock:
            for path in self.root.glob("job_*.json"):
                item = json.loads(path.read_text(encoding="utf-8"))
                item = self._expire_stalled(item)
                # A project is protected by one workflow lock.  Returning the
                # existing in-flight job prevents a second click (or another
                # tab) from creating a doomed job that can only fail on that
                # lock, while giving the caller the record it should track.
                if item.get("project_id") == project_id and item.get("status") in {"queued", "running", "cancelling"}:
                    return item, False
                if item.get("project_id") == project_id and item.get("idempotency_key") == idempotency_key:
                    return item, False
            job_id = "job_" + uuid.uuid4().hex
            submitted_at = _now()
            record = {"job_id":job_id, "project_id":project_id, "operation":operation,
                      "idempotency_key":idempotency_key, "status":"queued",
                      "created_at":submitted_at, "submitted_at":submitted_at,
                      "events":[{"seq":1,"type":"queued","timestamp":submitted_at}]}
            if on_event:
                self._callbacks[job_id] = on_event
            self._write(record)
            self._notify(record)
            self._pool.submit(self._run, job_id, execute)
            return record, True

    def active_for_project(self, project_id: str) -> dict[str, Any] | None:
        """Return the newest durable in-flight job for a project, if any."""
        with self._lock:
            active: list[dict[str, Any]] = []
            for path in self.root.glob("job_*.json"):
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                item = self._expire_stalled(item)
                if item.get("project_id") == project_id and item.get("status") in {"queued", "running", "cancelling"}:
                    active.append(item)
            return max(active, key=lambda item: str(item.get("created_at", "")), default=None)

    def _event(self, record: dict[str, Any], kind: str, **payload: Any) -> None:
        record["events"].append({"seq":len(record["events"])+1,"type":kind,"timestamp":_now(),**payload})

    def _heartbeat(self, job_id: str, stop: threading.Event) -> None:
        while not stop.wait(self._heartbeat_interval_seconds):
            with self._lock:
                try:
                    record = self._read(job_id)
                except JobNotFoundError:
                    return
                if record.get("status") not in {"running", "cancelling"}:
                    return
                record["heartbeat_at"] = _now()
                self._write(record)

    def _run(self, job_id: str, execute: Callable[[], Any]) -> None:
        with self._lock:
            # The worker is the final authority at the paid-execution boundary.
            # Observers may never poll this job, so expire it here while holding
            # the same lock used for the queued -> running transition.
            record = self._expire_stalled(self._read(job_id))
            if record.get("status") != "queued":
                self._callbacks.pop(job_id, None)
                return
            started_at = _now()
            record.update(status="running", started_at=started_at, heartbeat_at=started_at)
            self._event(record, "running"); self._write(record)
            self._notify(record)
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(job_id, heartbeat_stop),
            name=f"image-heartbeat-{job_id[4:12]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            if job_id in self._cancel:
                raise InterruptedError("任务在开始前已取消。")
            result = execute()
            with self._lock:
                record = self._read(job_id)
                # Once execute() has started it may already have committed durable
                # workflow state or incurred an external charge.  A late cancel
                # request therefore cannot truthfully turn a completed operation
                # into "cancelled" or discard its result.  Cancellation remains
                # effective at the pre-execution check above; after that point the
                # actual execution outcome is authoritative.
                cancellation_requested = job_id in self._cancel
                record.update(status="succeeded", finished_at=_now(), result=result)
                if cancellation_requested:
                    record["cancellation_requested"] = True
                self._event(record, "succeeded", cancellation_requested=cancellation_requested)
                self._cancel.discard(job_id)
                self._write(record)
                self._notify(record)
                self._callbacks.pop(job_id, None)
        except InterruptedError:
            with self._lock:
                record = self._read(job_id); record.update(status="cancelled", finished_at=_now())
                self._event(record, "cancelled"); self._write(record)
                self._cancel.discard(job_id)
                self._notify(record)
        except Exception as exc:
            with self._lock:
                code = getattr(exc, "code", exc.__class__.__name__)
                error: dict[str, Any] = {"code":code,"message":str(exc)}
                # 规范化失败分类（如 ModelCallError.category 的 timeout_unknown 等）
                # 随 job 记录暴露：调用方依 *_unknown 约定区分「结果未知、可能已扣费」
                # 与「已知失败」，决定幂等重试去重策略（对齐 gateway 的 possible_charge 语义）。
                category = getattr(exc, "category", None)
                if isinstance(category, str) and category:
                    error["category"] = category
                record = self._read(job_id); record.update(status="failed", finished_at=_now(), error=error)
                self._event(record, "failed", error=record["error"]); self._write(record)
                self._cancel.discard(job_id)
                self._notify(record)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)
            with self._lock:
                self._callbacks.pop(job_id, None)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self._expire_stalled(self._read(job_id))

    def events(self, job_id: str, after: int = 0) -> list[dict[str, Any]]:
        return [event for event in self.get(job_id)["events"] if event["seq"] > after]

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            record = self.get(job_id)
            if record["status"] == "queued":
                record.update(status="cancelled", finished_at=_now())
                self._event(record, "cancelled")
                self._write(record)
                self._notify(record)
                self._callbacks.pop(job_id, None)
            elif record["status"] == "running":
                self._cancel.add(job_id); record["status"] = "cancelling"
                self._event(record, "cancelling"); self._write(record)
            return record

    def metrics(self) -> dict[str, int | float | None]:
        """Return safe executor saturation metrics for readiness diagnostics."""
        with self._lock:
            now = datetime.now(timezone.utc)
            records: list[dict[str, Any]] = []
            for path in self.root.glob("job_*.json"):
                try:
                    records.append(self._expire_stalled(json.loads(path.read_text(encoding="utf-8")), now=now))
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    continue
            queued = [item for item in records if item.get("status") == "queued"]
            running = [item for item in records if item.get("status") in {"running", "cancelling"}]
            queue_ages = [self._age_seconds(item.get("submitted_at") or item.get("created_at"), now=now)
                          for item in queued]
            heartbeat_ages = [self._age_seconds(item.get("heartbeat_at") or item.get("started_at"), now=now)
                              for item in running]
            return {
                "max_workers": self._workers,
                "active_workers": len(running),
                "queue_depth": len(queued),
                "oldest_queued_seconds": round(max(queue_ages), 3) if queue_ages else None,
                "oldest_heartbeat_seconds": round(max(heartbeat_ages), 3) if heartbeat_ages else None,
            }
