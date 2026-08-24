"""Regression coverage for the published ImageTaskCard JSON Schema."""

import json
from pathlib import Path

import pytest
from agent_core.models import ImageTaskCard
from jsonschema import Draft202012Validator
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]


def test_schema_and_model_reject_empty_source_references() -> None:
    card = {
        "task_id": "task_schema",
        "project_id": "project_schema",
        "source_refs": [],
        "deliverable_goal": "Create a launch poster.",
        "usage_context": "Internal review.",
        "status": "draft",
    }
    schema = json.loads(
        (ROOT / "schemas" / "ImageTaskCard.schema.json").read_text(encoding="utf-8")
    )

    schema_errors = list(Draft202012Validator(schema).iter_errors(card))

    assert any(error.validator == "minItems" for error in schema_errors)
    with pytest.raises(ValidationError, match="source_refs must contain at least one"):
        ImageTaskCard.model_validate(card)
