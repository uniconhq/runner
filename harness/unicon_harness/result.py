"""Turning what a plan left into `result.json`, and the result of a run that
could not be graded.

`stopped` is the outcome of the step that runs once and stopped the run,
system_error when a fault ended it, otherwise null; `error` is the fault's
sentence for staff exactly when stopped is system_error. A test's row: its
outcome is the first non-accepted outcome among its steps; otherwise
accepted when the run went on to the end, or when a fault struck after every
per-test step of the test had run for it; otherwise skipped. Its values are
every per-test name of the report whose step ran for the test and wrote the
output, a wrong answer's included, and a skipped test has none. The once
values are every once name of the report whose step wrote the output, kept
when a fault struck later.

A number is written exactly as the primitive wrote it. A text has every
secret's value replaced by *** and is cut at 10,000 characters.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from unicon_harness.contracts import SCHEMA_VERSION, violation
from unicon_harness.grading import ACCEPTED, SKIPPED, Grading, Value
from unicon_harness.plan import Plan

SYSTEM_ERROR = "system_error"
TEXT_LIMIT = 10_000
MASK = "***"


def graded(
    plan: Plan,
    grading: Grading,
    secrets: Iterable[str],
    error: str | None = None,
) -> dict[str, Any]:
    """The result of a run that read its plan: one that ran to the end or was
    stopped by a step, or, with `error`, one a fault ended part way.
    """
    hidden = _hidden(secrets)
    stopped = SYSTEM_ERROR if error is not None else grading.stopped
    reader = _Reader(plan, grading, stopped, hidden)
    return _document(
        stopped=stopped,
        tests=[reader.row(test) for test in plan.tests],
        values=reader.once_values(),
        error=None if error is None else _error(error, hidden),
    )


def system_error(message: str, secrets: Iterable[str]) -> dict[str, Any]:
    """Nobody graded: the plan was never read, so there are no rows, and the
    error says why, for staff.
    """
    return _document(
        stopped=SYSTEM_ERROR,
        tests=[],
        values={},
        error=_error(message, _hidden(secrets)),
    )


def check(result: dict[str, Any]) -> str | None:
    """How the result breaks result.schema.json, or None."""
    return violation(result, "result")


def _document(
    *,
    stopped: str | None,
    tests: list[dict[str, Any]],
    values: dict[str, Any],
    error: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "stopped": stopped,
        "tests": tests,
        "values": values,
        "run_log": None,
        "error": error,
    }


class _Reader:
    def __init__(
        self, plan: Plan, grading: Grading, stopped: str | None, hidden: list[str]
    ) -> None:
        self._plan = plan
        self._grading = grading
        self._stopped = stopped
        self._hidden = hidden

    def row(self, test: str) -> dict[str, Any]:
        outcome = self._grading.test_outcomes.get(test)
        if outcome is None:
            outcome = ACCEPTED if self._finished(test) else SKIPPED
        values: dict[str, Any] = {}
        if outcome != SKIPPED:
            for name, entry in self._plan.report.items():
                if not self._plan.per_test(entry.step):
                    continue
                outputs = self._grading.per_test.get(entry.step, {}).get(test, {})
                if entry.output in outputs:
                    values[name] = self._reported(outputs[entry.output])
        return {"test": test, "outcome": outcome, "values": values}

    def once_values(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, entry in self._plan.report.items():
            if self._plan.per_test(entry.step):
                continue
            outputs = self._grading.once.get(entry.step, {})
            if entry.output in outputs:
                values[name] = self._reported(outputs[entry.output])
        return values

    def _finished(self, test: str) -> bool:
        """Whether a test with no outcome of its own got through: every test did
        when the run went to the end, none did when a step that runs once
        stopped it, and when a fault struck, a test every per-test step of
        which had already run for it.
        """
        if self._stopped is None:
            return True
        if self._stopped != SYSTEM_ERROR:
            return False
        steps = {step.id for step in self._plan.steps if step.kind != "once"}
        covering = [s for s in steps if test in self._plan.tests_of(s)]
        return bool(covering) and all(
            test in self._grading.per_test.get(step_id, {}) for step_id in covering
        )

    def _reported(self, value: Value) -> Any:
        """A number as it is, a text masked and cut. The plan lets the report
        read nothing else.
        """
        if isinstance(value, str):
            return _cut(_masked(value, self._hidden))
        if isinstance(value, int | Decimal) and not isinstance(value, bool):
            return value
        raise TypeError(f"a reported value cannot be {type(value).__name__}")


def _hidden(secrets: Iterable[str]) -> list[str]:
    """The secrets' values, longest first, so one that holds another is
    replaced whole.
    """
    return sorted({secret for secret in secrets if secret}, key=len, reverse=True)


def _masked(text: str, hidden: list[str]) -> str:
    for secret in hidden:
        text = text.replace(secret, MASK)
    return text


def _error(message: str, hidden: list[str]) -> str:
    return _cut(
        _masked(message.strip(), hidden) or "the harness failed without saying why"
    )


def _cut(text: str) -> str:
    if len(text) <= TEXT_LIMIT:
        return text
    return text[: TEXT_LIMIT - 30] + "\n... (cut short)"
