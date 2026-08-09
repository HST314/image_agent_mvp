"""Explicit workflow transitions and orthogonal self-check policies."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

TransitionMap = dict[str, frozenset[str]]
TRANSITIONS: TransitionMap = {
    "intake_clarify": frozenset({"confirmation_build"}),
    "confirmation_build": frozenset({"initial_candidate_generation"}),
    "initial_candidate_generation": frozenset({"master_candidate_selection"}),
    "master_candidate_selection": frozenset({"self_check_iteration"}),
    "self_check_iteration": frozenset({"self_check_iteration", "human_prompt_iteration"}),
    "human_prompt_iteration": frozenset({"human_prompt_iteration", "self_check_iteration", "final_approval"}),
    "final_approval": frozenset(),
}

class InvalidTransitionError(ValueError):
    pass

def validate_transition(current: str, target: str) -> None:
    if target not in TRANSITIONS.get(current, frozenset()):
        raise InvalidTransitionError(f"不能从“{current}”直接进入“{target}”。")

@dataclass(frozen=True)
class SelfCheckPolicy:
    termination: Literal["fix", "solo"]
    release: Literal["manual", "auto"]
    fixed_rounds: int = 1
    max_rounds: int = 3
    stop_early_on_pass: bool = False

    def should_stop(self, *, round_number: int, decision: Literal["continue", "pass", "blocked"]) -> bool:
        if decision == "blocked":
            return True
        if self.termination == "fix":
            return (self.stop_early_on_pass and decision == "pass") or round_number >= self.fixed_rounds
        return decision == "pass" or round_number >= self.max_rounds

    def needs_human_release(self) -> bool:
        return self.release == "manual"
