"""The escape fixtures' task holds together: every attack it runs is one
escape.c knows, has an input of its own and a verdict it may end on, and
the check judges escape.c's lines, refusing a run where a check not allowed
escaped. Running the task is deploy's end to end (`tests/e2e/test_escape.py`),
on a grading machine.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest
from ruamel.yaml import YAML

ESCAPE = Path(__file__).parents[1]
TASK = ESCAPE / "task"
VERDICTS = {
    "accepted",
    "time_limit",
    "memory_limit",
    "output_limit",
    "runtime_error",
}
"""What an attack may end on: refused, or stopped at a limit."""


@pytest.fixture(scope="module")
def check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check", TASK / "check" / "check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def attacks() -> list[str]:
    task = YAML(typ="safe").load((TASK / "task.yaml").read_text())
    [repeat] = task["repeat"]
    assert repeat["inputs"] == ["attack", "memory_limit"]
    return [Path(path).stem for path, _ in repeat["tuples"]]


def test_every_attack_is_one_of_escape_cs_with_an_input_and_a_verdict() -> None:
    modes = set(
        re.findall(r'!strcmp\(mode, "([a-z-]+)"\)', (ESCAPE / "escape.c").read_text())
    )
    expected = json.loads((ESCAPE / "expected.json").read_text())
    names = attacks()

    assert len(names) == len(set(names))
    assert set(names) <= modes
    assert set(names) == set(expected)
    assert sorted(path.stem for path in (TASK / "attacks").iterdir()) == sorted(names)
    for name in names:
        assert (TASK / "attacks" / f"{name}.in").read_text() == f"{name}\n"
        assert expected[name] and set(expected[name]) <= VERDICTS, name


def test_an_attack_that_leaves_something_comes_before_the_one_that_looks() -> None:
    names = attacks()

    assert names.index("leave-sysv") < names.index("find-sysv")
    assert names.index("mark") < names.index("read-mark")


def test_nothing_escaped_but_what_is_allowed_is_accepted(check: ModuleType) -> None:
    lines = [
        "bpf REFUSED Operation not permitted",
        "capability-bounding-set ESCAPED CapBnd:\t000001ffffffffff",
        "ids INFO uid 60001",
    ]

    assert check.judged(lines, {"capability-bounding-set"}) == (0, [])
    assert check.judged(lines, set()) == (
        1,
        ["capability-bounding-set ESCAPED CapBnd:\t000001ffffffffff"],
    )
    assert check.judged(["unknown mode x", ""], set()) == (1, [])
    assert check.judged([], set()) == (1, [])


def test_the_check_reports_what_escaped_and_tells_staff_every_line(
    check: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "report"
    report.mkdir()
    (tmp_path / "output").write_text("bpf ESCAPED map created\nmount REFUSED no\n")
    (tmp_path / "allowed").write_text(
        (TASK / "check" / "allowed").read_text() + "# a comment\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UNICON_REPORT", str(report))

    assert check.main() == 0

    assert (report / "verdict").read_text() == "1\n"
    assert (report / "escaped").read_text() == "1\n"
    assert (report / "message").read_text() == "bpf ESCAPED map created"
    assert not (report / "score").exists()
    assert "mount REFUSED no" in (report / "staff_message").read_text()


def test_the_allowed_checks_are_the_boxes_own(check: ModuleType) -> None:
    allowed = {
        line.partition("#")[0].strip()
        for line in (TASK / "check" / "allowed").read_text().splitlines()
    } - {""}

    assert allowed == {"write-dev-shm", "capability-bounding-set", "one-big-file"}
