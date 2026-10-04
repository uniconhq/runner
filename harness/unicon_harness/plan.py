"""Reading `plans/<stage>.json` from the task checkout and refusing one the
harness cannot run exactly. The schema fixes the shape; this module checks
what a schema cannot say: that a step id names one step or the runs of one
foreach, that every test named is in the plan's test list, and that every
reference points at a step that runs earlier.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from unicon_harness.contracts import SCHEMA_VERSION, violation
from unicon_harness.faults import GradingError
from unicon_harness.files import inside, read_bounded

PLAN_LIMIT_BYTES = 32 * 1024 * 1024
STAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

Kind = Literal["once", "test", "batch"]


@dataclass(frozen=True)
class Limits:
    time_ms: int
    cpu_ms: int
    memory_mb: int
    pids: int
    output_mb: int


@dataclass(frozen=True)
class Item:
    """One test's inputs in a batch step, or the inputs of a step that runs once
    (test None) or for one test.
    """

    test: str | None
    inputs: Mapping[str, Any]


@dataclass(frozen=True)
class Step:
    index: int
    id: str
    primitive: str
    image: str
    limits: Limits
    kind: Kind
    items: tuple[Item, ...]

    @property
    def test(self) -> str | None:
        """The test of a step that runs for one test."""
        return self.items[0].test if self.kind == "test" else None

    @property
    def label(self) -> str:
        return f"{self.id} for test {self.test}" if self.kind == "test" else self.id


@dataclass(frozen=True)
class Reference:
    step: str
    output: str


@dataclass(frozen=True)
class VerdictMap:
    outcome: Reference
    metrics: Mapping[str, Reference]
    time_ms: Reference | None
    memory_kb: Reference | None
    summary: Reference | None


@dataclass(frozen=True)
class Plan:
    harness_image: str
    stage: str
    tests: tuple[str, ...]
    steps: tuple[Step, ...]
    verdict: VerdictMap

    def per_test(self, step_id: str) -> bool:
        """Whether the step with this id runs per test, as one run per test or as
        a batch.
        """
        return any(s.id == step_id and s.kind != "once" for s in self.steps)

    def tests_of(self, step_id: str) -> list[str]:
        """The tests a per-test step covers, in plan order."""
        return [
            item.test
            for step in self.steps
            if step.id == step_id
            for item in step.items
            if item.test is not None
        ]


def path_for(task_checkout: Path, stage: str) -> Path:
    if not STAGE.match(stage):
        raise GradingError(f"the stage {stage!r} cannot name a plan file")
    return inside(task_checkout, f"plans/{stage}.json", f"the plan plans/{stage}.json")


def load(task_checkout: Path, stage: str) -> Plan:
    path = path_for(task_checkout, stage)
    raw = read_bounded(path, PLAN_LIMIT_BYTES, f"the plan plans/{stage}.json")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GradingError(f"plans/{stage}.json is not JSON: {exc}") from None
    return parse(document, stage)


def parse(document: Any, stage: str) -> Plan:
    if not isinstance(document, dict):
        raise GradingError("the plan is not a JSON object")
    declared = document.get("schema_version")
    if declared != SCHEMA_VERSION:
        raise GradingError(
            f"the plan declares schema_version {declared!r}, and this harness only "
            f"speaks {SCHEMA_VERSION}"
        )
    found = violation(document, "plan")
    if found is not None:
        raise GradingError(f"the plan does not match plan.schema.json {found}")
    if document["stage"] != stage:
        raise GradingError(
            f"plans/{stage}.json is the plan for stage {document['stage']!r}"
        )
    plan = Plan(
        harness_image=document["harness_image"],
        stage=document["stage"],
        tests=tuple(document["tests"]),
        steps=tuple(_step(i, s) for i, s in enumerate(document["steps"])),
        verdict=_verdict(document["verdict"]),
    )
    _check(plan)
    return plan


def _step(index: int, raw: dict[str, Any]) -> Step:
    if "batch" in raw:
        kind: Kind = "batch"
        items = tuple(Item(i["test"], i["inputs"]) for i in raw["batch"])
    elif "test" in raw:
        kind = "test"
        items = (Item(raw["test"], raw["inputs"]),)
    else:
        kind = "once"
        items = (Item(None, raw["inputs"]),)
    return Step(
        index=index,
        id=raw["id"],
        primitive=raw["primitive"],
        image=raw["image"],
        limits=Limits(**raw["limits"]),
        kind=kind,
        items=items,
    )


def _verdict(raw: dict[str, Any]) -> VerdictMap:
    def reference(value: dict[str, str] | None) -> Reference | None:
        return None if value is None else Reference(value["step"], value["output"])

    tests = raw.get("tests", {})
    return VerdictMap(
        outcome=Reference(raw["outcome"]["step"], raw["outcome"]["output"]),
        metrics={
            name: Reference(value["step"], value["output"])
            for name, value in raw.get("metrics", {}).items()
        },
        time_ms=reference(tests.get("time_ms")),
        memory_kb=reference(tests.get("memory_kb")),
        summary=reference(raw.get("summary")),
    )


def _check(plan: Plan) -> None:
    known_tests = set(plan.tests)
    kinds: dict[str, Kind] = {}
    seen_runs: set[tuple[str, str | None]] = set()
    for step in plan.steps:
        earlier = kinds.get(step.id)
        if earlier is not None and not (earlier == step.kind == "test"):
            raise GradingError(
                f"the plan has more than one step with id {step.id!r}, and they are "
                "not the runs of one foreach"
            )
        tests_here = [item.test for item in step.items]
        for test in tests_here:
            if test is not None and test not in known_tests:
                raise GradingError(
                    f"step {step.id} names test {test!r}, which is not in the "
                    "plan's tests"
                )
            if (step.id, test) in seen_runs:
                raise GradingError(f"step {step.id} runs test {test!r} twice")
            seen_runs.add((step.id, test))
        for item in step.items:
            for name, value in item.inputs.items():
                _check_input(plan, kinds, seen_runs, step, name, value)
        kinds[step.id] = step.kind
    for reference in _verdict_references(plan.verdict):
        if reference.step not in kinds:
            raise GradingError(
                f"the plan's verdict reads step {reference.step!r}, which the plan "
                "does not have"
            )


def _check_input(
    plan: Plan,
    kinds: Mapping[str, Kind],
    seen_runs: set[tuple[str, str | None]],
    step: Step,
    name: str,
    value: Mapping[str, Any],
) -> None:
    if "step" not in value:
        return
    source = value["step"]
    where = f"input {name} of step {step.id}"
    if source not in kinds:
        raise GradingError(
            f"{where} reads step {source!r}, which does not run before it"
        )
    test = value.get("test")
    if test is None:
        return
    if kinds[source] == "once":
        raise GradingError(
            f"{where} reads step {source} for one test, but it runs once"
        )
    if (source, test) not in seen_runs:
        raise GradingError(
            f"{where} reads step {source} for test {test!r}, which it does not run"
        )


def _verdict_references(verdict: VerdictMap) -> list[Reference]:
    found = [verdict.outcome, *verdict.metrics.values()]
    found += [r for r in (verdict.time_ms, verdict.memory_kb, verdict.summary) if r]
    return found
