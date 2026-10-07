"""A whole grading run: the plan run against the fake Docker, the callbacks and
the log against the platform's routes, and the result that comes out.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support import (
    JUDGE,
    SELF_ID,
    VOLUME,
    Checkouts,
    Does,
    FakeContainer,
    FakeDocker,
    Platform,
    classic_plan,
    limits,
    mountinfo,
)
from unicon_harness import result as results
from unicon_harness.main import EXIT_NOT_DELIVERED, EXIT_OK, grade_and_report
from unicon_harness.report import Reporter
from unicon_harness.runlog import RunLog

SECRET = "sk-example-not-a-real-key"
"""The example envelope's one secret, model-key."""


def grade(
    made: Checkouts,
    docker: FakeDocker,
    platform: Platform,
    wall_seconds: int = 900,
    log: RunLog | None = None,
) -> int:
    envelope = platform.envelope_for(made, wall_seconds)
    log = log or RunLog(echo=False)
    log.hide(envelope["callback"]["token"])
    log.hide("X-Amz-Signature=log-secret")
    with httpx.Client(timeout=5) as client:
        reporter = Reporter(envelope, log, client=client, sleep=lambda _: None)
        return grade_and_report(envelope, log, reporter, lambda: docker, mountinfo)


def rows(result: dict[str, Any]) -> dict[str, str]:
    return {row["test"]: row["outcome"] for row in result["tests"]}


def step_dir(made: Checkouts, step: str, nth: int = 0) -> Path:
    """The working directory of the `nth` container of a step."""
    found = sorted(
        p
        for p in (made.root / "unicon-steps").iterdir()
        if p.name.split("-", 1)[1] == step
    )
    return found[nth]


def inputs_of(made: Checkouts, step: str, nth: int = 0) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (step_dir(made, step, nth) / "inputs.json").read_text(encoding="utf-8")
    )
    return document


def writes(text: str) -> Callable[[Path], None]:
    """A container body that writes `text` as outputs.json, as it is."""

    def write(work: Path) -> None:
        (work / "outputs.json").write_text(text, encoding="utf-8")

    return write


def judged_plan(tests: list[str]) -> dict[str, Any]:
    """The classic plan with a judge after the check, once per test: it reads
    the run's output, a rubric folder of the task and a secret, and the report
    reads its fraction, bounded by 0 and 1, and its note.
    """
    plan = classic_plan(tests)
    plan["steps"] += [
        {
            "id": "judge",
            "primitive": "acme/rubric@v1",
            "image": JUDGE,
            "network": False,
            "limits": limits(),
            "outputs": {"fraction?": "number", "note?": "text", "outcome": "outcome"},
            "folders": ["rubric"],
            "test": test,
            "inputs": {
                "answer": {"step": "run", "output": "output"},
                "rubric": {"task": "rubric/"},
                "api_key": {"secret": "model-key"},
                "strict": {"value": True},
            },
        }
        for test in tests
    ]
    plan["report"]["fraction"] = {
        "step": "judge",
        "output": "fraction",
        "at_least": 0,
        "at_most": 1,
    }
    plan["report"]["note"] = {"step": "judge", "output": "note"}
    return plan


@pytest.fixture
def judged(made: Checkouts) -> Checkouts:
    made.write_plan(judged_plan(list(made.tests)))
    rubric = made.task / "rubric"
    (rubric / "levels").mkdir(parents=True)
    (rubric / "rubric.md").write_text("be right\n", encoding="utf-8")
    (rubric / "levels" / "1.md").write_text("one\n", encoding="utf-8")
    return made


def test_a_correct_submission_is_accepted_on_every_test(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    assert grade(made, docker, platform) == EXIT_OK

    result = platform.result()
    assert results.check(result) is None
    assert result["stopped"] is None and result["error"] is None
    assert rows(result) == {
        "main/1": "accepted",
        "main/2": "accepted",
        "main/10": "accepted",
    }
    assert [row["test"] for row in result["tests"]] == ["main/1", "main/2", "main/10"]
    assert all(row["values"]["memory_kb"] == 1024 for row in result["tests"])
    assert all(isinstance(row["values"]["time_ms"], int) for row in result["tests"])
    assert result["values"] == {"log": "main.py: compiled for python"}
    assert result["stopped_by"] is None
    assert set(result) == {
        "schema_version",
        "stopped",
        "stopped_by",
        "tests",
        "values",
        "run_log",
        "error",
    }


def test_the_run_reports_started_progress_and_the_result_with_the_token(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    grade(made, docker, platform)

    events = platform.events()
    assert events[0] == "started"
    assert events[-1] == "finished"
    progress = [c for c in platform.callbacks if c["event"] == "progress"]
    assert {"event": "progress", "step": "compile", "done": 1, "total": 1} in progress
    assert {"event": "progress", "step": "run", "done": 3, "total": 3} in progress
    assert {"event": "progress", "step": "check", "done": 3, "total": 3} in progress
    assert set(platform.authorizations) == {"Bearer example-one-run-callback-token"}
    assert set(platform.callbacks[-1]) == {"event", "result"}


def test_the_log_is_put_once_and_carries_no_credential(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    grade(made, docker, platform)

    assert len(platform.logs) == 1
    text = platform.logs[0].decode()
    assert "example-one-run-callback-token" not in text
    assert "log-secret" not in text
    assert platform.result()["run_log"] == f"{platform.url}/log"


def test_the_run_log_says_what_ran_and_how_without_the_machine(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    """Each step by its id and primitive, each test's outcome and the result;
    no image, volume, container, exit code or path of the machine.
    """
    (made.task / "tests" / "main" / "2" / "answer").write_text("61\n", "utf-8")
    docker.does("run", Does(printed="the primitive's own chatter"))

    grade(made, docker, platform)

    text = platform.logs[0].decode()
    lines = [line.split("] ", 1)[1] for line in text.splitlines() if "] " in line]
    assert lines[0] == "Grading submission/3, attempt 1."
    assert "compile (unicon/compile@v2): accepted" in lines
    assert "run (unicon/sandbox-run@v2): 3 tests" in lines
    assert "  test main/2: accepted" in lines
    assert "check for test main/2 (unicon/diff-check@v2): wrong_answer" in lines
    assert lines[-1] == "2 of 3 tests accepted."
    for detail in (
        VOLUME,
        "sha256",
        SELF_ID[:12],
        "unicon-steps",
        "exit code",
        "chatter",
        "0199a2c1",
        str(made.root),
    ):
        assert detail not in text, detail


def test_a_system_error_tells_the_log_only_that_there_was_one(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    docker.refuse_create = "unicon socket filter: image is not on the machine's list"

    grade(made, docker, platform)

    text = platform.logs[0].decode()
    assert "The run could not be graded because of a system error." in text
    assert "socket filter" not in text and "machine's list" not in text
    assert "tests accepted" not in text


def test_a_wrong_answer_ends_only_its_test_and_keeps_its_values(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    (made.task / "tests" / "main" / "2" / "answer").write_text("61\n", "utf-8")

    assert grade(made, docker, platform) == EXIT_OK

    result = platform.result()
    assert result["stopped"] is None
    assert rows(result) == {
        "main/1": "accepted",
        "main/2": "wrong_answer",
        "main/10": "accepted",
    }
    wrong = result["tests"][1]
    assert set(wrong["values"]) == {"time_ms", "memory_kb"}


def test_a_compile_error_stops_the_run_and_skips_every_test(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    made.write_source("def broken(:\n")

    assert grade(made, docker, platform) == EXIT_OK

    result = platform.result()
    assert results.check(result) is None
    assert result["stopped"] == "compile_error"
    assert result["stopped_by"] == "compile"
    assert result["error"] is None
    assert rows(result) == {
        "main/1": "skipped",
        "main/2": "skipped",
        "main/10": "skipped",
    }
    assert all(row["values"] == {} for row in result["tests"])
    assert result["values"]["log"].startswith("main.py:1:")
    assert len(docker.order) == 1
    assert "Stopped at compile_error." in platform.logs[0].decode()


def test_a_system_error_part_way_keeps_the_rows_it_had(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    """main/2 failed at the run and main/1 got through every step before the
    check for main/10 broke: those two keep their outcomes and values, main/10
    is skipped, and the compile log gathered before stays.
    """
    made.write_source(
        "import sys\n"
        "data = sys.stdin.read()\n"
        "if data.strip() == '10 20 30':\n"
        "    raise SystemExit(3)\n"
        "print(sum(int(x) for x in data.split()))\n"
    )
    checks: list[FakeContainer] = []

    def on_create(step: str, container: FakeContainer) -> None:
        if step == "check":
            checks.append(container)
            if len(checks) == 2:
                container.does = Does(
                    before=writes('{"schema_version": 5, "error": "the disk broke"}'),
                    runs_fixture=False,
                )

    docker.on_create = on_create

    assert grade(made, docker, platform) == EXIT_OK

    result = platform.result()
    assert results.check(result) is None
    assert result["stopped"] == "system_error"
    assert result["stopped_by"] is None
    assert (
        "step check for test main/10 could not work: the disk broke"
        in (result["error"])
    )
    assert rows(result) == {
        "main/1": "accepted",
        "main/2": "runtime_error",
        "main/10": "skipped",
    }
    assert set(result["tests"][0]["values"]) == {"time_ms", "memory_kb"}
    assert set(result["tests"][1]["values"]) == {"time_ms", "memory_kb"}
    assert result["tests"][2]["values"] == {}
    assert result["values"] == {"log": "main.py: compiled for python"}


def test_a_test_over_its_time_limit_skips_its_check(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    made.write_source(
        "import sys, time\n"
        "data = sys.stdin.read()\n"
        "if data.strip() == '10 20 30':\n"
        "    time.sleep(30)\n"
        "print(sum(int(x) for x in data.split()))\n"
    )

    grade(made, docker, platform)

    result = platform.result()
    assert result["stopped"] is None
    assert rows(result) == {
        "main/1": "accepted",
        "main/2": "time_limit",
        "main/10": "accepted",
    }
    checks = [b for b in docker.bodies() if b["Labels"]["unicon.step"] == "check"]
    assert len(checks) == 2


def test_a_batch_leaves_out_the_tests_that_already_failed(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    made.write_plan(classic_plan(["main/1", "main/2", "main/10"], check_batch=True))
    made.write_source(
        "import sys\n"
        "data = sys.stdin.read()\n"
        "if data.strip() == '5':\n"
        "    raise SystemExit(3)\n"
        "print(sum(int(x) for x in data.split()))\n"
    )

    grade(made, docker, platform)

    result = platform.result()
    assert rows(result) == {
        "main/1": "accepted",
        "main/2": "accepted",
        "main/10": "runtime_error",
    }
    batch = inputs_of(made, "check")["batch"]
    assert [item["test"] for item in batch] == ["main/1", "main/2"]


def test_a_batch_shares_one_copy_of_an_input_every_item_names(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    grade(made, docker, platform)

    inputs = inputs_of(made, "run")
    binaries = {item["inputs"]["binary"]["file"] for item in inputs["batch"]}
    assert binaries == {"in/1/binary"}
    assert inputs["batch"][0] == {
        "test": "main/1",
        "inputs": {
            "binary": {"file": "in/1/binary"},
            "input": {"file": "in/2/input"},
            "time_limit": 2,
            "memory_limit": 64,
        },
    }


def test_a_file_given_to_a_folder_port_is_a_folder_holding_it(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    grade(made, docker, platform)

    inputs = inputs_of(made, "compile")["inputs"]
    assert inputs == {"source": {"folder": "in/1/source"}, "language": "python"}
    placed = step_dir(made, "compile") / "in" / "1" / "source"
    assert [p.name for p in placed.iterdir()] == ["main.py"]


def test_a_folder_is_placed_as_one_directory_with_its_layout(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    """A folder contestant input and a folder of the task, each with a folder
    inside it.
    """
    plan = classic_plan(list(made.tests))
    plan["contestant"]["submission"] = {"type": "folder"}
    plan["steps"][0]["inputs"] |= {
        "entry": {"value": "main.py"},
        "notes": {"task": "notes/"},
    }
    made.write_plan(plan)
    made.write_submission(
        {
            "submission": {
                "files": ["files/submission/main.py", "files/submission/lib/sum.py"]
            },
            "language": {"value": "python"},
        },
        {
            "files/submission/main.py": "import sys\n"
            "print(sum(int(x) for x in sys.stdin.read().split()))\n",
            "files/submission/lib/sum.py": "# a helper\n",
        },
    )
    (made.task / "notes" / "deep").mkdir(parents=True)
    (made.task / "notes" / "a.txt").write_text("a\n", encoding="utf-8")
    (made.task / "notes" / "deep" / "b.txt").write_text("b\n", encoding="utf-8")

    assert grade(made, docker, platform) == EXIT_OK

    assert platform.result()["stopped"] is None
    inputs = inputs_of(made, "compile")["inputs"]
    assert inputs["source"] == {"folder": "in/1/submission"}
    assert inputs["notes"] == {"folder": "in/2/notes"}
    work = step_dir(made, "compile")
    placed = sorted(
        p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file()
    )
    assert [p for p in placed if p.startswith("in/")] == [
        "in/1/submission/lib/sum.py",
        "in/1/submission/main.py",
        "in/2/notes/a.txt",
        "in/2/notes/deep/b.txt",
    ]


def test_a_template_is_filled_with_the_contestants_values_in_their_spelling(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    plan = classic_plan(list(made.tests))
    plan["contestant"] |= {"seed": {"type": "number"}, "verbose": {"type": "boolean"}}
    for item in plan["steps"][1]["batch"]:
        item["inputs"]["args"] = {
            "template": "--seed {0} --verbose {1} {{off}}",
            "parts": [{"submission": "seed"}, {"submission": "verbose"}],
        }
    made.write_plan(plan)
    made.write_submission(
        {
            "submission": {"files": ["files/submission/main.py"]},
            "language": {"value": "python"},
            "seed": {"value": 0},
            "verbose": {"value": True},
        },
        {"files/submission/main.py": "print(3)\n"},
    )
    document = made.submission / "submission.json"
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            '"seed": {"value": 0}', '"seed": {"value": 2.50}'
        ),
        encoding="utf-8",
    )

    grade(made, docker, platform)

    batch = inputs_of(made, "run")["batch"]
    assert {item["inputs"]["args"] for item in batch} == {
        "--seed 2.5 --verbose true {off}"
    }


def test_a_contestant_number_reaches_a_primitive_as_written(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    plan = classic_plan(list(made.tests))
    plan["contestant"]["seed"] = {"type": "number"}
    plan["steps"][0]["inputs"]["seed"] = {"submission": "seed"}
    made.write_plan(plan)
    made.write_submission(
        {
            "submission": {"files": ["files/submission/main.py"]},
            "language": {"value": "python"},
            "seed": {"value": 0},
        },
        {"files/submission/main.py": "print(3)\n"},
    )
    document = made.submission / "submission.json"
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            '"seed": {"value": 0}', '"seed": {"value": 0.10000000000000000001}'
        ),
        encoding="utf-8",
    )

    grade(made, docker, platform)

    text = (step_dir(made, "compile") / "inputs.json").read_text(encoding="utf-8")
    assert '"seed": 0.10000000000000000001' in text


def test_a_number_reaches_the_result_exactly_as_the_primitive_wrote_it(
    judged: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    docker.does(
        "judge",
        Does(
            before=writes(
                '{"schema_version": 5, "outputs": '
                '{"fraction": 0.1, "outcome": "accepted"}}'
            ),
            runs_fixture=False,
        ),
    )

    assert grade(judged, docker, platform) == EXIT_OK

    result = platform.result()
    assert result["stopped"] is None
    assert [row["values"]["fraction"] for row in result["tests"]] == [
        Decimal("0.1")
    ] * 3
    assert b'"fraction": 0.1}' in platform.bodies[-1]
    assert b"0.1000000000000000055" not in platform.bodies[-1]


@pytest.mark.parametrize(
    ("written", "reported"),
    [
        ("1", b'"fraction": 1}'),
        ("1.0000000000000000000", b'"fraction": 1.0000000000000000000}'),
    ],
)
def test_a_number_on_its_bound_is_inside_it(
    judged: Checkouts,
    docker: FakeDocker,
    platform: Platform,
    written: str,
    reported: bytes,
) -> None:
    docker.does(
        "judge",
        Does(
            before=writes(
                '{"schema_version": 5, "outputs": '
                f'{{"fraction": {written}, "outcome": "accepted"}}}}'
            ),
            runs_fixture=False,
        ),
    )

    grade(judged, docker, platform)

    assert platform.result()["stopped"] is None
    assert reported in platform.bodies[-1]


@pytest.mark.parametrize(
    ("written", "found"),
    [
        ("1.0000000000000000001", "is 1.0000000000000000001, above its at_most of 1"),
        ("-0.0000000000000000001", "below its at_least of 0"),
        ("Infinity", "is Infinity, not a finite number"),
        ("NaN", "is NaN, not a finite number"),
        ('"0.5"', "is text, not a number"),
    ],
)
def test_a_reported_number_out_of_bounds_or_not_finite_is_a_system_error(
    judged: Checkouts,
    docker: FakeDocker,
    platform: Platform,
    written: str,
    found: str,
) -> None:
    docker.does(
        "judge",
        Does(
            before=writes(
                '{"schema_version": 5, "outputs": '
                f'{{"fraction": {written}, "outcome": "accepted"}}}}'
            ),
            runs_fixture=False,
        ),
    )

    assert grade(judged, docker, platform) == EXIT_OK

    result = platform.result()
    assert results.check(result) is None
    assert result["stopped"] == "system_error"
    assert found in result["error"]
    assert rows(result) == {
        "main/1": "skipped",
        "main/2": "skipped",
        "main/10": "skipped",
    }


def test_a_secret_is_written_for_the_step_and_masked_everywhere_else(
    judged: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    def judge(work: Path) -> None:
        document = json.loads((work / "inputs.json").read_text(encoding="utf-8"))
        key = document["inputs"]["api_key"]
        outputs = {"note": f"called with {key}", "outcome": "accepted"}
        (work / "outputs.json").write_text(
            json.dumps({"schema_version": 5, "outputs": outputs}), encoding="utf-8"
        )

    docker.does(
        "judge",
        Does(before=judge, runs_fixture=False, printed=f"my key is {SECRET}"),
    )
    log = RunLog(echo=False)

    assert grade(judged, docker, platform, log=log) == EXIT_OK

    result = platform.result()
    assert [row["values"]["note"] for row in result["tests"]] == ["called with ***"] * 3
    inputs = inputs_of(judged, "judge")["inputs"]
    assert inputs["api_key"] == SECRET
    assert inputs["strict"] is True
    assert all(SECRET.encode() not in body for body in platform.bodies)
    assert SECRET not in platform.logs[0].decode()
    assert SECRET not in log.staff_text()
    assert "my key is <redacted>" in log.staff_text()


def test_a_secret_in_a_fault_is_masked_in_the_error(
    judged: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    docker.does(
        "judge",
        Does(
            before=writes(
                f'{{"schema_version": 5, "error": "the key {SECRET} was refused"}}'
            ),
            runs_fixture=False,
        ),
    )

    grade(judged, docker, platform)

    assert platform.result()["error"].endswith(
        "could not work: the key *** was refused"
    )


def test_a_port_wired_to_an_optional_output_not_written_is_left_out(
    judged: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    """The judge accepts without its optional note, so the explain step after
    it gets no note port at all.
    """
    plan = judged_plan(list(judged.tests))
    plan["steps"] += [
        {
            "id": "explain",
            "primitive": "acme/explain@v1",
            "image": JUDGE,
            "network": False,
            "limits": limits(),
            "outputs": {"outcome": "outcome"},
            "test": test,
            "inputs": {
                "note": {"step": "judge", "output": "note"},
                "fraction": {"step": "judge", "output": "fraction"},
            },
        }
        for test in judged.tests
    ]
    judged.write_plan(plan)
    docker.does(
        "judge",
        Does(
            before=writes(
                '{"schema_version": 5, "outputs": '
                '{"fraction": 1, "outcome": "accepted"}}'
            ),
            runs_fixture=False,
        ),
    )
    docker.does(
        "explain",
        Does(
            before=writes('{"schema_version": 5, "outputs": {"outcome": "accepted"}}'),
            runs_fixture=False,
        ),
    )
    log = RunLog(echo=False)

    assert grade(judged, docker, platform, log=log) == EXIT_OK

    result = platform.result()
    assert result["stopped"] is None
    assert set(rows(result).values()) == {"accepted"}
    assert inputs_of(judged, "explain")["inputs"] == {"fraction": 1}
    assert (
        "input note of step explain for test main/1 is left out: step judge did not "
        "write its optional output note"
    ) in log.staff_text()


def test_a_folder_of_the_task_given_to_a_folder_port_keeps_its_layout(
    judged: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    grade(judged, docker, platform)

    inputs = inputs_of(judged, "judge")["inputs"]
    assert inputs["rubric"] == {"folder": "in/2/rubric"}
    assert inputs["answer"] == {"file": "in/1/output"}
    work = step_dir(judged, "judge")
    assert (work / "in" / "2" / "rubric" / "rubric.md").read_text() == "be right\n"
    assert (work / "in" / "2" / "rubric" / "levels" / "1.md").is_file()


def test_a_test_without_its_per_test_file_is_skipped_before_any_step(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    """An output-only task: the contestant gives one answer file per test, by
    the test's id, with or without an ending, and leaves main/2 out.
    """
    plan = classic_plan(list(made.tests))
    plan["contestant"] = {"answers": {"type": "file", "per_test": True}}
    checks = plan["steps"][2:]
    for check in checks:
        check["inputs"]["actual"] = {"submission": "answers"}
    plan["steps"] = checks
    plan["report"] = {}
    made.write_plan(plan)
    made.write_submission(
        {"answers": {"files": ["files/answers/main/1.txt", "files/answers/main/10"]}},
        {"files/answers/main/1.txt": "3\n", "files/answers/main/10": "5\n"},
    )

    assert grade(made, docker, platform) == EXIT_OK

    result = platform.result()
    assert result["stopped"] is None
    assert rows(result) == {
        "main/1": "accepted",
        "main/2": "skipped",
        "main/10": "accepted",
    }
    assert len(docker.order) == 2
    assert inputs_of(made, "check")["inputs"]["actual"] == {"file": "in/1/1.txt"}
    assert "test main/2: skipped, input answers has no file for it" in (
        platform.logs[0].decode()
    )


@pytest.mark.parametrize(
    ("change", "found"),
    [
        (lambda step: step.update(network=True), "gives no network to a step"),
        (lambda step: step["limits"].update(gpus=1), "machine has no GPUs"),
    ],
)
def test_a_step_that_asks_for_the_network_or_a_gpu_is_refused(
    made: Checkouts,
    docker: FakeDocker,
    platform: Platform,
    change: Callable[[dict[str, Any]], None],
    found: str,
) -> None:
    plan = classic_plan(list(made.tests))
    change(plan["steps"][1])
    made.write_plan(plan)

    grade(made, docker, platform)

    result = platform.result()
    assert result["stopped"] == "system_error"
    assert "step run asks for" in result["error"] and found in result["error"]
    assert [b["Labels"]["unicon.step"] for b in docker.bodies()] == ["compile"]
    assert result["values"] == {"log": "main.py: compiled for python"}


def test_every_step_container_carries_the_whole_sandbox_set(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    grade(made, docker, platform)

    assert len(docker.bodies()) == 5
    for body in docker.bodies():
        host = body["HostConfig"]
        assert host["NetworkMode"] == "none"
        assert host["ReadonlyRootfs"] is True
        assert host["CapDrop"] == ["ALL"]
        assert host["SecurityOpt"] == ["no-new-privileges", "seccomp=builtin"]
        assert host["Memory"] == host["MemorySwap"] > 0
        assert host["PidsLimit"] == 64
        assert host["NanoCpus"] == 1_000_000_000
        assert {u["Name"] for u in host["Ulimits"]} == {"cpu", "fsize"}
        assert body["User"] != "0" and not body["User"].startswith("0:")
        assert (
            body["Labels"]["unicon.grading"] == "0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31"
        )
        work, tmp = host["Mounts"]
        assert work["Source"] == "wp_01j9example_default"
        assert work["Target"] == "/work"
        assert work["VolumeOptions"]["Subpath"].startswith("unicon-steps/")
        assert work["VolumeOptions"]["NoCopy"] is True
        assert tmp["Type"] == "tmpfs" and tmp["Target"] == "/tmp"
    run = docker.bodies()[1]
    # The step's 30 s and the container's start allowance of 10 s.
    assert run["Labels"]["unicon.time_ms"] == "40000"


def test_every_container_is_removed(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    grade(made, docker, platform)

    assert docker.containers
    assert all(c.removed for c in docker.containers.values())


def test_a_primitive_error_is_a_system_error_not_a_grade(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    made.write_source("# fixture: error\n")

    assert grade(made, docker, platform) == EXIT_OK

    result = platform.result()
    assert results.check(result) is None
    assert result["stopped"] == "system_error"
    assert set(rows(result).values()) == {"skipped"}
    assert result["values"] == {}
    assert "the fixture was asked to fail" in result["error"]


def test_a_step_that_writes_no_outputs_is_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    made.write_source("# fixture: no-outputs\n")

    grade(made, docker, platform)

    assert platform.result()["stopped"] == "system_error"
    assert "wrote no outputs.json" in platform.result()["error"]


def test_outputs_that_break_the_contract_stop_before_the_next_step(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    made.write_source("# fixture: bad-outputs\n")

    grade(made, docker, platform)

    result = platform.result()
    assert result["stopped"] == "system_error"
    assert "primitive.schema.json" in result["error"]
    assert len(docker.order) == 1


def _outputs(**outputs: Any) -> dict[str, Any]:
    return {"schema_version": 5, "outputs": outputs}


@pytest.mark.parametrize(
    ("outputs", "found"),
    [
        (_outputs(outcome="great"), "not one a step may"),
        (_outputs(outcome="skipped"), "not one a step may"),
        (_outputs(outcome="system_error"), "not one a step may"),
        (_outputs(compile_log="hi"), "returned no outcome"),
        (
            _outputs(compile_log="ok", outcome="accepted"),
            "returned accepted without its output binary",
        ),
        (
            _outputs(binary={"file": "out/b"}, compile_log=3, outcome="accepted"),
            "output compile_log of step compile is a number, not text",
        ),
        (
            _outputs(binary={"folder": "out/b"}, compile_log="", outcome="accepted"),
            "output binary of step compile is a folder, not a file",
        ),
        (
            _outputs(extra="x", outcome="compile_error"),
            "wrote output extra, which its primitive does not declare",
        ),
        ({"schema_version": 4, "outputs": {}}, "schema_version 5"),
        (_outputs(binary={"file": "in/1/main.py"}), "not under out/"),
        (_outputs(binary={"file": "out/missing"}), "does not exist"),
        ({"schema_version": 5, "batch": []}, "is not one"),
    ],
)
def test_what_a_step_wrote_is_checked(
    made: Checkouts,
    docker: FakeDocker,
    platform: Platform,
    outputs: dict[str, Any],
    found: str,
) -> None:
    def write(work: Path) -> None:
        (work / "out" / "b").write_text("binary", encoding="utf-8")
        (work / "outputs.json").write_text(json.dumps(outputs), encoding="utf-8")

    docker.does("compile", Does(before=write, runs_fixture=False))

    grade(made, docker, platform)

    result = platform.result()
    assert result["stopped"] == "system_error"
    assert found in result["error"]


def test_a_failed_step_need_not_write_its_other_outputs(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    docker.does(
        "compile",
        Does(
            before=writes(json.dumps(_outputs(outcome="compile_error"))),
            runs_fixture=False,
        ),
    )

    grade(made, docker, platform)

    result = platform.result()
    assert result["stopped"] == "compile_error"
    assert result["values"] == {}


def test_a_folder_output_reaches_the_next_step_with_its_layout(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    plan = classic_plan(list(made.tests))
    plan["steps"][0]["outputs"]["binary"] = "folder"
    made.write_plan(plan)

    def build(work: Path) -> None:
        (work / "out" / "build" / "lib").mkdir(parents=True)
        (work / "out" / "build" / "main").write_text("m", encoding="utf-8")
        (work / "out" / "build" / "lib" / "x").write_text("x", encoding="utf-8")
        outputs = _outputs(
            binary={"folder": "out/build"}, compile_log="", outcome="accepted"
        )
        (work / "outputs.json").write_text(json.dumps(outputs), encoding="utf-8")

    docker.does("compile", Does(before=build, runs_fixture=False))
    docker.does(
        "run",
        Does(
            before=writes('{"schema_version": 5, "error": "stop"}'), runs_fixture=False
        ),
    )

    grade(made, docker, platform)

    batch = inputs_of(made, "run")["batch"]
    assert batch[0]["inputs"]["binary"] == {"folder": "in/1/build"}
    placed = step_dir(made, "run") / "in" / "1" / "build"
    assert (placed / "main").read_text() == "m"
    assert (placed / "lib" / "x").read_text() == "x"


def test_outputs_over_the_step_output_limit_are_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    def write(work: Path) -> None:
        (work / "out" / "big").write_bytes(b"x" * (9 * 1024 * 1024))
        (work / "outputs.json").write_text(
            json.dumps(_outputs(outcome="compile_error")), encoding="utf-8"
        )

    docker.does("compile", Does(before=write, runs_fixture=False))

    grade(made, docker, platform)

    assert "more than 8 MB" in platform.result()["error"]


def test_a_run_once_step_past_its_time_limit_is_killed_and_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    docker.does("compile", Does(hangs=True))

    grade(made, docker, platform)

    result = platform.result()
    assert result["stopped"] == "system_error"
    assert "time limit of 10000 ms" in result["error"]
    assert any(call.startswith("kill") for call in docker.calls)
    assert all(c.removed for c in docker.containers.values())


@pytest.mark.parametrize(
    ("does", "found"),
    [
        (
            Does(hangs=True),
            "step check for test main/2 was killed at its time limit of 10000 ms",
        ),
        (
            Does(exit_code=137, out_of_memory=True, runs_fixture=False),
            "step check for test main/2 was killed at its memory limit of 256 MB",
        ),
    ],
)
def test_a_single_test_step_killed_at_its_limits_is_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform, does: Does, found: str
) -> None:
    """The primitive keeps the contestant's program within the test's limits,
    so a check that outran its container's own is a fault, not the test's.
    """
    checks: list[FakeContainer] = []

    def on_create(step: str, container: FakeContainer) -> None:
        if step == "check":
            checks.append(container)
            if len(checks) == 2:
                container.does = does

    docker.on_create = on_create

    grade(made, docker, platform)

    result = platform.result()
    assert result["stopped"] == "system_error"
    assert result["stopped_by"] is None
    assert found in result["error"]
    assert rows(result) == {
        "main/1": "accepted",
        "main/2": "skipped",
        "main/10": "skipped",
    }


def test_a_batch_killed_at_its_memory_limit_is_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    docker.does("run", Does(exit_code=137, out_of_memory=True, runs_fixture=False))

    grade(made, docker, platform)

    result = platform.result()
    assert result["stopped"] == "system_error"
    assert "step run was killed at its memory limit of 256 MB" in result["error"]


def test_valid_outputs_count_even_when_docker_saw_an_oom_kill_inside(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    """The kernel kills the largest process at the memory limit, usually the
    contestant's program under sandbox-run, and Docker then marks the whole
    container. The primitive survived and said what happened, so its word
    stands.
    """
    docker.does("run", Does(out_of_memory=True))

    grade(made, docker, platform)

    assert set(rows(platform.result()).values()) == {"accepted"}


def test_the_run_wall_clock_ends_the_run_as_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    docker.does("compile", Does(hangs=True))
    envelope_wall = 1

    grade(made, docker, platform, wall_seconds=envelope_wall)

    assert "wall clock" in platform.result()["error"]


def test_a_refused_create_is_a_system_error_naming_the_reason(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    docker.refuse_create = "unicon socket filter: image is not on the machine's list"

    grade(made, docker, platform)

    result = platform.result()
    assert result["stopped"] == "system_error"
    assert "image is not on the machine's list" in result["error"]


def test_a_plan_that_is_missing_is_a_system_error_with_no_rows(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    (made.task / "plans" / "plan.json").unlink()

    grade(made, docker, platform)

    result = platform.result()
    assert results.check(result) is None
    assert result["stopped"] == "system_error"
    assert "plans/plan.json does not exist" in result["error"]
    assert result["tests"] == [] and result["values"] == {}
    assert docker.order == []


def test_a_plan_of_another_version_is_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    plan = classic_plan(["main/1"])
    plan["schema_version"] = 4
    made.write_plan(plan)

    grade(made, docker, platform)

    assert "schema_version 4" in platform.result()["error"]


def test_a_missing_submission_file_is_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    (made.submission / "files" / "submission" / "main.py").unlink()

    grade(made, docker, platform)

    result = platform.result()
    assert (
        "submission file files/submission/main.py does not exist" in (result["error"])
    )
    assert set(rows(result).values()) == {"skipped"}


@pytest.mark.parametrize(
    ("inputs", "found"),
    [
        (
            {"submission": {"files": ["files/submission/main.py"]}},
            "has no input language, which the plan declares",
        ),
        (
            {
                "submission": {"files": ["files/submission/main.py"]},
                "language": {"value": "python"},
                "seed": {"value": 7},
            },
            "gives input seed, which the plan does not declare",
        ),
        (
            {
                "submission": {"files": ["files/submission/main.py"]},
                "language": {"files": ["files/language/x"]},
            },
            "gives files, and its type is enum",
        ),
        (
            {"submission": {"value": "main.py"}, "language": {"value": "python"}},
            "gives a value, and its type is file",
        ),
        (
            {
                "submission": {"files": ["files/submission/main.py"]},
                "language": {"value": "rust"},
            },
            "is 'rust', which is not one of its options",
        ),
        (
            {
                "submission": {
                    "files": ["files/submission/main.py", "files/submission/x.py"]
                },
                "language": {"value": "python"},
            },
            "gives 2 files, and it is one file",
        ),
    ],
)
def test_a_submission_that_does_not_match_the_plan_is_a_system_error(
    made: Checkouts,
    docker: FakeDocker,
    platform: Platform,
    inputs: dict[str, Any],
    found: str,
) -> None:
    made.write_submission(
        inputs,
        {
            "files/submission/x.py": "",
            "files/language/x": "",
        },
    )

    grade(made, docker, platform)

    result = platform.result()
    assert result["stopped"] == "system_error"
    assert found in result["error"]
    assert docker.order == []


def answers_plan(tests: list[str]) -> dict[str, Any]:
    """The classic plan with a per-test answers input every check reads."""
    plan = classic_plan(tests)
    plan["contestant"]["answers"] = {"type": "file", "per_test": True}
    for check in plan["steps"][2:]:
        check["inputs"]["expected"] = {"submission": "answers"}
    return plan


def test_a_per_test_file_for_a_test_the_plan_does_not_have_is_not_read(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    """A rejudge against a plan that dropped main/3: its file is noted for
    staff and the tests the plan has are graded.
    """
    made.write_plan(answers_plan(list(made.tests)))
    answers = {
        f"files/answers/{test}.txt": answer for test, (_, answer) in made.tests.items()
    }
    made.write_submission(
        {
            "submission": {"files": ["files/submission/main.py"]},
            "language": {"value": "python"},
            "answers": {"files": [*answers, "files/answers/main/3.txt"]},
        },
        answers | {"files/answers/main/3.txt": "3\n"},
    )
    log = RunLog(echo=False)

    assert grade(made, docker, platform, log=log) == EXIT_OK

    result = platform.result()
    assert result["stopped"] is None
    assert set(rows(result).values()) == {"accepted"}
    assert (
        "input answers of submission.json gives a file for test main/3, which the "
        "plan does not have; it is not read"
    ) in log.staff_text()
    assert "main/3" not in platform.logs[0].decode()


@pytest.mark.parametrize(
    ("files", "found"),
    [
        (["main/1."], "gives main/1., which is not named <group>/<test>"),
        (["main"], "gives main, which is not named <group>/<test>"),
        (["main/1/x"], "gives main/1/x, which is not named <group>/<test>"),
        (["main/1", "main/1.txt"], "gives more than one file for test main/1"),
        (["main/3", "main/3.txt"], "gives more than one file for test main/3"),
    ],
)
def test_a_malformed_per_test_file_or_a_second_one_is_a_system_error(
    made: Checkouts,
    docker: FakeDocker,
    platform: Platform,
    files: list[str],
    found: str,
) -> None:
    made.write_plan(answers_plan(list(made.tests)))
    made.write_submission(
        {
            "submission": {"files": ["files/submission/main.py"]},
            "language": {"value": "python"},
            "answers": {"files": [f"files/answers/{name}" for name in files]},
        },
        {f"files/answers/{name}": "3\n" for name in files},
    )

    grade(made, docker, platform)

    result = platform.result()
    assert result["stopped"] == "system_error"
    assert found in result["error"]
    assert docker.order == []


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
def test_a_task_file_behind_a_symbolic_link_is_refused(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    target = made.task / "tests" / "main" / "1" / "input"
    target.unlink()
    target.symlink_to("/etc/hostname")

    grade(made, docker, platform)

    assert "symbolic link" in platform.result()["error"]


def test_a_run_the_forge_refuses_at_started_grades_nothing(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    platform.answer_callbacks(409)

    assert grade(made, docker, platform) == EXIT_NOT_DELIVERED
    assert docker.order == []
    assert platform.events() == ["started"]


def test_the_result_is_retried_until_the_forge_takes_it(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    platform.answer_finished(503, 502, 200)

    assert grade(made, docker, platform) == EXIT_OK
    assert platform.events().count("finished") == 3


def test_a_result_the_forge_rejects_is_not_delivered(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    platform.answer_finished(422)

    assert grade(made, docker, platform) == EXIT_NOT_DELIVERED
    assert platform.events().count("finished") == 1


def test_a_log_that_cannot_be_put_leaves_the_log_pointer_null(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    platform.answer_log(403)

    assert grade(made, docker, platform) == EXIT_OK
    assert platform.result()["run_log"] is None
    assert len(platform.logs) == 3
