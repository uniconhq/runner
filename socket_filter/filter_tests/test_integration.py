"""The socket filter on a real Docker, reproducing the prototype's checks
(tools/socket-policy-test in the proposal repo): a stand-in harness runs the
harness's own path through it, an escape probe as an approved step, and every
request the filter must refuse; a second run on the same filter cannot touch
the first's steps; and the reaper removes a step that outlives its clock.
Run with `pytest -m docker`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from filter_tests.lab import (
    FILTER_UID,
    PYTHON,
    Lab,
    docker,
    docker_ready,
    fixture_image,
    lab,
    remapped,
)

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_ready(), reason="needs a Docker daemon"),
]

STANDIN = (Path(__file__).parent / "standin.py").read_text(encoding="utf-8")


def _standin(
    made: Lab,
    what: str,
    grading: str,
    workspace: str,
    sockets: str,
    *args: str,
    detach: bool = False,
    writable_socket: bool = False,
    outside_remap: bool = False,
    **env: str,
) -> str:
    """A stand-in harness, with the filter's socket directory mounted read-only
    as the CI mounts it, or writable to show what the read-only mount
    prevents. `outside_remap` runs it as the filter runs, outside any
    user-namespace remap.
    """
    code = made.volume(f"code-{what}", {"standin.py": STANDIN})
    variables = [f"--env={k}={v}" for k, v in env.items()]
    return made.run(
        what,
        *(["--userns", "host"] if outside_remap else []),
        "-v",
        f"{workspace}:/woodpecker",
        "-v",
        f"{sockets}:/run/unicon{'' if writable_socket else ':ro'}",
        "-v",
        f"{code}:/code:ro",
        f"--env=UNICON_GRADING_ID={grading}",
        f"--env=IMAGE={fixture_image()}",
        f"--env=WORKSPACE={workspace}",
        *variables,
        PYTHON,
        "python",
        "/code/standin.py",
        *args,
        detach=detach,
    )


def _results(container: str) -> dict[str, dict[str, Any]]:
    lines = [
        json.loads(line)
        for line in docker("logs", container).splitlines()
        if line.startswith('{"check"')
    ]
    return {line["check"]: line for line in lines}


def test_the_prototype_checks_through_a_real_daemon() -> None:
    with lab() as made:
        first = made.grading()
        front, sockets = made.filter([fixture_image()])
        workspace = made.volume("ws")
        other = made.volume("other-ws")
        foreign = made.run("foreign", PYTHON, "sleep", "600")
        foreign_id = docker("inspect", foreign, "--format", "{{.Id}}").strip()
        standin = _standin(
            made,
            "standin",
            first,
            workspace,
            sockets,
            "checks",
            FOREIGN=foreign_id,
            OTHER_WORKSPACE=other,
            FILTER_DIRECTORY=sockets,
            SOCKET_OWNER=_filter_uid_in_a_container(),
        )
        results = _results(standin)
        filter_log = docker("logs", front)

    failed = {name: r["detail"] for name, r in results.items() if not r["passed"]}
    assert failed == {}, failed
    assert len(results) >= 70, sorted(results)
    probe = results["B the escape probe"]["detail"]
    assert probe["uid"] == 10001
    assert probe["cap_eff"] == "0000000000000000"
    assert probe["seccomp"] == "2" and probe["no_new_privs"] == "1"
    assert probe["user_namespace"] == "blocked"
    assert probe["docker_socket"] is False
    assert probe["network"] == "blocked" and probe["interfaces"] == ["lo"]
    assert probe["write_root"] == "Read-only file system"
    assert probe["write_work"] == "ok" and probe["write_tmp"] == "ok"
    flags = results["A5 the flags as the daemon keeps them"]["detail"]
    assert flags["SecurityOpt"] == ["no-new-privileges", "seccomp=builtin"]
    assert flags["MemorySwap"] == flags["Memory"]
    assert '"decision": "deny"' in filter_log


def test_one_run_cannot_reach_another_runs_steps() -> None:
    with lab() as made:
        first, second = made.grading(), made.grading()
        _, sockets = made.filter([fixture_image()])
        first = _standin(
            made,
            "first",
            first,
            made.volume("ws1"),
            sockets,
            "hold",
            "600000",
            detach=True,
        )
        held = _held(first)
        second = _standin(
            made, "second", second, made.volume("ws2"), sockets, "poke", held
        )
        results = _results(second)
        still_there = docker("inspect", held, "--format", "{{.State.Running}}").strip()

    assert {name: r["passed"] for name, r in results.items()} == {
        "X inspect another run's step": True,
        "X kill another run's step": True,
        "X remove another run's step": True,
    }
    assert still_there == "true"


def test_the_reaper_removes_a_step_that_outlives_its_clock() -> None:
    with lab() as made:
        first = made.grading()
        _, sockets = made.filter(
            [fixture_image()],
            "-e",
            "UNICON_FILTER_GRACE_SECONDS=1",
            "-e",
            "UNICON_FILTER_REAP_EVERY_SECONDS=1",
        )
        holder = _standin(
            made,
            "holder",
            first,
            made.volume("ws"),
            sockets,
            "hold",
            "1000",
            detach=True,
        )
        held = _held(holder)
        gone = _gone_within(held, 20)
        holder_running = docker(
            "inspect", holder, "--format", "{{.State.Running}}"
        ).strip()

    assert gone, "the step outlived its 1 s clock and the grace"
    assert holder_running == "true"


def test_the_reaper_removes_a_step_whose_harness_is_gone() -> None:
    with lab() as made:
        first = made.grading()
        _, sockets = made.filter(
            [fixture_image()], "-e", "UNICON_FILTER_REAP_EVERY_SECONDS=1"
        )
        holder = _standin(
            made,
            "holder",
            first,
            made.volume("ws"),
            sockets,
            "hold",
            "600000",
            detach=True,
        )
        held = _held(holder)
        docker("rm", "-f", holder)
        gone = _gone_within(held, 20)

    assert gone, "the step outlived its harness"


def test_the_filter_exits_when_its_socket_is_replaced() -> None:
    """A harness that could write the socket's directory, which the read-only
    mount keeps it from, replaces the socket with its own; the filter notices
    within its check interval and exits 3, for the machine to restart it. The
    harness here runs outside any user-namespace remap: a remapped one could
    not write the filter's directory even mounted writable, and would leave
    nothing for the filter to notice.
    """
    with lab() as made:
        front, sockets = made.filter(
            [fixture_image()], "-e", "UNICON_FILTER_SOCKET_CHECK_SECONDS=0.2"
        )
        _standin(
            made,
            "replacer",
            made.grading(),
            made.volume("ws"),
            sockets,
            "replace",
            detach=True,
            writable_socket=True,
            outside_remap=True,
        )
        code = made.wait(front, 60)
        filter_log = docker("logs", front)

    assert code == 3, filter_log
    assert "is not the one the filter bound" in filter_log


def _filter_uid_in_a_container() -> str:
    """The owner a harness sees on the filter's socket: the filter's uid, or
    under userns-remap the kernel's overflow uid, because the filter runs
    outside the remap and its uid is not mapped into the harness's.
    """
    if not remapped():
        return FILTER_UID
    return docker("run", "--rm", PYTHON, "cat", "/proc/sys/kernel/overflowuid").strip()


def _held(container: str) -> str:
    for _ in range(100):
        for line in docker("logs", container, check=False).splitlines():
            if line.startswith('{"held"'):
                return str(json.loads(line)["held"])
        time.sleep(0.2)
    raise AssertionError(docker("logs", container, check=False))


def _gone_within(container: str, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not docker("ps", "-aq", "--filter", f"id={container}").strip():
            return True
        time.sleep(0.5)
    docker("rm", "-f", container, check=False)
    return False
