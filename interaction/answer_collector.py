"""Utilities for collecting or simulating clarification answers."""

from __future__ import annotations

from agent_core.models import QuestionAnswer, QuestionAnswerRecord, QuestionCard, QuestionCardStatus


def collect_answers(question_card: QuestionCard, selections: dict[str, str | None] | None = None) -> QuestionAnswerRecord:
    """Collect answers for a question card.

    Missing selections are treated as explicit skips and remain unconfirmed in
    the downstream confirmation document.
    """

    selections = selections or {}
    answers: list[QuestionAnswer] = []
    for question in question_card.questions:
        selected = selections.get(question.question_id)
        valid_options = {option.option_id for option in question.options}
        if selected is not None and selected not in valid_options:
            raise ValueError(f"Invalid option '{selected}' for question '{question.question_id}'.")
        question.user_selected_option_id = selected
        answers.append(
            QuestionAnswer(
                question_id=question.question_id,
                selected_option_id=selected,
                skipped=selected is None,
            )
        )

    question_card.status = (
        QuestionCardStatus.SKIPPED
        if all(answer.skipped for answer in answers)
        else QuestionCardStatus.ANSWERED
    )
    return QuestionAnswerRecord(
        question_card_id=question_card.question_card_id,
        task_id=question_card.task_id,
        answers=answers,
    )
