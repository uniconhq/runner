"""What verdict.schema.json accepts, branch by branch. Nothing writes a verdict
until Task 6, so the schema is the whole contract today.
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


@pytest.fixture
def graded() -> dict[str, Any]:
    """The published example: a plan that ran to the end."""
    document: dict[str, Any] = json.loads(
        (REPO_ROOT / "examples" / "verdict.json").read_text(encoding="utf-8")
    )
    return document


@pytest.fixture
def failed(graded: dict[str, Any]) -> dict[str, Any]:
    """The other branch: nobody graded, so there is a sentence and no result."""
    return graded | {
        "outcome": "contestant_error",
        "verdict": None,
        "score": None,
        "summary": None,
        "error_message": "the submission did not compile",
    }


def _refusals(document: dict[str, Any]) -> list[str]:
    return [error.message for error in _VALIDATOR.iter_errors(document)]


def test_the_published_example_is_accepted(graded: dict[str, Any]) -> None:
    assert _refusals(graded) == []


def test_a_contestant_error_with_a_message_is_accepted(failed: dict[str, Any]) -> None:
    assert _refusals(failed) == []


def test_a_system_error_with_a_message_is_accepted(failed: dict[str, Any]) -> None:
    failed["outcome"] = "system_error"
    failed["error_message"] = "the plan bundle could not be fetched"

    assert _refusals(failed) == []


def test_a_system_error_carrying_a_verdict_is_refused(failed: dict[str, Any]) -> None:
    """The failure this branch exists for: a harness that crashed and still filed
    a WA against the contestant.
    """
    failed["outcome"] = "system_error"
    failed["verdict"] = "WA"

    assert _refusals(failed) != []


def test_a_verdict_with_nothing_in_it_is_refused(graded: dict[str, Any]) -> None:
    graded["verdict"] = None
    graded["score"] = None
    graded["summary"] = None

    assert _refusals(graded) != []


def test_a_contestant_error_with_no_message_is_refused(failed: dict[str, Any]) -> None:
    failed["error_message"] = None

    assert _refusals(failed) != []


def test_a_contestant_error_with_an_empty_message_is_refused(
    failed: dict[str, Any],
) -> None:
    failed["error_message"] = ""

    assert _refusals(failed) != []


def test_a_graded_verdict_that_also_reports_an_error_is_refused(
    graded: dict[str, Any],
) -> None:
    graded["error_message"] = "storage was unreachable"

    assert _refusals(graded) != []


@pytest.mark.parametrize("score", ["0", "-3", "12.5", "100.00"])
def test_a_decimal_score_is_accepted(graded: dict[str, Any], score: str) -> None:
    graded["score"] = score

    assert _refusals(graded) == []


@pytest.mark.parametrize("score", ["", "abc", "1.2.3", "+4", "1e3", " 7", "1,000"])
def test_anything_that_is_not_a_decimal_number_is_refused(
    graded: dict[str, Any], score: str
) -> None:
    """The backend parses this text into the column the leaderboard sorts on. A
    value it cannot parse arrives after the grading, when nobody can fix it.
    """
    graded["score"] = score

    assert _refusals(graded) != []


def test_a_verdict_with_no_task_named_is_refused(graded: dict[str, Any]) -> None:
    """Without the task repo the published tag names nothing, and the file stops
    being readable on its own.
    """
    del graded["task"]

    assert _refusals(graded) != []


@pytest.mark.parametrize("field", ["verdict", "task_published_tag"])
def test_an_empty_string_is_refused_where_a_name_is_required(
    graded: dict[str, Any], field: str
) -> None:
    graded[field] = ""

    assert _refusals(graded) != []
