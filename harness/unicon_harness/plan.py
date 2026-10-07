"""Reading `plans/plan.json` from the task checkout and refusing one the harness
cannot run exactly. The schema fixes the shape; this module checks what a
schema cannot say: that a step id names one step or the entries of one
per-test step, that every step that runs once comes before every step that
runs per test, that every test named is in the plan's test list, that every
reference points at an output declared by another step that runs earlier,
and that a step that runs once reads no step that runs per test, that every
contestant input a value names is one the plan declares, of the kind the
value needs, that every template is well formed, and that the report reads
only text and number outputs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from unicon_harness import exact
from unicon_harness.contracts import SCHEMA_VERSION, violation
from unicon_harness.faults import GradingError
from unicon_harness.files import inside, read_bounded

PLAN_FILE = "plans/plan.json"
PLAN_LIMIT_BYTES = 32 * 1024 * 1024
SCALARS = frozenset({"text", "number", "boolean", "enum"})
REPORTABLE = frozenset({"text", "number"})
TEMPLATE_TOKEN = re.compile(r"\{\{|\}\}|\{([0-9]+)\}|[{}]")

Kind = Literal["once", "test", "batch"]
Number = int | Decimal


@dataclass(frozen=True)
class Limits:
    time_ms: int
    cpu_ms: int
    memory_mb: int
    pids: int
    output_mb: int
    gpus: int


@dataclass(frozen=True)
class Output:
    """A declared output port: its type, and whether an accepted result may
    leave it out.
    """

    type: str
    optional: bool


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
    network: bool
    limits: Limits
    outputs: Mapping[str, Output]
    folders: frozenset[str]
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
class ContestantInput:
    type: str
    options: tuple[str, ...]
    per_test: bool


@dataclass(frozen=True)
class Report:
    """One name of the workflow's report: the output it reads and the bounds a
    number must keep, compared exactly as written.
    """

    step: str
    output: str
    at_least: Number | None
    at_most: Number | None


@dataclass(frozen=True)
class Plan:
    harness_image: str
    tests: tuple[str, ...]
    contestant: Mapping[str, ContestantInput]
    steps: tuple[Step, ...]
    report: Mapping[str, Report]

    def per_test(self, step_id: str) -> bool:
        """Whether the step with this id runs per test, as one run per test or as
        a batch.
        """
        return any(s.id == step_id and s.kind != "once" for s in self.steps)

    def output(self, step_id: str, name: str) -> Output:
        """An output port a step of the plan declares."""
        return next(s.outputs[name] for s in self.steps if s.id == step_id)

    def tests_of(self, step_id: str) -> list[str]:
        """The tests a per-test step covers, in plan order."""
        return [
            item.test
            for step in self.steps
            if step.id == step_id
            for item in step.items
            if item.test is not None
        ]


def load(task_checkout: Path) -> Plan:
    path = inside(task_checkout, PLAN_FILE, f"the plan {PLAN_FILE}")
    raw = read_bounded(path, PLAN_LIMIT_BYTES, f"the plan {PLAN_FILE}")
    try:
        document = exact.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise GradingError(f"{PLAN_FILE} is not JSON: {exc}") from None
    return parse(document)


def parse(document: Any) -> Plan:
    if not isinstance(document, dict):
        raise GradingError("the plan is not a JSON object")
    declared = document.get("schema_version")
    if declared != SCHEMA_VERSION:
        raise GradingError(
            f"the plan declares schema_version {declared!r}, and this harness only "
            f"speaks {SCHEMA_VERSION}"
        )
    if not exact.finite(document):
        raise GradingError("the plan holds a number that is not finite")
    found = violation(document, "plan")
    if found is not None:
        raise GradingError(f"the plan does not match plan.schema.json {found}")
    plan = Plan(
        harness_image=document["harness_image"],
        tests=tuple(document["tests"]),
        contestant={
            input_id: ContestantInput(
                type=entry["type"],
                options=tuple(entry.get("options", ())),
                per_test=entry.get("per_test", False),
            )
            for input_id, entry in document["contestant"].items()
        },
        steps=tuple(_step(i, s) for i, s in enumerate(document["steps"])),
        report={
            name: Report(
                step=entry["step"],
                output=entry["output"],
                at_least=entry.get("at_least"),
                at_most=entry.get("at_most"),
            )
            for name, entry in document["report"].items()
        },
    )
    _check(plan)
    return plan


def pieces(template: str) -> list[str | int]:
    """A template as its text and the indexes of the parts written into it, in
    order. `{{` and `}}` are literal braces; any other brace that does not
    close an index is refused with ValueError.
    """
    found: list[str | int] = []
    position = 0
    for token in TEMPLATE_TOKEN.finditer(template):
        found.append(template[position : token.start()])
        position = token.end()
        if token.group(1) is not None:
            found.append(int(token.group(1)))
        elif token.group(0) in ("{{", "}}"):
            found.append(token.group(0)[0])
        else:
            raise ValueError(f"a lone {token.group(0)} at character {token.start()}")
    found.append(template[position:])
    return [piece for piece in found if piece != ""]


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
        network=raw["network"],
        limits=Limits(**raw["limits"]),
        outputs={
            name.removesuffix("?"): Output(declared, name.endswith("?"))
            for name, declared in raw["outputs"].items()
        },
        folders=frozenset(raw.get("folders", ())),
        kind=kind,
        items=items,
    )


def _check(plan: Plan) -> None:
    known_tests = set(plan.tests)
    earlier: dict[str, Step] = {}
    seen_runs: set[tuple[str, str | None]] = set()
    per_test_seen = False
    for step in plan.steps:
        before = earlier.get(step.id)
        if before is not None and not (before.kind == step.kind == "test"):
            raise GradingError(
                f"the plan has more than one step with id {step.id!r}, and they are "
                "not the entries of one per-test step"
            )
        for item in step.items:
            test = item.test
            if test is not None and test not in known_tests:
                raise GradingError(
                    f"step {step.id} names test {test!r}, which is not in the "
                    "plan's tests"
                )
            if (step.id, test) in seen_runs:
                raise GradingError(f"step {step.id} runs test {test!r} twice")
            seen_runs.add((step.id, test))
            for name, value in item.inputs.items():
                _check_input(plan, earlier, step, name, value)
        if step.kind == "once" and per_test_seen:
            raise GradingError(
                f"step {step.id} runs once after a step that runs per test"
            )
        per_test_seen = per_test_seen or step.kind != "once"
        earlier[step.id] = step
    for name, entry in plan.report.items():
        _check_report(earlier, name, entry)


def _check_input(
    plan: Plan,
    earlier: Mapping[str, Step],
    step: Step,
    name: str,
    value: Mapping[str, Any],
) -> None:
    where = f"input {name} of step {step.label}"
    if "step" in value:
        if value["step"] == step.id:
            raise GradingError(f"{where} reads its own step {step.id}")
        source = earlier.get(value["step"])
        if source is None:
            raise GradingError(
                f"{where} reads step {value['step']!r}, which does not run before it"
            )
        if step.kind == "once" and source.kind != "once":
            raise GradingError(
                f"{where} reads step {source.id}, which runs per test, and step "
                f"{step.id} runs once"
            )
        if value["output"] not in source.outputs:
            raise GradingError(
                f"{where} reads output {value['output']} of step {source.id}, which "
                "it does not declare"
            )
    elif "submission" in value:
        given = _contestant(plan, value["submission"], where)
        if given.per_test and step.kind == "once":
            raise GradingError(
                f"{where} reads input {value['submission']}, which gives one file per "
                f"test, and step {step.id} runs once"
            )
    elif "template" in value:
        try:
            indexes = [p for p in pieces(value["template"]) if isinstance(p, int)]
        except ValueError as exc:
            raise GradingError(f"the template of {where} has {exc}") from None
        parts = value["parts"]
        if any(index >= len(parts) for index in indexes):
            raise GradingError(
                f"the template of {where} names a part it does not have, of "
                f"{len(parts)}"
            )
        for part in parts:
            if _contestant(plan, part["submission"], where).type not in SCALARS:
                raise GradingError(
                    f"the template of {where} writes in input {part['submission']}, "
                    "which is not text, a number, a boolean or an enum"
                )


def _contestant(plan: Plan, input_id: str, where: str) -> ContestantInput:
    given = plan.contestant.get(input_id)
    if given is None:
        raise GradingError(
            f"{where} reads input {input_id}, which the plan's contestant inputs do "
            "not declare"
        )
    return given


def _check_report(steps: Mapping[str, Step], name: str, entry: Report) -> None:
    step = steps.get(entry.step)
    if step is None:
        raise GradingError(
            f"the plan's report {name} reads step {entry.step!r}, which the plan "
            "does not have"
        )
    output = step.outputs.get(entry.output)
    if output is None:
        raise GradingError(
            f"the plan's report {name} reads output {entry.output} of step "
            f"{step.id}, which it does not declare"
        )
    if output.type not in REPORTABLE:
        raise GradingError(
            f"the plan's report {name} reads output {entry.output} of step "
            f"{step.id}, of type {output.type}, and a result carries only text "
            "and numbers"
        )
    bounded = entry.at_least is not None or entry.at_most is not None
    if bounded and output.type != "number":
        raise GradingError(
            f"the plan's report {name} gives bounds to a {output.type}, not a number"
        )
