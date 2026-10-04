"""Turning what a plan left into `verdict.json`, and the verdict for a run that
could not be graded.

A test's row: its outcome is the first non-accepted outcome among its steps,
otherwise the value of verdict.outcome for that test when it names a per-test
output of any name, otherwise accepted, and skipped when the run stopped
before its steps were done; time_ms and memory_kb come from the plan's
verdict.tests references, 0 when not given or skipped; its metrics from the
per-test references in verdict.metrics. The run's outcome is the one that
stopped it, otherwise read from verdict.outcome: for a per-test reference,
accepted when every test is, otherwise the first non-accepted test's outcome
in test order. Run metrics sum a per-test reference over the tests and take a
run-once one as it is; a metric that could not be computed is 0. The summary
is the verdict.summary output when it is non-empty text, otherwise one
sentence the harness writes.
"""

from __future__ import annotations

from typing import Any

from unicon_harness.contracts import SCHEMA_VERSION, violation
from unicon_harness.faults import GradingError
from unicon_harness.grading import ACCEPTED, STEP_OUTCOMES, Grading, Value
from unicon_harness.plan import Plan, Reference

SUMMARY_LIMIT = 10_000


def graded(plan: Plan, grading: Grading) -> dict[str, Any]:
    reader = _Reader(plan, grading)
    rows = [reader.row(test) for test in plan.tests]
    outcome = reader.outcome(rows)
    metrics = {
        name: reader.metric(name, ref, rows)
        for name, ref in plan.verdict.metrics.items()
    }
    return _document(
        outcome=outcome,
        metrics=metrics,
        tests=rows,
        summary=reader.summary(outcome, rows),
    )


def system_error(message: str) -> dict[str, Any]:
    """Nobody graded: no rows, no metrics, and the summary says why, for staff."""
    return _document(
        outcome="system_error",
        metrics={},
        tests=[],
        summary=_cut(message.strip() or "the harness failed without saying why"),
    )


def check(verdict: dict[str, Any]) -> str | None:
    """How the verdict breaks verdict.schema.json, or None."""
    return violation(verdict, "verdict")


def _document(
    *,
    outcome: str,
    metrics: dict[str, float | int],
    tests: list[dict[str, Any]],
    summary: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "outcome": outcome,
        "metrics": metrics,
        "tests": tests,
        "summary": summary,
        "log": None,
    }


class _Reader:
    def __init__(self, plan: Plan, grading: Grading) -> None:
        self._plan = plan
        self._grading = grading

    def row(self, test: str) -> dict[str, Any]:
        verdict = self._plan.verdict
        outcome = self._grading.test_outcomes.get(test)
        if outcome is None:
            outcome = "skipped" if self._unfinished(test) else self._outcome_of(test)
        skipped = outcome == "skipped"
        metrics = {
            name: 0 if skipped else self._number(self._for_test(ref, test), ref, 0)
            for name, ref in verdict.metrics.items()
            if self._plan.per_test(ref.step)
        }
        return {
            "id": test,
            "outcome": outcome,
            "time_ms": 0 if skipped else self._count(verdict.time_ms, test),
            "memory_kb": 0 if skipped else self._count(verdict.memory_kb, test),
            "metrics": metrics,
        }

    def outcome(self, rows: list[dict[str, Any]]) -> str:
        if self._grading.stopped is not None:
            return self._grading.stopped
        ref = self._plan.verdict.outcome
        if self._plan.per_test(ref.step):
            failed = [row["outcome"] for row in rows if row["outcome"] != ACCEPTED]
            return failed[0] if failed else ACCEPTED
        outputs = self._grading.once.get(ref.step, {})
        value = outputs.get(ref.output)
        if not isinstance(value, str) or value not in STEP_OUTCOMES:
            raise GradingError(
                f"the verdict's outcome, output {ref.output} of step {ref.step}, is "
                f"{value!r} and not an outcome"
            )
        return value

    def metric(
        self, name: str, ref: Reference, rows: list[dict[str, Any]]
    ) -> float | int:
        if self._plan.per_test(ref.step):
            total: float | int = sum(row["metrics"][name] for row in rows)
            return total
        if ref.step not in self._grading.once:
            return 0
        return self._number(self._grading.once[ref.step].get(ref.output), ref, None)

    def summary(self, outcome: str, rows: list[dict[str, Any]]) -> str:
        ref = self._plan.verdict.summary
        if ref is not None and not self._plan.per_test(ref.step):
            value = self._grading.once.get(ref.step, {}).get(ref.output)
            if isinstance(value, str) and value.strip():
                return _cut(value)
        if rows:
            accepted = sum(1 for row in rows if row["outcome"] == ACCEPTED)
            return f"{accepted} of {len(rows)} tests accepted."
        return f"The outcome is {outcome}."

    def _outcome_of(self, test: str) -> str:
        """A test's outcome when none of its steps returned another: what
        verdict.outcome gives for it when that names a per-test output, which
        need not be called outcome, otherwise accepted.
        """
        ref = self._plan.verdict.outcome
        if not self._plan.per_test(ref.step):
            return ACCEPTED
        value = self._for_test(ref, test)
        if value is None:
            return ACCEPTED
        if not isinstance(value, str) or value not in STEP_OUTCOMES:
            raise GradingError(
                f"the verdict's outcome, output {ref.output} of step {ref.step}, is "
                f"{value!r} for test {test} and not an outcome"
            )
        return value

    def _unfinished(self, test: str) -> bool:
        """Whether some per-test step of this test did not run, which happens
        only when a run-once step stopped the grading.
        """
        if self._grading.stopped is None:
            return False
        return any(
            test not in self._grading.per_test.get(step.id, {})
            for step in self._plan.steps
            if step.kind != "once" and any(item.test == test for item in step.items)
        )

    def _for_test(self, ref: Reference, test: str) -> Value | None:
        """The value for one test, or None when the step did not run for it."""
        if self._plan.per_test(ref.step):
            runs = self._grading.per_test.get(ref.step, {})
            if test not in runs:
                return None
            if ref.output not in runs[test]:
                raise GradingError(
                    f"step {ref.step} gave no output {ref.output} for test {test}, "
                    "which the verdict reads"
                )
            return runs[test][ref.output]
        return self._grading.once.get(ref.step, {}).get(ref.output)

    def _count(self, ref: Reference | None, test: str) -> int:
        if ref is None:
            return 0
        value = self._number(self._for_test(ref, test), ref, 0)
        if value < 0:
            raise GradingError(f"output {ref.output} of step {ref.step} is negative")
        return round(value)

    def _number(
        self, value: Value | None, ref: Reference, missing: int | None
    ) -> float | int:
        if value is None:
            if missing is None:
                raise GradingError(
                    f"step {ref.step} gave no output {ref.output}, which the "
                    "verdict reads"
                )
            return missing
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise GradingError(
                f"output {ref.output} of step {ref.step} is {value!r}, not a number"
            )
        return value


def _cut(text: str) -> str:
    if len(text) <= SUMMARY_LIMIT:
        return text
    return text[: SUMMARY_LIMIT - 30] + "\n... (cut short)"
