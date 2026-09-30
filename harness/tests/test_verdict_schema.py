"""What verdict.schema.json accepts: one outcome from the fixed list, metrics as
named numbers, a row per test, and a system error that grades nobody. The
verdicts the harness builds are checked against it in test_run.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

REPO_ROOT = Path(__file__).parents[2]

_SCHEMA = json.loads(
    (REPO_ROOT / "schemas" / "verdict.schema.json").read_text(encoding="utf-8")
)
_VALIDATOR = Draft202012Validator(_SCHEMA, format_checker=FormatChecker())

OUTCOMES = [
    "accepted",
    "partial",
    "wrong_answer",
    "time_limit",
    "memory_limit",
    "output_limit",
    "runtime_error",
    "compile_error",
    "skipped",
    "system_error",
]


@pytest.fixture
def graded() -> dict[str, Any]:
    """The published example: a plan that ran to the end."""
    document: dict[str, Any] = json.loads(
        (REPO_ROOT / "examples" / "verdict.json").read_text(encoding="utf-8")
    )
    return document


@pytest.fixture
def broken(graded: dict[str, Any]) -> dict[str, Any]:
    """The other shape: the harness could not grade, so nothing about the
    submission is claimed.
    """
    return graded | {
        "outcome": "system_error",
        "metrics": {},
        "tests": [],
        "summary": "the primitive image for step run could not be started",
    }


def _refusals(document: dict[str, Any]) -> list[str]:
    return [error.message for error in _VALIDATOR.iter_errors(document)]


def test_the_published_example_is_accepted(graded: dict[str, Any]) -> None:
    assert _refusals(graded) == []


def test_a_system_error_that_grades_nobody_is_accepted(broken: dict[str, Any]) -> None:
    assert _refusals(broken) == []


def test_the_outcome_list_is_fixed(graded: dict[str, Any]) -> None:
    for outcome in OUTCOMES:
        if outcome != "system_error":
            assert _refusals(graded | {"outcome": outcome}) == [], outcome
    assert _refusals(graded | {"outcome": "AC"}) != []
    assert _refusals(graded | {"outcome": "verdict"}) != []


def test_a_system_error_carrying_a_grade_is_refused(broken: dict[str, Any]) -> None:
    """The failure this rule exists for: a harness that crashed and still filed
    a wrong answer, or points, against the contestant.
    """
    assert _refusals(broken | {"tests": [_row("01", "wrong_answer")]}) != []
    assert _refusals(broken | {"metrics": {"points": 0}}) != []
    assert _refusals(broken | {"summary": ""}) != []


def test_there_is_no_separate_score(graded: dict[str, Any]) -> None:
    assert _refusals(graded | {"score": "100"}) != []
    assert _refusals(graded | {"verdict": "AC"}) != []


def test_metrics_are_named_numbers(graded: dict[str, Any]) -> None:
    assert _refusals(graded | {"metrics": {"points": 12.5, "accuracy": 0.9}}) == []
    assert _refusals(graded | {"metrics": {}}) == []
    assert _refusals(graded | {"metrics": {"points": "lots"}}) != []
    assert _refusals(graded | {"metrics": None}) != []
    assert _refusals(graded | {"metrics": {"Points": 1}}) != []


def test_every_test_row_carries_its_own_outcome_time_memory_and_metrics(
    graded: dict[str, Any],
) -> None:
    assert _refusals(graded | {"tests": [_row("01", "time_limit")]}) == []
    assert _refusals(graded | {"tests": []}) == []
    for missing in ("outcome", "time_ms", "memory_kb", "metrics"):
        row = _row("01", "accepted")
        del row[missing]
        assert _refusals(graded | {"tests": [row]}) != [], missing
    assert _refusals(graded | {"tests": [_row("01", "AC")]}) != []


def test_an_unmeasured_time_is_null_not_absent(graded: dict[str, Any]) -> None:
    row = _row("01", "accepted") | {"time_ms": None, "memory_kb": None}
    assert _refusals(graded | {"tests": [row]}) == []


def test_the_log_pointer_is_a_url_or_null(graded: dict[str, Any]) -> None:
    assert _refusals(graded | {"log": None}) == []
    assert _refusals(graded | {"log": "not a url"}) != []


def test_a_verdict_names_its_task_and_publication(graded: dict[str, Any]) -> None:
    """Without them the submission's tag names nothing, and the file stops
    being readable on its own after a loss of the database.
    """
    for field in ("task", "publication", "submission", "grading_id"):
        without = dict(graded)
        del without[field]
        assert _refusals(without) != [], field
    assert _refusals(graded | {"publication": {"tag": "v3", "commit": "a" * 40}}) != []


def _row(test_id: str, outcome: str) -> dict[str, Any]:
    return {
        "id": test_id,
        "outcome": outcome,
        "time_ms": 10,
        "memory_kb": 1024,
        "metrics": {"points": 1},
    }
