"""Five-candidate generation with partial success and idempotent retries."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from storage.project_store import ProjectStore, content_hash


class CandidateBatchError(RuntimeError):
    """Aggregate candidate failure without discarding provider recovery metadata."""

    def __init__(self, failures: list[dict[str, Any]]) -> None:
        if not failures:
            raise ValueError("候选图批次异常必须包含失败明细。")
        # Any permanent failure blocks an automatic retry. Otherwise the first
        # failure carries the provider's original unknown-outcome category.
        representative = next((item for item in failures if not item.get("retryable")), failures[0])
        self.failures = failures
        self.category = str(representative.get("category") or "invalid_input")
        self.retryable = all(bool(item.get("retryable")) for item in failures)
        if self.retryable:
            message = f"候选图有 {len(failures)} 项暂时生成失败；已批准的技能版本和成功项均已保存，可从上一成功点重试。"
        else:
            message = f"候选图有 {len(failures)} 项生成失败且不可重试：{representative.get('error') or '请求被拒绝'}"
        super().__init__(message)

class CandidateBatchGenerator:
    def __init__(self, store: ProjectStore, render: Callable[[int], dict[str, Any]], *, attempts: int = 2, max_workers: int = 5) -> None:
        self.store, self.render, self.attempts = store, render, attempts
        self.max_workers = max(1, min(5, max_workers))

    @staticmethod
    def _idempotency_key(input_hash: str, index: int, cache_scope: dict[str, Any] | None) -> str:
        parts: list[Any] = ["initial_candidate_generation", input_hash]
        # Keep source compatibility for older callers while allowing production
        # generation to isolate approved skill/render-plan versions.
        if cache_scope is not None:
            parts.append(cache_scope)
        parts.append(index)
        return content_hash(parts)

    @staticmethod
    def _matches_expected(asset: Any, expected: dict[str, Any] | None) -> bool:
        if not isinstance(asset, dict):
            return False
        if expected is None:
            return True
        return all(asset.get(field) == expected.get(field)
                   for field in ("style_id", "prompt_version_id", "provenance"))

    def generate(self, input_hash: str, *, cache_scope: dict[str, Any] | None = None,
                 expected_assets: list[dict[str, Any]] | None = None) -> dict[str, list[Any]]:
        # 候选数量由渲染方案（expected_assets）决定：风格库模式固定 5 个风格方案，
        # 「不使用数据库」模式为 candidate_concurrency 个自由方案。
        count = len(expected_assets) if expected_assets is not None else 5
        if count < 1:
            raise ValueError("候选缓存校验必须提供至少一个方案的 provenance。")
        successes: list[Any] = []; failures: list[Any] = []
        with self.store.lock():
            events = self.store.events.read_all()
            pending: list[tuple[int, str]] = []
            for index in range(count):
                key = self._idempotency_key(input_hash, index, cache_scope)
                expected = expected_assets[index] if expected_assets is not None else None
                candidates = [e.get("asset") for e in reversed(events)
                              if e.get("type") == "candidate_succeeded" and e.get("idempotency_key") == key]
                cached = next((asset for asset in candidates if self._matches_expected(asset, expected)), None)
                if candidates and any(not self._matches_expected(asset, expected) for asset in candidates):
                    self.store.events.append(
                        "candidate_cache_rejected", index=index, idempotency_key=key,
                        reason="provenance_mismatch", expected=expected,
                    )
                if cached: successes.append(cached); continue
                pending.append((index, key))

            def one(index: int, key: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
                error: Exception | None = None
                for attempt in range(1, self.attempts + 1):
                    try:
                        asset = self.render(index)
                        self.store.events.append("candidate_succeeded", index=index, attempt=attempt, asset=asset,
                                                 idempotency_key=key, cache_scope=cache_scope)
                        return asset, None
                    except Exception as exc:
                        error = exc
                        detail = {"code": type(exc).__name__, "message": str(exc),
                                  "category": getattr(exc, "category", "invalid_input"),
                                  "retryable": bool(getattr(exc, "retryable", False))}
                        self.store.events.append("candidate_failed", index=index, attempt=attempt, error=detail, idempotency_key=key)
                return None, {"index": index, "error": str(error), "code": type(error).__name__,
                              "category": getattr(error, "category", "invalid_input"),
                              "retryable": bool(getattr(error, "retryable", False)), "idempotency_key": key}

            with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="candidate") as pool:
                futures = {pool.submit(one, index, key): index for index, key in pending}
                for future in as_completed(futures):
                    asset, failure = future.result()
                    if asset is not None: successes.append(asset)
                    if failure is not None: failures.append(failure)
            successes.sort(key=lambda item: int(item.get("candidate_index", item.get("uri", 0))) if str(item.get("candidate_index", item.get("uri", 0))).isdigit() else 0)
            failures.sort(key=lambda item: item["index"])
        return {"succeeded": successes, "failed": failures}
