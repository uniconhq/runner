"""A whole grading run: the plan run against the fake Docker, the callbacks and
the log against the platform's routes, and the verdict that comes out.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support import (
    SCORE,
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
from unicon_harness import verdict as verdicts
from unicon_harness.main import EXIT_NOT_DELIVERED, EXIT_OK, grade_and_report
from unicon_harness.report import Reporter
from unicon_harness.runlog import RunLog

SLEEPER = "import time\ntime.sleep(60)\n"


def grade(
    made: Checkouts,
    docker: FakeDocker,
    platform: Platform,
    wall_seconds: int = 900,
) -> int:
    envelope = platform.envelope_for(made, wall_seconds)
    log = RunLog(echo=False)
    log.hide(envelope["callback"]["token"])
    log.hide("X-Amz-Signature=log-secret")
    with httpx.Client(timeout=5) as client:
        reporter = Reporter(envelope, log, client=client, sleep=lambda _: None)
        return grade_and_report(envelope, log, reporter, lambda: docker, mountinfo)


def rows(verdict: dict[str, Any]) -> dict[str, str]:
    return {row["id"]: row["outcome"] for row in verdict["tests"]}


def test_a_correct_submission_is_accepted_on_every_test(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    assert grade(made, docker, platform) == EXIT_OK

    verdict = platform.verdict()
    assert verdicts.check(verdict) is None
    assert verdict["outcome"] == "accepted"
    assert rows(verdict) == {"1": "accepted", "2": "accepted", "10": "accepted"}
    assert [row["id"] for row in verdict["tests"]] == ["1", "2", "10"]
    assert verdict["metrics"] == {"points": 3}
    assert all(row["metrics"] == {"points": 1} for row in verdict["tests"])
    assert all(row["memory_kb"] == 1024 for row in verdict["tests"])
    assert verdict["summary"] == "main.py: compiled for python"
    assert verdict["grading_id"] == "0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31"


def test_the_run_reports_started_progress_and_the_verdict_with_the_token(
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


def test_the_log_is_put_once_and_carries_no_credential(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    grade(made, docker, platform)

    assert len(platform.logs) == 1
    text = platform.logs[0].decode()
    assert "example-one-run-callback-token" not in text
    assert "log-secret" not in text
    assert platform.verdict()["log"] == f"{platform.url}/log"


def test_the_contestants_log_says_what_ran_and_how_without_the_machine(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    """Each step by its id and primitive, each test's outcome and the verdict;
    no image, volume, container, exit code or path of the machine.
    """
    (made.task / "data" / "testcases" / "2.ans").write_text("61\n", encoding="utf-8")
    docker.does("run", Does(printed="the primitive's own chatter"))

    grade(made, docker, platform)

    text = platform.logs[0].decode()
    lines = [line.split("] ", 1)[1] for line in text.splitlines() if "] " in line]
    assert lines[0] == "Grading submission/3, stage default, attempt 1."
    assert "compile (compile@v1): accepted" in lines
    assert "run (sandbox-run@v1): 3 tests" in lines
    assert "  test 2: accepted" in lines
    assert "check for test 2 (diff-check@v1): wrong_answer" in lines
    assert "Verdict: wrong_answer." in lines
    assert "  | main.py: compiled for python" in text
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


def test_a_system_error_tells_the_contestant_only_that_there_was_one(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    docker.refuse_create = "unicon socket filter: image is not on the machine's list"

    grade(made, docker, platform)

    text = platform.logs[0].decode()
    assert "The run could not be graded because of a system error." in text
    assert "socket filter" not in text and "machine's list" not in text
    assert "Verdict" not in text


def test_a_wrong_answer_is_the_first_failing_test_and_scores_the_rest(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    (made.task / "data" / "testcases" / "2.ans").write_text("61\n", encoding="utf-8")

    assert grade(made, docker, platform) == EXIT_OK

    verdict = platform.verdict()
    assert verdict["outcome"] == "wrong_answer"
    assert rows(verdict) == {"1": "accepted", "2": "wrong_answer", "10": "accepted"}
    assert verdict["metrics"] == {"points": 2}


def test_a_compile_error_stops_the_run_and_skips_every_test(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    made.write_source("def broken(:\n")

    assert grade(made, docker, platform) == EXIT_OK

    verdict = platform.verdict()
    assert verdicts.check(verdict) is None
    assert verdict["outcome"] == "compile_error"
    assert rows(verdict) == {"1": "skipped", "2": "skipped", "10": "skipped"}
    assert all(
        row["time_ms"] == 0 and row["metrics"] == {"points": 0}
        for row in verdict["tests"]
    )
    assert verdict["metrics"] == {"points": 0}
    assert verdict["summary"].startswith("main.py:1:")
    assert len(docker.order) == 1


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

    verdict = platform.verdict()
    assert verdict["outcome"] == "time_limit"
    assert rows(verdict) == {"1": "accepted", "2": "time_limit", "10": "accepted"}
    assert verdict["metrics"] == {"points": 2}
    checks = [b for b in docker.bodies() if b["Labels"]["unicon.step"] == "check"]
    assert len(checks) == 2


def test_a_batch_leaves_out_the_tests_that_already_failed(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    made.write_plan(classic_plan(["1", "2", "10"], check_batch=True))
    made.write_source(
        "import sys\n"
        "data = sys.stdin.read()\n"
        "if data.strip() == '5':\n"
        "    raise SystemExit(3)\n"
        "print(sum(int(x) for x in data.split()))\n"
    )

    grade(made, docker, platform)

    verdict = platform.verdict()
    assert rows(verdict) == {"1": "accepted", "2": "accepted", "10": "runtime_error"}
    check_dir = next(
        p for p in (made.root / "unicon-steps").iterdir() if p.name.endswith("check")
    )
    inputs = json.loads((check_dir / "inputs.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in inputs["batch"]] == ["1", "2"]


def test_a_batch_shares_one_copy_of_an_input_every_item_names(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    grade(made, docker, platform)

    run_dir = next(
        p for p in (made.root / "unicon-steps").iterdir() if p.name.endswith("run")
    )
    inputs = json.loads((run_dir / "inputs.json").read_text(encoding="utf-8"))
    binaries = {item["inputs"]["binary"]["file"] for item in inputs["batch"]}
    assert binaries == {"in/1/binary"}
    assert inputs["batch"][0]["inputs"]["input"] == {"file": "in/2/1.in"}
    assert inputs["batch"][0]["inputs"]["time_limit"] == 2.0


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
    assert run["Labels"]["unicon.time_ms"] == "30000"


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

    verdict = platform.verdict()
    assert verdicts.check(verdict) is None
    assert verdict["outcome"] == "system_error"
    assert verdict["tests"] == [] and verdict["metrics"] == {}
    assert "the fixture was asked to fail" in verdict["summary"]


def test_a_step_that_writes_no_outputs_is_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    made.write_source("# fixture: no-outputs\n")

    grade(made, docker, platform)

    assert platform.verdict()["outcome"] == "system_error"
    assert "wrote no outputs.json" in platform.verdict()["summary"]


def test_outputs_that_break_the_contract_stop_before_the_next_step(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    made.write_source("# fixture: bad-outputs\n")

    grade(made, docker, platform)

    verdict = platform.verdict()
    assert verdict["outcome"] == "system_error"
    assert "primitive.schema.json" in verdict["summary"]
    assert len(docker.order) == 1


@pytest.mark.parametrize(
    ("outputs", "found"),
    [
        ({"schema_version": 3, "outputs": {"outcome": "great"}}, "not one a step may"),
        (
            {"schema_version": 3, "outputs": {"outcome": "skipped"}},
            "not one a step may",
        ),
        ({"schema_version": 2, "outputs": {}}, "schema_version 3"),
        (
            {"schema_version": 3, "outputs": {"binary": {"file": "in/1/main.py"}}},
            "not under out/",
        ),
        (
            {"schema_version": 3, "outputs": {"binary": {"file": "out/missing"}}},
            "does not exist",
        ),
        ({"schema_version": 3, "batch": []}, "is not one"),
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
        (work / "outputs.json").write_text(json.dumps(outputs), encoding="utf-8")

    docker.does("compile", Does(before=write, runs_fixture=False))

    grade(made, docker, platform)

    verdict = platform.verdict()
    assert verdict["outcome"] == "system_error"
    assert found in verdict["summary"]


def test_outputs_over_the_step_output_limit_are_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    def write(work: Path) -> None:
        (work / "out" / "big").write_bytes(b"x" * (9 * 1024 * 1024))
        (work / "outputs.json").write_text(
            json.dumps({"schema_version": 3, "outputs": {"outcome": "accepted"}}),
            encoding="utf-8",
        )

    docker.does("compile", Does(before=write, runs_fixture=False))

    grade(made, docker, platform)

    assert "more than 8 MB" in platform.verdict()["summary"]


def test_a_run_once_step_past_its_time_limit_is_killed_and_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    docker.does("compile", Does(hangs=True))

    grade(made, docker, platform)

    verdict = platform.verdict()
    assert verdict["outcome"] == "system_error"
    assert "time limit of 10000 ms" in verdict["summary"]
    assert any(call.startswith("kill") for call in docker.calls)
    assert all(c.removed for c in docker.containers.values())


def test_a_single_test_step_killed_at_its_limits_gives_that_test_the_limit(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    checks: list[FakeContainer] = []

    def on_create(step: str, container: FakeContainer) -> None:
        if step != "check":
            return
        checks.append(container)
        if len(checks) == 2:
            container.does = Does(hangs=True)
        if len(checks) == 3:
            container.does = Does(exit_code=137, out_of_memory=True, runs_fixture=False)

    docker.on_create = on_create

    grade(made, docker, platform)

    verdict = platform.verdict()
    assert rows(verdict) == {"1": "accepted", "2": "time_limit", "10": "memory_limit"}
    assert verdict["outcome"] == "time_limit"


def test_a_batch_killed_at_its_memory_limit_is_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    docker.does("run", Does(exit_code=137, out_of_memory=True, runs_fixture=False))

    grade(made, docker, platform)

    verdict = platform.verdict()
    assert verdict["outcome"] == "system_error"
    assert "batch of step run was killed at its memory limit" in verdict["summary"]


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

    assert platform.verdict()["outcome"] == "accepted"


def test_the_run_wall_clock_ends_the_run_as_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    docker.does("compile", Does(hangs=True))
    envelope_wall = 1

    grade(made, docker, platform, wall_seconds=envelope_wall)

    assert "wall clock" in platform.verdict()["summary"]


def test_a_refused_create_is_a_system_error_naming_the_reason(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    docker.refuse_create = "unicon socket filter: image is not on the machine's list"

    grade(made, docker, platform)

    verdict = platform.verdict()
    assert verdict["outcome"] == "system_error"
    assert "image is not on the machine's list" in verdict["summary"]


def test_a_per_test_outcome_of_another_name_decides_each_test(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    """verdict.outcome may name a per-test output that is not called outcome;
    each test's row then reads it.
    """
    plan = classic_plan(["1", "2", "10"], check_batch=True)
    plan["verdict"]["outcome"] = {"step": "check", "output": "judged"}
    made.write_plan(plan)

    def judge(work: Path) -> None:
        document = json.loads((work / "inputs.json").read_text(encoding="utf-8"))
        batch = [
            {
                "id": item["id"],
                "outputs": {
                    "judged": "wrong_answer" if item["id"] == "2" else "accepted",
                    "points": 1,
                },
            }
            for item in document["batch"]
        ]
        (work / "outputs.json").write_text(
            json.dumps({"schema_version": 3, "batch": batch}), encoding="utf-8"
        )

    docker.does("check", Does(before=judge, runs_fixture=False))

    assert grade(made, docker, platform) == EXIT_OK

    verdict = platform.verdict()
    assert verdict["outcome"] == "wrong_answer"
    assert rows(verdict) == {"1": "accepted", "2": "wrong_answer", "10": "accepted"}


def test_a_per_test_outcome_of_another_name_must_be_an_outcome(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    plan = classic_plan(["1", "2", "10"])
    plan["verdict"]["outcome"] = {"step": "check", "output": "points"}
    made.write_plan(plan)

    grade(made, docker, platform)

    verdict = platform.verdict()
    assert verdict["outcome"] == "system_error"
    assert "not an outcome" in verdict["summary"]


def test_a_scorer_reads_a_per_test_output_over_every_test(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    plan = classic_plan(["1", "2", "10"])
    plan["steps"].append(
        {
            "id": "score",
            "primitive": "score@v1",
            "image": SCORE,
            "entrypoint": ["python", "/opt/fixture.py", "score"],
            "limits": limits(),
            "inputs": {"results": {"step": "check", "output": "outcome"}},
        }
    )
    plan["verdict"]["metrics"] = {"points": {"step": "score", "output": "points"}}
    made.write_plan(plan)
    (made.task / "data" / "testcases" / "2.ans").write_text("61\n", encoding="utf-8")

    grade(made, docker, platform)

    verdict = platform.verdict()
    assert verdict["metrics"] == {"points": 20}
    assert all(row["metrics"] == {} for row in verdict["tests"])
    score_dir = next(
        p for p in (made.root / "unicon-steps").iterdir() if p.name.endswith("score")
    )
    inputs = json.loads((score_dir / "inputs.json").read_text(encoding="utf-8"))
    assert inputs["inputs"]["results"] == ["accepted", "wrong_answer", "accepted"]


def test_a_plan_that_is_missing_is_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    (made.task / "plans" / "default.json").unlink()

    grade(made, docker, platform)

    verdict = platform.verdict()
    assert verdict["outcome"] == "system_error"
    assert "plans/default.json does not exist" in verdict["summary"]
    assert docker.order == []


def test_a_plan_of_another_version_is_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    plan = classic_plan(["1"])
    plan["schema_version"] = 2
    made.write_plan(plan)

    grade(made, docker, platform)

    assert "schema_version 2" in platform.verdict()["summary"]


def test_a_missing_submission_file_is_a_system_error(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    (made.submission / "files" / "submission" / "main.py").unlink()

    grade(made, docker, platform)

    assert (
        "submission file files/submission/main.py does not exist"
        in (platform.verdict()["summary"])
    )


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
def test_a_task_file_behind_a_symbolic_link_is_refused(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    target = made.task / "data" / "testcases" / "1.in"
    target.unlink()
    target.symlink_to("/etc/hostname")

    grade(made, docker, platform)

    assert "symbolic link" in platform.verdict()["summary"]


def test_a_run_the_forge_refuses_at_started_grades_nothing(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    platform.answer_callbacks(409)

    assert grade(made, docker, platform) == EXIT_NOT_DELIVERED
    assert docker.order == []
    assert platform.events() == ["started"]


def test_the_verdict_is_retried_until_the_forge_takes_it(
    made: Checkouts, docker: FakeDocker, platform: Platform
) -> None:
    platform.answer_finished(503, 502, 200)

    assert grade(made, docker, platform) == EXIT_OK
    assert platform.events().count("finished") == 3


def test_a_verdict_the_forge_rejects_is_not_delivered(
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
    assert platform.verdict()["log"] is None
    assert len(platform.logs) == 3
