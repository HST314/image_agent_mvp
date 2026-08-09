"""Build constrained rework prompt deltas for later calibration phases."""

from __future__ import annotations


def build_rework_delta(mismatches: list[str], locked_elements: list[str]) -> str:
    """Create a narrow rework delta from mismatch summaries."""

    mismatch_text = "\n".join(f"- {item}" for item in mismatches) or "- none"
    locked_text = "\n".join(f"- {item}" for item in locked_elements) or "- none"
    return (
        "Apply only the following corrections:\n"
        f"{mismatch_text}\n\n"
        "Keep these elements unchanged:\n"
        f"{locked_text}"
    )
