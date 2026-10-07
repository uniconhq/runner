"""What result.schema.json accepts: what stopped the run and which step that
runs once stopped it, a row per test with its outcome and values, the once
values, and an error exactly when a fault ended the run. The results the
harness builds are checked against it in test_run.py; here are its rules,
and the harness's own builder for the shapes a run can take.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from tests.support import classic_plan
from unicon_harness import exact
from unicon_harness import result as results
from unicon_harness.grading import Grading
from unicon_harness.plan import parse

REPO_ROOT = Path(__file__).parents[2]

_SCHEMA = json.loads(
    (REPO_ROOT / "schemas" / "result.schema.json").read_text(encoding="utf-8")
)
_VALIDATOR = Draft202012Validator(_SCHEMA, format_checker=FormatChecker())

OUTCOMES = [
    "accepted",
    "wrong_answer",
    "time_limit",
    "memory_limit",
    "output_limit",
    "runtime_error",
    "compile_error",
    "skipped",
]
TESTS = ["main/1", "main/2"]


@pytest.fixture
def graded() -> dict[str, Any]:
    """The published example: a plan that ran to the end."""
    document: dict[str, Any] = exact.loads(
        (REPO_ROOT / "examples" / "result.json").read_bytes()
    )
    return document


@pytest.fixture
def broken(graded: dict[str, Any]) -> dict[str, Any]:
    """A fault ended the run before any test got through."""
    return graded | {
        "stopped": "system_error",
        "tests": [_row("main/1", "skipped", {})],
        "values": {},
        "error": "the primitive image for step run could not be started",
    }


def _refusals(document: dict[str, Any]) -> list[str]:
    return [error.message for error in _VALIDATOR.iter_errors(document)]


def _row(test: str, outcome: str, values: dict[str, Any] | None = None) -> Any:
    return {"test": test, "outcome": outcome, "values": values or {}}


def test_the_published_example_is_accepted(graded: dict[str, Any]) -> None:
    assert _refusals(graded) == []


def test_a_system_error_with_an_error_is_accepted(broken: dict[str, Any]) -> None:
    assert _refusals(broken) == []
    assert _refusals(broken | {"tests": []}) == []


def test_an_error_comes_exactly_with_a_system_error(
    graded: dict[str, Any], broken: dict[str, Any]
) -> None:
    assert _refusals(broken | {"error": None}) != []
    assert _refusals(broken | {"error": ""}) != []
    assert _refusals(graded | {"error": "something"}) != []
    stopped = graded | {"stopped": "compile_error", "stopped_by": "compile"}
    assert _refusals(stopped) == []
    assert _refusals(stopped | {"error": "the compile failed"}) != []


def test_a_run_is_stopped_by_any_outcome_but_accepted_and_skipped(
    graded: dict[str, Any],
) -> None:
    for outcome in OUTCOMES:
        refused = _refusals(graded | {"stopped": outcome, "stopped_by": "compile"})
        assert (refused != []) == (outcome in ("accepted", "skipped")), outcome


def test_the_step_that_stopped_the_run_is_named_exactly_when_one_did(
    graded: dict[str, Any], broken: dict[str, Any]
) -> None:
    stopped = graded | {"stopped": "compile_error", "stopped_by": "compile"}
    assert _refusals(stopped | {"stopped_by": None}) != []
    assert _refusals(stopped | {"stopped_by": "Compile step"}) != []
    assert _refusals(graded | {"stopped_by": "compile"}) != []
    assert _refusals(broken | {"stopped_by": "compile"}) != []
    without = dict(graded)
    del without["stopped_by"]
    assert _refusals(without) != []


def test_a_row_takes_any_outcome_but_system_error(graded: dict[str, Any]) -> None:
    for outcome in OUTCOMES:
        assert _refusals(graded | {"tests": [_row("main/1", outcome)]}) == []
    assert _refusals(graded | {"tests": [_row("main/1", "system_error")]}) != []
    assert _refusals(graded | {"tests": [_row("main/1", "partial")]}) != []
    assert _refusals(graded | {"tests": [_row("main/1", "AC")]}) != []


def test_a_row_is_keyed_by_its_test(graded: dict[str, Any]) -> None:
    row = {"id": "main/1", "outcome": "accepted", "values": {}}
    assert _refusals(graded | {"tests": [row]}) != []
    assert _refusals(graded | {"tests": [_row("1", "accepted")]}) != []


def test_there_is_no_run_outcome_summary_or_metrics(graded: dict[str, Any]) -> None:
    for field in ("outcome", "summary", "metrics", "log", "score"):
        assert _refusals(graded | {field: "x"}) != [], field


def test_values_are_named_numbers_and_texts(graded: dict[str, Any]) -> None:
    values = {"points": 12.5, "fraction": Decimal("0.1"), "note": "fine"}
    assert _refusals(graded | {"values": values}) == []
    assert _refusals(graded | {"values": {"ok": True}}) != []
    assert _refusals(graded | {"values": {"Points": 1}}) != []
    assert _refusals(graded | {"values": {"note": "x" * 10_001}}) != []
    assert _refusals(graded | {"tests": [_row("main/1", "accepted", {"a": [1]})]}) != []


def test_the_log_pointer_is_a_url_or_null(graded: dict[str, Any]) -> None:
    assert _refusals(graded | {"run_log": None}) == []
    assert _refusals(graded | {"run_log": "not a url"}) != []


def test_a_result_with_no_report_is_accepted() -> None:
    plan = classic_plan(TESTS)
    plan["report"] = {}
    built = results.graded(parse(plan), Grading(), [])
    assert results.check(built) is None
    assert built["values"] == {}
    assert [row["values"] for row in built["tests"]] == [{}, {}]


def test_a_result_that_never_read_its_plan_has_no_rows() -> None:
    built = results.system_error("the plan is missing", ["unused"])
    assert results.check(built) is None
    assert built["tests"] == [] and built["stopped"] == "system_error"
    assert built["stopped_by"] is None


def test_a_result_names_the_step_that_stopped_it_but_not_after_a_fault() -> None:
    plan = parse(classic_plan(TESTS))
    grading = Grading(once={"compile": {"outcome": "compile_error"}})
    grading.stopped = "compile_error"
    grading.stopped_by = "compile"

    built = results.graded(plan, grading, [])
    faulted = results.graded(plan, grading, [], error="the log could not be read")

    assert results.check(built) is None and results.check(faulted) is None
    assert (built["stopped"], built["stopped_by"]) == ("compile_error", "compile")
    assert (faulted["stopped"], faulted["stopped_by"]) == ("system_error", None)


def test_a_long_text_is_cut_and_a_secret_in_it_masked() -> None:
    plan = parse(classic_plan(TESTS))
    grading = Grading(once={"compile": {"compile_log": "key=s3cr3t " + "x" * 20_000}})
    grading.stopped = "compile_error"
    grading.stopped_by = "compile"

    built = results.graded(plan, grading, ["s3cr3t"])

    log = built["values"]["log"]
    assert results.check(built) is None
    assert log.startswith("key=*** ")
    assert len(log) <= results.TEXT_LIMIT and log.endswith("(cut short)")


def test_a_secret_holding_another_is_masked_whole() -> None:
    plan = parse(classic_plan(TESTS))
    grading = Grading(once={"compile": {"compile_log": "ab abc"}})

    built = results.graded(plan, grading, ["ab", "abc"], error="ab abc")

    assert built["values"]["log"] == "*** ***"
    assert built["error"] == "*** ***"


def test_a_fault_before_any_test_got_through_skips_them_all() -> None:
    plan = parse(classic_plan(TESTS))
    grading = Grading(per_test={"run": {"main/1": {"outcome": "accepted"}}})

    built = results.graded(plan, grading, [], error="the check broke")

    assert [row["outcome"] for row in built["tests"]] == ["skipped", "skipped"]
    assert built["error"] == "the check broke"
