"""Reading `submission.json` from the submission checkout: what the contestant
gave for each input, and where its files are, checked against the inputs the
plan declares.

A file input gives its one file and a folder input its files, each under
`files/<input id>/` at its path in the folder. A per-test file input gives at
most one file per test, at `files/<input id>/<group>/<test>` or
`files/<input id>/<group>/<test>.<ending>`, the ending not empty. A file
named any other way, or a second file for one test, is refused; a file for a
test the plan does not have, which a rejudge against a plan that dropped or
renamed the test meets, is noted in the CI log and not read; and a test
without a file is skipped. A text, number, boolean or enum input gives its
value. Numbers are read as the decimals they are written as.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from unicon_harness import exact
from unicon_harness.contracts import SCHEMA_VERSION, violation
from unicon_harness.faults import GradingError
from unicon_harness.files import inside, read_bounded
from unicon_harness.plan import ContestantInput, Plan
from unicon_harness.runlog import RunLog

SUBMISSION_FILE = "submission.json"
SUBMISSION_LIMIT_BYTES = 1024 * 1024
PER_TEST_FILE = re.compile(r"^([A-Za-z0-9_-]+/[A-Za-z0-9_-]+)(\.[^/]+)?$")

Scalar = str | int | Decimal | bool


@dataclass(frozen=True)
class Given:
    """One contestant input: its files by their path under `files/<input id>/`,
    its files by test for a per-test input, or its value.
    """

    files: tuple[tuple[str, Path], ...] = ()
    tests: dict[str, Path] = field(default_factory=dict)
    value: Scalar | None = None


def load(checkout: Path, plan: Plan, log: RunLog) -> dict[str, Given]:
    path = inside(checkout, SUBMISSION_FILE, SUBMISSION_FILE)
    raw = read_bounded(path, SUBMISSION_LIMIT_BYTES, SUBMISSION_FILE)
    try:
        document = exact.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise GradingError(f"{SUBMISSION_FILE} is not JSON: {exc}") from None
    if not isinstance(document, dict):
        raise GradingError(f"{SUBMISSION_FILE} is not a JSON object")
    declared = document.get("schema_version")
    if declared != SCHEMA_VERSION:
        raise GradingError(
            f"{SUBMISSION_FILE} declares schema_version {declared!r}, and this harness "
            f"only speaks {SCHEMA_VERSION}"
        )
    found = violation(document, "submission")
    if found is not None:
        raise GradingError(
            f"{SUBMISSION_FILE} does not match submission.schema.json {found}"
        )
    entries: dict[str, Any] = document["inputs"]
    for input_id in plan.contestant:
        if input_id not in entries:
            raise GradingError(
                f"{SUBMISSION_FILE} has no input {input_id}, which the plan declares"
            )
    for input_id in entries:
        if input_id not in plan.contestant:
            raise GradingError(
                f"{SUBMISSION_FILE} gives input {input_id}, which the plan does not "
                "declare"
            )
    return {
        input_id: _given(
            checkout, plan, log, input_id, plan.contestant[input_id], entry
        )
        for input_id, entry in entries.items()
    }


def _given(
    checkout: Path,
    plan: Plan,
    log: RunLog,
    input_id: str,
    declared: ContestantInput,
    entry: dict[str, Any],
) -> Given:
    """One entry, already checked against the schema."""
    where = f"input {input_id} of {SUBMISSION_FILE}"
    if "value" in entry:
        return Given(value=_value(where, declared, entry["value"]))
    if declared.type not in ("file", "folder"):
        raise GradingError(f"{where} gives files, and its type is {declared.type}")
    prefix = f"files/{input_id}/"
    files = []
    for relative in entry["files"]:
        if not relative.startswith(prefix):
            raise GradingError(f"{where} lists {relative}, outside {prefix}")
        source = inside(checkout, relative, f"submission file {relative}")
        files.append((relative.removeprefix(prefix), source))
    if declared.per_test:
        return Given(tests=_by_test(where, plan, log, files))
    if declared.type == "file" and len(files) != 1:
        raise GradingError(f"{where} gives {len(files)} files, and it is one file")
    return Given(files=tuple(files))


def _value(where: str, declared: ContestantInput, value: Scalar) -> Scalar:
    kind = declared.type
    if kind in ("file", "folder"):
        raise GradingError(f"{where} gives a value, and its type is {kind}")
    if kind == "number":
        number = not isinstance(value, bool) and isinstance(value, int | Decimal)
        if not number or (isinstance(value, Decimal) and not value.is_finite()):
            raise GradingError(f"{where} is {value!r}, not a finite number")
    elif kind == "boolean" and not isinstance(value, bool):
        raise GradingError(f"{where} is {value!r}, not a boolean")
    elif kind in ("text", "enum") and not isinstance(value, str):
        raise GradingError(f"{where} is {value!r}, not text")
    if kind == "enum" and value not in declared.options:
        raise GradingError(f"{where} is {value!r}, which is not one of its options")
    return value


def _by_test(
    where: str, plan: Plan, log: RunLog, files: list[tuple[str, Path]]
) -> dict[str, Path]:
    known = set(plan.tests)
    named: dict[str, Path] = {}
    for relative, source in files:
        matched = PER_TEST_FILE.match(relative)
        if matched is None:
            raise GradingError(
                f"{where} gives {relative}, which is not named <group>/<test> or "
                "<group>/<test>.<ending>"
            )
        test = matched.group(1)
        if test in named:
            raise GradingError(f"{where} gives more than one file for test {test}")
        named[test] = source
    for test in named:
        if test not in known:
            log.event(
                f"{where} gives a file for test {test}, which the plan does not "
                "have; it is not read"
            )
    return {test: source for test, source in named.items() if test in known}
