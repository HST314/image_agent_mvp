"""Approval helpers for the task confirmation gate."""

from __future__ import annotations

from agent_core.errors import GateBlockedError
from agent_core.models import SignStatus, TaskConfirmationDoc, utc_now


def approve_confirmation_doc(doc: TaskConfirmationDoc, signed_by: str) -> TaskConfirmationDoc:
    """Record explicit human or authorized-controller approval."""

    doc.sign_status = SignStatus.APPROVED
    doc.signed_by = signed_by
    doc.signed_at = utc_now()
    return doc


def assert_approved(doc: TaskConfirmationDoc) -> None:
    """Raise if a confirmation document is not approved."""

    if doc.sign_status is not SignStatus.APPROVED:
        raise GateBlockedError("TaskConfirmationDoc must be approved before downstream generation.")
