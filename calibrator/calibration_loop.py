"""Recoverable visual inspection/rework loop."""
from __future__ import annotations
from typing import Any, Callable, Literal
from pydantic import BaseModel, ConfigDict
from agent_core.models import ReferenceImage, VisualCheckResult
from agent_core.workflow import SelfCheckPolicy
from prompt_engine.context_assembler import ContextAssembler, ContextPolicy
from storage.project_store import ProjectStore, content_hash

class ManualAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["execute", "edit_and_execute", "skip", "end", "accept_current"]
    edited_delta: str | None = None

    def effective_delta(self, original: str) -> str:
        if self.action == "edit_and_execute":
            if not self.edited_delta or not self.edited_delta.strip():
                raise ValueError("编辑建议后执行必须提供非空修改建议。")
            return self.edited_delta.strip()
        return original


class CalibrationLoop:
    def __init__(self, store: ProjectStore, policy: SelfCheckPolicy, *, inspector: Callable[[str, str], dict[str, Any]], reworker: Callable[[dict[str, Any]], dict[str, Any]], presenter: Callable[[int, VisualCheckResult], None] | None = None) -> None:
        self.store, self.policy, self.inspector, self.reworker, self.presenter = store, policy, inspector, reworker, presenter or (lambda _n, _r: None)

    def run(self, *, current_asset: dict[str, Any], stable_specification: str, constraints: list[str], approve: Callable[[VisualCheckResult], ManualAction] | None = None, start_round: int = 1) -> dict[str, Any]:
        current = current_asset
        limit = self.policy.fixed_rounds if self.policy.termination == "fix" else self.policy.max_rounds
        for number in range(start_round, limit + 1):
            self.store.events.append("inspection_started", round=number, asset=current)
            inspection_key = self.store.idempotency_key("inspection", content_hash(current), content_hash(stable_specification), "vlm")
            cached = self._successful(inspection_key)
            raw = cached or self.inspector(str(current["uri"]), stable_specification)
            result = VisualCheckResult.model_validate(raw)
            self.store.events.append("inspection_reused" if cached else "inspection_completed", round=number, result=result.model_dump(mode="json"), idempotency_key=inspection_key)
            self.presenter(number, result)
            checked_hash = str(current["sha256"])
            choice = ManualAction(action="execute")
            # A blocked inspection can never flow through automatically. Solo
            # mode explicitly requires a human disposition; manual policies do
            # so for every inspection.
            if self.policy.needs_human_release() or result.decision == "blocked":
                self.store.events.append("waiting_human_approval", round=number)
                self.store.checkpoint("self_check_iteration", {"phase": "waiting_human_approval", "round": number, "asset": current, "inspection": result.model_dump(mode="json")})
                if approve is None:
                    return {"waiting": True, "phase": "waiting_human_approval", "round": number, "asset": current,
                            "inspection": result.model_dump(mode="json"), "calibration_status": "waiting_human_decision",
                            "termination_satisfied": False, "termination_reason": "inspection_blocked" if result.decision == "blocked" else "manual_release_required",
                            "latest_checked_asset_hash": checked_hash, "selected_policy": self.policy.__dict__}
                choice = approve(result)
            if choice.action == "end":
                self.store.events.append("calibration_terminated_without_delivery", round=number, asset_hash=checked_hash, decision=result.decision)
                return {"waiting": True, "phase": "terminated_without_delivery", "round": number, "asset": current,
                        "inspection": result.model_dump(mode="json"), "calibration_status": "terminated_without_delivery",
                        "termination_satisfied": False, "termination_reason": "human_ended_without_delivery",
                        "latest_checked_asset_hash": checked_hash, "selected_policy": self.policy.__dict__}
            if choice.action == "accept_current":
                self.store.events.append("calibration_current_asset_accepted", round=number, asset_hash=checked_hash,
                                         decision=result.decision, policy=self.policy.__dict__)
                return {"waiting": False, "phase": "calibration_completed", "round": number, "asset": current,
                        "inspection": result.model_dump(mode="json"), "calibration_status": "human_accepted",
                        "termination_satisfied": True, "termination_reason": "human_accepted_current_asset",
                        "latest_checked_asset_hash": checked_hash, "selected_policy": self.policy.__dict__}
            if result.decision == "blocked":
                return {"waiting": True, "phase": "waiting_human_approval", "round": number, "asset": current,
                        "inspection": result.model_dump(mode="json"), "calibration_status": "waiting_human_decision",
                        "termination_satisfied": False, "termination_reason": "inspection_blocked",
                        "latest_checked_asset_hash": checked_hash, "selected_policy": self.policy.__dict__}
            if result.decision == "pass":
                # A passing asset is already the asset that was just inspected.
                # Fixed-round mode may perform the remaining inspections, but
                # must never mutate that asset merely to consume the budget.
                if self.policy.termination == "solo" or self.policy.stop_early_on_pass or number >= limit:
                    return {"waiting": False, "phase": "calibration_completed", "round": number, "asset": current,
                            "inspection": result.model_dump(mode="json"), "calibration_status": "completed",
                            "termination_satisfied": True, "termination_reason": "pass",
                            "latest_checked_asset_hash": checked_hash, "selected_policy": self.policy.__dict__}
                self.store.checkpoint("self_check_iteration", {"phase": "round_checkpointed", "round": number, "asset": current})
                self.store.events.append("round_checkpointed", round=number, asset=current)
                continue
            if number >= limit:
                # One round is one actual VLM inspection. There is no budget to
                # inspect a reworked asset after the final inspection, so retain
                # the checked asset and wait for an explicit, auditable human
                # disposition (accept_current or end).
                reason = "fixed_round_limit" if self.policy.termination == "fix" else "solo_round_limit"
                self.store.events.append("calibration_round_limit_reached", round=number, asset_hash=checked_hash,
                                         decision=result.decision, policy=self.policy.__dict__)
                self.store.checkpoint("self_check_iteration", {"phase": "waiting_human_approval", "round": number,
                    "asset": current, "inspection": result.model_dump(mode="json"),
                    "latest_checked_asset_hash": checked_hash, "termination_reason": reason})
                return {"waiting": True, "phase": "waiting_human_approval", "round": number,
                        "reason": "已达到质检轮次上限，请人工决定。", "asset": current,
                        "inspection": result.model_dump(mode="json"), "calibration_status": "waiting_human_decision",
                        "termination_satisfied": False, "termination_reason": reason,
                        "latest_checked_asset_hash": checked_hash, "selected_policy": self.policy.__dict__}
            if choice.action != "skip":
                self.store.events.append("rework_started", round=number)
                refs = [ReferenceImage(uri=str(current["uri"]), role="current", source=str(current.get("artifact_id", "current")), sha256=str(current["sha256"]), order=0, reason="当前最新画面必须作为首张参考")]
                delta = choice.effective_delta(result.rework_prompt_delta)
                assembled = ContextAssembler(ContextPolicy("image")).assemble(objective="按本轮质检意见修正画面", specification=stable_specification, constraints=constraints, current_input=str(current), feedback=delta, references=refs)
                key = self.store.idempotency_key("self_check_rework", content_hash(current), content_hash(assembled["text"]), "image", current["sha256"])
                current = self._successful(key) or self.reworker(assembled)
                self.store.events.append("rework_completed", round=number, asset=current, references=assembled["references"], idempotency_key=key)
            self.store.checkpoint("self_check_iteration", {"phase": "round_checkpointed", "round": number, "asset": current})
            self.store.events.append("round_checkpointed", round=number, asset=current)
        raise RuntimeError("质检循环意外退出，未形成可审计的终止事实。")

    def _successful(self, key: str) -> Any | None:
        for event in reversed(self.store.events.read_all()):
            if event.get("idempotency_key") == key and event.get("type") in {"inspection_completed", "rework_completed"}:
                return event.get("result") or event.get("asset")
        return None
