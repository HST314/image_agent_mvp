"""Five-candidate generation with partial success and idempotent retries."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from storage.project_store import ProjectStore, content_hash

class CandidateBatchGenerator:
    def __init__(self, store: ProjectStore, render: Callable[[int], dict[str, Any]], *, attempts: int = 2, max_workers: int = 5) -> None:
        self.store, self.render, self.attempts = store, render, attempts
        self.max_workers = max(1, min(5, max_workers))

    def generate(self, input_hash: str) -> dict[str, list[Any]]:
        successes: list[Any] = []; failures: list[Any] = []
        with self.store.lock():
            events = self.store.events.read_all()
            pending: list[tuple[int, str]] = []
            for index in range(5):
                key = content_hash(["initial_candidate_generation", input_hash, index])
                cached = next((e.get("asset") for e in reversed(events) if e.get("type") == "candidate_succeeded" and e.get("idempotency_key") == key), None)
                if cached: successes.append(cached); continue
                pending.append((index, key))

            def one(index: int, key: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
                error: Exception | None = None
                for attempt in range(1, self.attempts + 1):
                    try:
                        asset = self.render(index)
                        self.store.events.append("candidate_succeeded", index=index, attempt=attempt, asset=asset, idempotency_key=key)
                        return asset, None
                    except Exception as exc:
                        error = exc; self.store.events.append("candidate_failed", index=index, attempt=attempt, error={"code": type(exc).__name__, "message": str(exc)}, idempotency_key=key)
                return None, {"index": index, "error": str(error), "idempotency_key": key}

            with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="candidate") as pool:
                futures = {pool.submit(one, index, key): index for index, key in pending}
                for future in as_completed(futures):
                    asset, failure = future.result()
                    if asset is not None: successes.append(asset)
                    if failure is not None: failures.append(failure)
            successes.sort(key=lambda item: int(item.get("candidate_index", item.get("uri", 0))) if str(item.get("candidate_index", item.get("uri", 0))).isdigit() else 0)
            failures.sort(key=lambda item: item["index"])
        return {"succeeded": successes, "failed": failures}
