"""Running a plan: the rules the forge compiler relies on.

1. Steps run in plan order. A step's file inputs are copied into its working
   directory under in/, files from earlier steps included; nothing a step
   wrote is visible to another step except through the plan's references.
2. An output named outcome is special. A run-once step that returns an outcome
   other than accepted stops the grading there: every later step is skipped
   and the run's outcome is that one.
3. A per-test step that returns an outcome other than accepted for a test
   skips every later step of that test (left out of a batch), and that
   outcome is the test's.
4. Anything unexpected is a GradingError, which becomes a system_error. A
   container of a single-test step killed by its wall or memory limit gives
   that test time_limit or memory_limit; a batch or run-once step killed that
   way is a fault.

What a step wrote is checked against the primitive contract before anything
reads it: outputs.json's shape, one batch entry per item in order, every file
a regular file under out/, the total under out/ within the step's output
limit, and an outcome from the verdict's list.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unicon_harness.contracts import SCHEMA_VERSION, violation
from unicon_harness.faults import GradingError
from unicon_harness.files import copy_into, inside, read_bounded, tree_size
from unicon_harness.plan import Item, Plan, Step
from unicon_harness.runlog import RunLog
from unicon_harness.sandbox import MIB, Ended, Sandbox
from unicon_harness.submission import Given, Scalar
from unicon_harness.workspace import Workspace

ACCEPTED = "accepted"
OUTCOMES = (
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
)
STEP_OUTCOMES = frozenset(OUTCOMES) - {"skipped", "system_error"}
"""What a step may return as outcome. skipped is the harness's word for a test
it did not run, and a primitive that could not work says so with error.
"""

OUTPUTS_LIMIT_BYTES = 16 * MIB

Progress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class FileValue:
    """A file a value names, where the harness sees it."""

    path: Path


Value = Scalar | FileValue | list[FileValue] | list[Scalar]
Outputs = dict[str, Value]


@dataclass
class Grading:
    """What running the plan left: every step's outputs, each test's first
    non-accepted outcome, and the outcome that stopped the run, if one did.
    """

    once: dict[str, Outputs] = field(default_factory=dict)
    per_test: dict[str, dict[str, Outputs]] = field(default_factory=dict)
    test_outcomes: dict[str, str] = field(default_factory=dict)
    stopped: str | None = None
    stopped_by: str | None = None


class Placer:
    """Lays one container's input files out under in/: each distinct source file
    once, at in/<n>/<its own name>, so a hundred tests sharing one binary copy
    it once and a primitive still sees the file's name.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._placed: dict[Path, str] = {}

    def place(self, source: Path) -> str:
        known = self._placed.get(source)
        if known is not None:
            return known
        relative = f"in/{len(self._placed) + 1}/{source.name}"
        try:
            copy_into(source, self._directory / relative)
        except OSError as exc:
            raise GradingError(
                f"could not copy {source.name} in: {exc.strerror}"
            ) from None
        self._placed[source] = relative
        return relative


class Grader:
    def __init__(
        self,
        plan: Plan,
        task: Path,
        given: Mapping[str, Given],
        workspace: Workspace,
        sandbox: Sandbox,
        log: RunLog,
        run_ends: float,
        progress: Progress,
    ) -> None:
        self._plan = plan
        self._task = task
        self._given = given
        self._workspace = workspace
        self._sandbox = sandbox
        self._log = log
        self._run_ends = run_ends
        self._progress = progress
        self._grading = Grading()
        self._containers = 0
        self._done: dict[str, int] = {}
        self._totals: dict[str, int] = {}
        for step in plan.steps:
            self._totals[step.id] = self._totals.get(step.id, 0) + len(step.items)

    def run(self) -> Grading:
        for step in self._plan.steps:
            if self._grading.stopped is not None:
                self._log.tell(
                    f"{_named(step)}: skipped, the run stopped at "
                    f"{self._grading.stopped_by}"
                )
                continue
            if step.kind == "once":
                self._once(step)
            elif step.kind == "test":
                self._single(step)
            else:
                self._batch(step)
        return self._grading

    def _once(self, step: Step) -> None:
        ended, outputs = self._container(step, list(step.items))
        if ended.killed is not None:
            raise GradingError(_killed(step, ended))
        if outputs is None:
            raise GradingError(_no_outputs(step, ended))
        self._grading.once[step.id] = outputs[0]
        outcome = outputs[0].get("outcome")
        if outcome is not None and outcome != ACCEPTED:
            self._log.tell(f"{_named(step)}: {outcome}, so the run stops here")
            self._grading.stopped = str(outcome)
            self._grading.stopped_by = step.id
        else:
            self._log.tell(f"{_named(step)}: {outcome or 'done'}")

    def _single(self, step: Step) -> None:
        test = _tested(step.items[0])
        if test in self._grading.test_outcomes:
            self._skip(
                step, 1, f"test {test} is already {self._grading.test_outcomes[test]}"
            )
            return
        ended, outputs = self._container(step, list(step.items))
        if ended.killed == "run_clock":
            raise GradingError(_killed(step, ended))
        if ended.killed == "time":
            self._fail(test, step, "time_limit")
            return
        if outputs is None:
            if ended.out_of_memory:
                self._fail(test, step, "memory_limit")
                return
            raise GradingError(_no_outputs(step, ended))
        self._record(step, test, outputs[0])
        outcome = outputs[0].get("outcome")
        self._log.tell(f"{_named(step)}: {outcome or 'done'}")

    def _batch(self, step: Step) -> None:
        items = [i for i in step.items if i.test not in self._grading.test_outcomes]
        left_out = len(step.items) - len(items)
        if not items:
            self._skip(step, left_out, "every test of it has already failed")
            return
        ended, outputs = self._container(step, items, left_out)
        if ended.killed is not None:
            raise GradingError(_killed(step, ended))
        if outputs is None:
            if ended.out_of_memory:
                raise GradingError(
                    f"the batch of step {step.id} was killed at its memory limit of "
                    f"{step.limits.memory_mb} MB"
                )
            raise GradingError(_no_outputs(step, ended))
        self._log.tell(
            f"{_named(step)}: {len(items)} tests"
            + (f", {left_out} left out as already failed" if left_out else "")
        )
        for item, item_outputs in zip(items, outputs, strict=True):
            test = _tested(item)
            self._record(step, test, item_outputs)
            self._log.tell(f"  test {test}: {item_outputs.get('outcome') or 'done'}")

    def _record(self, step: Step, test: str, outputs: Outputs) -> None:
        self._grading.per_test.setdefault(step.id, {})[test] = outputs
        outcome = outputs.get("outcome")
        if outcome is not None and outcome != ACCEPTED:
            self._grading.test_outcomes[test] = str(outcome)

    def _fail(self, test: str, step: Step, outcome: str) -> None:
        self._log.tell(
            f"{_named(step)}: {outcome}, stopped at its "
            f"{'time' if outcome == 'time_limit' else 'memory'} limit"
        )
        self._grading.test_outcomes[test] = outcome

    def _skip(self, step: Step, count: int, why: str) -> None:
        self._log.tell(f"{_named(step)}: skipped, {why}")
        self._advance(step, count)

    def _advance(self, step: Step, count: int) -> None:
        done = self._done.get(step.id, 0) + count
        self._done[step.id] = done
        self._progress(step.id, done, self._totals[step.id])

    def _container(
        self, step: Step, items: list[Item], left_out: int = 0
    ) -> tuple[Ended, list[Outputs] | None]:
        """Run one container over `items` and read back what it wrote, or None
        for outputs when it wrote no outputs.json or was killed.
        """
        self._containers += 1
        directory = self._workspace.new_step_dir(f"{self._containers:03d}-{step.id}")
        placer = Placer(directory)
        document: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
        if step.kind == "batch":
            document["batch"] = [
                {"id": item.test, "inputs": self._inputs(step, item, placer)}
                for item in items
            ]
        else:
            document["inputs"] = self._inputs(step, items[0], placer)
        (directory / "inputs.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8"
        )
        self._log.event(
            f"step {step.label}: {step.primitive} from {step.image}"
            + (f", {len(items)} tests" if step.kind == "batch" else "")
        )
        ended = self._sandbox.run(step, directory, self._run_ends)
        self._log.event(
            f"step {step.label} ended in {ended.elapsed_ms} ms with exit code "
            f"{ended.exit_code}"
            + (", out of memory" if ended.out_of_memory else "")
            + (f", killed at the {_clock_name(ended)}" if ended.killed else "")
        )
        outputs = None if ended.killed else self._outputs(step, directory, items)
        self._advance(step, len(items) + left_out)
        return ended, outputs

    def _inputs(self, step: Step, item: Item, placer: Placer) -> dict[str, Any]:
        return {
            name: _placed(self._resolve(step, item, name, value), placer)
            for name, value in item.inputs.items()
        }

    def _resolve(
        self, step: Step, item: Item, name: str, value: Mapping[str, Any]
    ) -> Value:
        where = f"input {name} of step {step.label}"
        if "value" in value:
            literal: Scalar = value["value"]
            return literal
        if "task" in value:
            paths = value["task"]
            if isinstance(paths, str):
                return FileValue(inside(self._task, paths, f"task file {paths}"))
            return [FileValue(inside(self._task, p, f"task file {p}")) for p in paths]
        if "submission" in value:
            return self._contestant(value["submission"], value.get("field"), where)
        return self._output(value["step"], value["output"], value.get("test"), where)

    def _contestant(self, input_id: str, wanted: str | None, where: str) -> Value:
        given = self._given.get(input_id)
        if given is None:
            raise GradingError(
                f"submission.json has no input {input_id}, which {where} needs"
            )
        if wanted == "language":
            if given.language is None:
                raise GradingError(
                    f"input {input_id} has no language, which {where} needs"
                )
            return given.language
        if given.files:
            files = [FileValue(path) for path in given.files]
            return files[0] if len(files) == 1 else files
        if given.value is None:
            raise GradingError(f"input {input_id} has neither files nor a value")
        return given.value

    def _output(self, source: str, output: str, test: str | None, where: str) -> Value:
        if not self._plan.per_test(source):
            outputs = self._grading.once.get(source, {})
            if output not in outputs:
                raise GradingError(
                    f"step {source} gave no output {output}, which {where} needs"
                )
            return outputs[output]
        runs = self._grading.per_test.get(source, {})
        if test is not None:
            if output not in runs.get(test, {}):
                raise GradingError(
                    f"step {source} gave no output {output} for test {test}, which "
                    f"{where} needs"
                )
            return runs[test][output]
        return self._over_tests(source, output, where)

    def _over_tests(self, source: str, output: str, where: str) -> Value:
        """A per-test output over every test, in test order. A test the step did
        not reach has no value and is left out, except for outcome, where it
        gives that test's own outcome so a scorer still sees one per test.
        """
        runs = self._grading.per_test.get(source, {})
        covered = set(self._plan.tests_of(source))
        values: list[Any] = []
        for test in self._plan.tests:
            if test not in covered:
                continue
            if output in runs.get(test, {}):
                values.append(runs[test][output])
            elif output == "outcome" and test in self._grading.test_outcomes:
                values.append(self._grading.test_outcomes[test])
        if any(isinstance(v, list) for v in values):
            raise GradingError(
                f"{where} would be a list of lists, which no primitive takes"
            )
        kinds = {isinstance(v, FileValue) for v in values}
        if len(kinds) > 1:
            raise GradingError(f"{where} would mix files and values in one list")
        return values

    def _outputs(
        self, step: Step, directory: Path, items: list[Item]
    ) -> list[Outputs] | None:
        path = directory / "outputs.json"
        if not path.exists() and not path.is_symlink():
            return None
        what = f"outputs.json of step {step.label}"
        raw = read_bounded(
            inside(directory, "outputs.json", what), OUTPUTS_LIMIT_BYTES, what
        )
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GradingError(f"{what} is not JSON: {exc}") from None
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != SCHEMA_VERSION
        ):
            raise GradingError(
                f"{what} is not written against schema_version {SCHEMA_VERSION}"
            )
        found = violation(document, "primitive", "outputs_file")
        if found is not None:
            raise GradingError(f"{what} does not match primitive.schema.json {found}")
        if "error" in document:
            raise GradingError(f"step {step.label} could not work: {document['error']}")
        if step.kind == "batch":
            entries = document.get("batch")
            if entries is None:
                raise GradingError(
                    f"{what} has no batch, and step {step.id} is a batch"
                )
            ids = [entry["id"] for entry in entries]
            wanted = [item.test for item in items]
            if ids != wanted:
                raise GradingError(
                    f"{what} answers for tests {ids}, and the batch was {wanted}"
                )
            raw_outputs = [entry["outputs"] for entry in entries]
        else:
            if "outputs" not in document:
                raise GradingError(
                    f"{what} has a batch, and step {step.label} is not one"
                )
            raw_outputs = [document["outputs"]]
        budget = step.limits.output_mb * MIB * len(items)
        if tree_size(directory / "out") > budget:
            raise GradingError(
                f"step {step.label} wrote more than {step.limits.output_mb} MB per run "
                "under out/"
            )
        return [self._read_values(step, directory, one) for one in raw_outputs]

    def _read_values(self, step: Step, directory: Path, raw: dict[str, Any]) -> Outputs:
        outputs: Outputs = {}
        for name, value in raw.items():
            outputs[name] = self._read_value(step, directory, name, value)
        outcome = outputs.get("outcome")
        if outcome is not None and outcome not in STEP_OUTCOMES:
            raise GradingError(
                f"step {step.label} returned outcome {outcome!r}, which is not one a "
                "step may return"
            )
        return outputs

    def _read_value(self, step: Step, directory: Path, name: str, value: Any) -> Value:
        def file(entry: dict[str, str]) -> FileValue:
            relative = entry["file"]
            what = f"output {name} of step {step.label}, {relative}"
            if not relative.startswith("out/"):
                raise GradingError(f"{what}, is not under out/")
            return FileValue(inside(directory, relative, what))

        if isinstance(value, dict):
            return file(value)
        if isinstance(value, list):
            if all(isinstance(entry, dict) for entry in value):
                return [file(entry) for entry in value]
            scalars: list[Scalar] = value
            return scalars
        scalar: Scalar = value
        return scalar


def _tested(item: Item) -> str:
    """The test of an item of a per-test step, which the plan always names."""
    if item.test is None:
        raise GradingError("a per-test step has an item without a test")
    return item.test


def _placed(value: Value, placer: Placer) -> Any:
    if isinstance(value, FileValue):
        return {"file": placer.place(value.path)}
    if isinstance(value, list):
        return [_placed(entry, placer) for entry in value]
    return value


def _named(step: Step) -> str:
    """A step as the contestant's log names it: its label and its primitive."""
    return f"{step.label} ({step.primitive})"


def _killed(step: Step, ended: Ended) -> str:
    if ended.killed == "run_clock":
        return f"the run passed its wall clock during step {step.label}"
    return f"step {step.label} was killed at its time limit of {step.limits.time_ms} ms"


def _clock_name(ended: Ended) -> str:
    return "run's wall clock" if ended.killed == "run_clock" else "step's time limit"


def _no_outputs(step: Step, ended: Ended) -> str:
    if ended.out_of_memory:
        return (
            f"step {step.label} was killed at its memory limit of "
            f"{step.limits.memory_mb} MB"
        )
    return (
        f"step {step.label} exited with code {ended.exit_code} and wrote no "
        "outputs.json"
    )
