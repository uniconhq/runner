"""Running a plan: the rules the forge compiler relies on.

1. A test with no file in a per-test contestant input is skipped before any
   step runs, and left out of every per-test step.
2. Steps run in plan order, every step that runs once first. A step's file
   and folder inputs are copied into its working directory under in/, files
   from earlier steps included; nothing a step wrote is visible to another
   step except through the plan's references.
3. A step that runs once and returns an outcome other than accepted stops the
   grading there: every later step is skipped and so is every test.
4. A per-test step runs only for the tests with no outcome yet. One that
   returns an outcome other than accepted for a test gives the test that
   outcome and skips its later steps (left out of a batch).
5. Anything unexpected is a GradingError, which stops the run as a
   system_error. A container of a single-test step killed by its wall or
   memory limit gives that test time_limit or memory_limit; a batch or
   run-once step killed that way is a fault. This machine gives a step no
   network and no GPUs, so a step that asks for either is a fault too.

What a step wrote is checked against the primitive contract and the plan
before anything reads it: outputs.json's shape, one batch entry per item in
order, the total under out/ within the step's output limit, and per item an
outcome a step may return, every output the plan declares for the step and no
other, each of its declared type (a file a regular file under out/, a folder
a directory under out/, a number finite), every output without a `?` present
on accepted, and every number the report reads within its bounds, compared
exactly as written.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from unicon_harness import exact
from unicon_harness.contracts import SCHEMA_VERSION, violation
from unicon_harness.faults import GradingError
from unicon_harness.files import (
    copy_into,
    folder_inside,
    inside,
    read_bounded,
    tree,
    tree_size,
)
from unicon_harness.plan import Item, Plan, Step, pieces
from unicon_harness.runlog import RunLog
from unicon_harness.sandbox import MIB, Ended, Sandbox
from unicon_harness.submission import Given, Scalar
from unicon_harness.workspace import Workspace

ACCEPTED = "accepted"
SKIPPED = "skipped"
OUTCOMES = (
    "accepted",
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


@dataclass(frozen=True)
class FolderValue:
    """A folder a value names: its name, and its files by their paths in it."""

    name: str
    files: tuple[tuple[str, Path], ...]


Value = Scalar | FileValue | FolderValue
Outputs = dict[str, Value]


@dataclass
class Grading:
    """What running the plan left: every step's outputs, each test's first
    non-accepted outcome, and the outcome that stopped the run, if a step
    that runs once stopped it.
    """

    once: dict[str, Outputs] = field(default_factory=dict)
    per_test: dict[str, dict[str, Outputs]] = field(default_factory=dict)
    test_outcomes: dict[str, str] = field(default_factory=dict)
    stopped: str | None = None
    stopped_by: str | None = None


class Placer:
    """Lays one container's input files out under in/, each distinct value
    once, so a hundred tests sharing one binary copy it once: a file at
    in/<n>/<its own name>, a folder as one directory in/<n>/<its name>/ with
    its layout, and a file given to a folder port as a folder holding that
    one file, in/<n>/<port>/<its own name>.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._placed: dict[object, str] = {}

    def file(self, source: Path) -> str:
        return self._place(source, source.name, ((source.name, source),), False)

    def folder(self, folder: FolderValue) -> str:
        return self._place(folder, folder.name, folder.files, True)

    def file_as_folder(self, source: Path, port: str) -> str:
        return self._place((port, source), port, ((source.name, source),), True)

    def _place(
        self,
        key: object,
        name: str,
        files: tuple[tuple[str, Path], ...],
        folder: bool,
    ) -> str:
        known = self._placed.get(key)
        if known is not None:
            return known
        relative = f"in/{len(self._placed) + 1}/{name}"
        try:
            if folder:
                (self._directory / relative).mkdir(parents=True)
                for inner, source in files:
                    copy_into(source, self._directory / relative / inner)
            else:
                copy_into(files[0][1], self._directory / relative)
        except OSError as exc:
            raise GradingError(f"could not copy {name} in: {exc.strerror}") from None
        self._placed[key] = relative
        return relative


class Grader:
    def __init__(
        self,
        plan: Plan,
        task: Path,
        given: Mapping[str, Given],
        secrets: Mapping[str, str],
        workspace: Workspace,
        sandbox: Sandbox,
        log: RunLog,
        run_ends: float,
        progress: Progress,
    ) -> None:
        self._plan = plan
        self._task = task
        self._given = given
        self._secrets = secrets
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

    @property
    def grading(self) -> Grading:
        """What the run has left so far, which a fault part way keeps."""
        return self._grading

    def run(self) -> Grading:
        self._skip_unanswered()
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

    def _skip_unanswered(self) -> None:
        """Skip every test a per-test contestant input has no file for."""
        for input_id, declared in self._plan.contestant.items():
            if not declared.per_test:
                continue
            answered = self._given[input_id].tests
            for test in self._plan.tests:
                if test not in answered and test not in self._grading.test_outcomes:
                    self._log.tell(
                        f"test {test}: skipped, input {input_id} has no file for it"
                    )
                    self._grading.test_outcomes[test] = SKIPPED

    def _once(self, step: Step) -> None:
        ended, outputs = self._container(step, list(step.items))
        if ended.killed is not None:
            raise GradingError(_killed(step, ended))
        if outputs is None:
            raise GradingError(_no_outputs(step, ended))
        self._grading.once[step.id] = outputs[0]
        outcome = outputs[0]["outcome"]
        if outcome != ACCEPTED:
            self._log.tell(f"{_named(step)}: {outcome}, so the run stops here")
            self._grading.stopped = str(outcome)
            self._grading.stopped_by = step.id
        else:
            self._log.tell(f"{_named(step)}: {outcome}")

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
        self._log.tell(f"{_named(step)}: {outputs[0]['outcome']}")

    def _batch(self, step: Step) -> None:
        items = [i for i in step.items if i.test not in self._grading.test_outcomes]
        left_out = len(step.items) - len(items)
        if not items:
            self._skip(step, left_out, "every test of it already has an outcome")
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
            + (f", {left_out} left out as already done" if left_out else "")
        )
        for item, item_outputs in zip(items, outputs, strict=True):
            test = _tested(item)
            self._record(step, test, item_outputs)
            self._log.tell(f"  test {test}: {item_outputs['outcome']}")

    def _record(self, step: Step, test: str, outputs: Outputs) -> None:
        self._grading.per_test.setdefault(step.id, {})[test] = outputs
        outcome = outputs["outcome"]
        if outcome != ACCEPTED:
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
        if step.network:
            raise GradingError(
                f"step {step.label} asks for the network, and this machine gives no "
                "network to a step"
            )
        if step.limits.gpus > 0:
            raise GradingError(
                f"step {step.label} asks for GPUs, and this machine has no GPUs"
            )
        self._containers += 1
        directory = self._workspace.new_step_dir(f"{self._containers:03d}-{step.id}")
        placer = Placer(directory)
        document: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
        if step.kind == "batch":
            document["batch"] = [
                {"test": item.test, "inputs": self._inputs(step, item, placer)}
                for item in items
            ]
        else:
            document["inputs"] = self._inputs(step, items[0], placer)
        (directory / "inputs.json").write_text(
            exact.dumps(document, indent=2), encoding="utf-8"
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
            name: _placed(
                self._resolve(step, item, name, value), placer, name, step.folders
            )
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
            return self._task_value(value["task"])
        if "submission" in value:
            return self._contestant(value["submission"], item.test, where)
        if "secret" in value:
            secret = self._secrets.get(value["secret"])
            if secret is None:
                raise GradingError(
                    f"the envelope has no secret {value['secret']}, which {where} needs"
                )
            return secret
        if "template" in value:
            parts = [self._scalar(part["submission"], where) for part in value["parts"]]
            return "".join(
                spelled(parts[piece]) if isinstance(piece, int) else piece
                for piece in pieces(value["template"])
            )
        return self._output(value["step"], value["output"], item.test, where)

    def _task_value(self, path: str) -> Value:
        if not path.endswith("/"):
            return FileValue(inside(self._task, path, f"task file {path}"))
        relative = path.removesuffix("/")
        what = f"task folder {path}"
        root = folder_inside(self._task, relative, what)
        return FolderValue(relative.rsplit("/", 1)[-1], tree(root, what))

    def _contestant(self, input_id: str, test: str | None, where: str) -> Value:
        given = self._given[input_id]
        if self._plan.contestant[input_id].per_test:
            if test is None or test not in given.tests:
                raise GradingError(
                    f"input {input_id} has no file for test {test}, which {where} needs"
                )
            return FileValue(given.tests[test])
        if self._plan.contestant[input_id].type == "folder":
            return FolderValue(input_id, given.files)
        if given.files:
            return FileValue(given.files[0][1])
        return self._scalar(input_id, where)

    def _scalar(self, input_id: str, where: str) -> Scalar:
        value = self._given[input_id].value
        if value is None:
            raise GradingError(f"input {input_id} has no value, which {where} needs")
        return value

    def _output(self, source: str, output: str, test: str | None, where: str) -> Value:
        if not self._plan.per_test(source):
            outputs = self._grading.once.get(source, {})
            if output not in outputs:
                raise GradingError(
                    f"step {source} gave no output {output}, which {where} needs"
                )
            return outputs[output]
        runs = self._grading.per_test.get(source, {})
        if test is None or output not in runs.get(test, {}):
            raise GradingError(
                f"step {source} gave no output {output} for test {test}, which "
                f"{where} needs"
            )
        return runs[test][output]

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
            document = exact.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
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
            tests = [entry["test"] for entry in entries]
            wanted = [item.test for item in items]
            if tests != wanted:
                raise GradingError(
                    f"{what} answers for tests {tests}, and the batch was {wanted}"
                )
            raw_outputs = [(entry["test"], entry["outputs"]) for entry in entries]
        else:
            if "outputs" not in document:
                raise GradingError(
                    f"{what} has a batch, and step {step.label} is not one"
                )
            raw_outputs = [(items[0].test, document["outputs"])]
        budget = step.limits.output_mb * MIB * len(items)
        if tree_size(directory / "out") > budget:
            raise GradingError(
                f"step {step.label} wrote more than {step.limits.output_mb} MB per run "
                "under out/"
            )
        read = [
            self._read_values(step, directory, test, one) for test, one in raw_outputs
        ]
        for (test, _), outputs in zip(raw_outputs, read, strict=True):
            self._check_bounds(step, test, outputs)
        return read

    def _read_values(
        self, step: Step, directory: Path, test: str | None, raw: dict[str, Any]
    ) -> Outputs:
        where = f"step {step.id} for test {test}" if test else f"step {step.label}"
        outputs: Outputs = {}
        for name, value in raw.items():
            declared = step.outputs.get(name)
            if declared is None:
                raise GradingError(
                    f"{where} wrote output {name}, which its primitive does not declare"
                )
            outputs[name] = _read_value(directory, where, name, declared.type, value)
        outcome = outputs.get("outcome")
        if outcome is None:
            raise GradingError(f"{where} returned no outcome")
        if outcome not in STEP_OUTCOMES:
            raise GradingError(
                f"{where} returned outcome {outcome!r}, which is not one a step may "
                "return"
            )
        if outcome == ACCEPTED:
            for name, declared in step.outputs.items():
                if not declared.optional and name not in outputs:
                    raise GradingError(
                        f"{where} returned accepted without its output {name}"
                    )
        return outputs

    def _check_bounds(self, step: Step, test: str | None, outputs: Outputs) -> None:
        """Every number the report reads from this step, within the bounds the
        report gives it, compared exactly as written.
        """
        for name, entry in self._plan.report.items():
            value = outputs.get(entry.output)
            if entry.step != step.id or not isinstance(value, int | Decimal):
                continue
            where = f"report {name}, output {entry.output} of step {step.id}" + (
                f" for test {test}" if test else ""
            )
            if entry.at_least is not None and value < entry.at_least:
                raise GradingError(
                    f"{where}, is {value}, below its at_least of {entry.at_least}"
                )
            if entry.at_most is not None and value > entry.at_most:
                raise GradingError(
                    f"{where}, is {value}, above its at_most of {entry.at_most}"
                )


def spelled(value: Scalar) -> str:
    """A scalar as a template writes it in: a number as the shortest decimal,
    with no exponent, no trailing zeros and no point when it is whole; a
    boolean as true or false; text as it is.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return "0" if text in ("-0", "") else text
    return value


def _read_value(
    directory: Path, where: str, name: str, declared: str, value: Any
) -> Value:
    """One output, as its declared type, or a fault saying how it is not."""
    what = f"output {name} of {where}"
    if declared in ("file", "folder"):
        if not isinstance(value, dict) or declared not in value:
            raise GradingError(f"{what} is {_kind(value)}, not a {declared}")
        relative = value[declared]
        if not relative.startswith("out/"):
            raise GradingError(f"{what}, {relative}, is not under out/")
        if declared == "file":
            return FileValue(inside(directory, relative, f"{what}, {relative},"))
        root = folder_inside(directory, relative, f"{what}, {relative},")
        return FolderValue(
            relative.rsplit("/", 1)[-1], tree(root, f"{what}, {relative},")
        )
    if declared == "number":
        if isinstance(value, bool) or not isinstance(value, int | Decimal):
            raise GradingError(f"{what} is {_kind(value)}, not a number")
        if isinstance(value, Decimal) and not value.is_finite():
            raise GradingError(f"{what} is {value}, not a finite number")
        return value
    if declared == "boolean":
        if not isinstance(value, bool):
            raise GradingError(f"{what} is {_kind(value)}, not a boolean")
        return value
    if not isinstance(value, str):
        raise GradingError(f"{what} is {_kind(value)}, not text")
    return value


def _kind(value: Any) -> str:
    if isinstance(value, dict):
        return "a file" if "file" in value else "a folder"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, str):
        return "text"
    return "a number"


def _tested(item: Item) -> str:
    """The test of an item of a per-test step, which the plan always names."""
    if item.test is None:
        raise GradingError("a per-test step has an item without a test")
    return item.test


def _placed(value: Value, placer: Placer, port: str, folders: frozenset[str]) -> Any:
    if isinstance(value, FileValue):
        if port in folders:
            return {"folder": placer.file_as_folder(value.path, port)}
        return {"file": placer.file(value.path)}
    if isinstance(value, FolderValue):
        return {"folder": placer.folder(value)}
    return value


def _named(step: Step) -> str:
    """A step as the run log names it: its label and its primitive."""
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
