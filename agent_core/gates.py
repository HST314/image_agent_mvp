"""Workflow preconditions."""
from agent_core.errors import GateBlockedError
from agent_core.models import SignStatus, TaskConfirmationDoc

RENDER_STATES = frozenset({"initial_candidate_generation", "master_candidate_selection", "self_check_iteration", "human_prompt_iteration", "final_approval"})

def require_approved_confirmation(doc: TaskConfirmationDoc | None, target_state: str) -> None:
    if target_state in RENDER_STATES and (doc is None or doc.sign_status is not SignStatus.APPROVED):
        raise GateBlockedError("请先确认创作任务书，再继续生成图片。")
