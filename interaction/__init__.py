"""Human interaction contracts and Chinese presentation helpers."""

from interaction.answer_collector import collect_answers
from interaction.approval_gate import approve_confirmation_doc, assert_approved
from interaction.confirmation_builder import build_confirmation_doc
from interaction.question_generator import generate_question_card

__all__ = [
    "approve_confirmation_doc",
    "assert_approved",
    "build_confirmation_doc",
    "collect_answers",
    "generate_question_card",
]
