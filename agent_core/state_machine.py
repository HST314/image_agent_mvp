"""Recoverable 5-to-1 workflow coordinator."""
from __future__ import annotations
from typing import Any, Callable
from agent_core.workflow import validate_transition
from storage.project_store import ProjectStore, content_hash

class RecoverableWorkflow:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def advance(self, current: str, target: str, state_data: dict[str, Any], action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        validate_transition(current, target)
        self.store.start_step(target, input_hash=content_hash(state_data))
        try:
            result = action()
            complete = {**state_data, **result, "state": target}
            self.store.checkpoint(target, complete)
            return complete
        except Exception as exc:
            self.store.fail_step(target, {"code": type(exc).__name__, "message": str(exc), "retryable": not isinstance(exc, (ValueError, TypeError))})
            raise

    @staticmethod
    def select_master(candidates: list[dict[str, Any]], selected_id: str) -> dict[str, Any]:
        if len(candidates) != 5:
            raise ValueError("必须先获得 5 个候选方向才能选择主图。")
        matches = [item for item in candidates if item.get("id") == selected_id]
        if len(matches) != 1:
            raise ValueError("请选择 5 张候选图中的 1 张。")
        return matches[0]

    @staticmethod
    def validate_final_asset(asset: dict[str, Any], *, human_approved: bool, self_check_complete: bool) -> None:
        if asset.get("mock") or str(asset.get("uri") or asset.get("url", "")).startswith("mock:"):
            raise ValueError("模拟资产不能作为最终交付。")
        if not self_check_complete:
            raise ValueError("最新图片尚未满足所选质检终止规则。")
        if not human_approved:
            raise ValueError("最终交付必须经过人工确认。")

Phase1StateMachine = RecoverableWorkflow  # source-compatible import; old stages are intentionally unavailable
