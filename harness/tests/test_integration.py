"""A whole grading run on a real Docker, the way a grading machine runs one: the
harness image in a container with the run's workspace volume and the socket
filter's socket, the filter in front of the daemon, the fixture primitives as
step images by digest, and a stand-in platform on the harness's network.

The contestant program tries what a sandbox must stop: the network, memory
past the limit, a fork storm, and root. Run with `pytest -m docker`.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from filter_tests.lab import (
    Lab,
    docker,
    docker_ready,
    fixture_image,
    image,
    lab,
    labelled,
)
from tests.support import classic_plan, example

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_ready(), reason="needs a Docker daemon"),
]

TOKEN = "lab-one-run-token"

PROGRAM = """\
import os, socket, sys, time
data = sys.stdin.read().split()
kind = data[0]
if kind == "sum":
    print(int(data[1]) + int(data[2]))
elif kind == "net":
    try:
        socket.create_connection(("1.1.1.1", 80), timeout=2)
        print("open")
    except OSError:
        print("blocked")
elif kind == "mem":
    block = b"x" * (512 * 1024 * 1024)
    print(len(block))
elif kind == "fork":
    children, refused = [], False
    for _ in range(200):
        try:
            pid = os.fork()
        except OSError:
            refused = True
            break
        if pid == 0:
            time.sleep(5)
            os._exit(0)
        children.append(pid)
    for pid in children:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    print("blocked" if refused else "open")
elif kind == "sandbox":
    status = open("/proc/self/status").read()
    checks = {
        "not root": os.getuid() != 0,
        "no capabilities": "CapEff:\\t0000000000000000" in status,
        "no new privileges": "NoNewPrivs:\\t1" in status,
        "seccomp filter": "Seccomp:\\t2" in status,
    }
    try:
        open("/escape", "w")
        checks["read-only root"] = False
    except OSError:
        checks["read-only root"] = True
    try:
        os.unshare(os.CLONE_NEWUSER)
        checks["no user namespace"] = False
    except OSError:
        checks["no user namespace"] = True
    print("sandboxed" if all(checks.values()) else f"open {checks}")
"""

TESTS = {
    "main/1": ("sum 1 2\n", "3\n"),
    "main/2": ("sum 2 2\n", "5\n"),
    "sandbox/1": ("net\n", "blocked\n"),
    "sandbox/2": ("mem\n", "0\n"),
    "sandbox/3": ("fork\n", "blocked\n"),
    "sandbox/4": ("sandbox\n", "sandboxed\n"),
}


def _workspace(fixture: str) -> dict[str, bytes | str]:
    plan = classic_plan(list(TESTS), image=fixture, run_memory_mb=128)
    files: dict[str, bytes | str] = {"task/plans/plan.json": json.dumps(plan)}
    for test, (given, answer) in TESTS.items():
        files[f"task/tests/{test}/input"] = given
        files[f"task/tests/{test}/answer"] = answer
    files["submission/files/submission/main.py"] = PROGRAM
    files["submission/submission.json"] = json.dumps(
        {
            "schema_version": 5,
            "inputs": {
                "submission": {"files": ["files/submission/main.py"]},
                "language": {"value": "python"},
            },
        }
    )
    return files


def _envelope(grading: str) -> dict[str, Any]:
    envelope = example("envelope.json")
    envelope["grading_id"] = grading
    envelope["callback"] = {"url": "http://platform:8080/callback", "token": TOKEN}
    envelope["log_put"] = "http://platform:8080/log?X-Amz-Signature=lab-secret"
    envelope["deadline"] = "2999-01-01T00:00:00Z"
    return envelope


def _grade(made: Lab, grading: str) -> tuple[int, list[dict[str, Any]], str]:
    fixture = fixture_image()
    network = made.network("net")
    workspace = made.volume("ws", _workspace(fixture))
    data = made.volume("platform", {"envelope.json": json.dumps(_envelope(grading))})
    made.run(
        "platform",
        "--network",
        network,
        "--network-alias",
        "platform",
        "-v",
        f"{data}:/data:ro",
        "--entrypoint",
        "python",
        fixture,
        "/opt/platform_server.py",
        "8080",
    )
    front, sockets = made.filter([fixture])
    harness = made.run(
        "harness",
        "--network",
        network,
        "-v",
        f"{workspace}:/woodpecker",
        "-v",
        f"{sockets}:/run/unicon:ro",
        "-e",
        "DOCKER_HOST=unix:///run/unicon/docker.sock",
        "-e",
        f"UNICON_GRADING_ID={grading}",
        "-e",
        "UNICON_ENVELOPE_URL=http://platform:8080/envelope?key=lab-key",
        image("harness"),
    )
    code = made.wait(harness)
    received = [
        json.loads(line)
        for line in docker("logs", made.name("platform")).splitlines()
        if line.startswith("{")
    ]
    return code, received, docker("logs", front)


def test_a_real_run_grades_through_the_filter_and_reports_back() -> None:
    with lab() as made:
        grading = made.grading()
        code, received, filter_log = _grade(made, grading)
        harness_log = docker("logs", made.name("harness"))
        left = labelled(grading)

    assert code == 0, harness_log
    callbacks = [
        json.loads(base64.b64decode(r["body"]))
        for r in received
        if r["method"] == "POST"
    ]
    assert callbacks[0] == {"event": "started"}
    assert any(c["event"] == "progress" for c in callbacks)
    result = callbacks[-1]["result"]
    assert callbacks[-1]["event"] == "finished"
    assert {r["authorization"] for r in received if r["method"] == "POST"} == {
        f"Bearer {TOKEN}"
    }

    outcomes = {row["test"]: row["outcome"] for row in result["tests"]}
    assert outcomes == {
        "main/1": "accepted",
        "main/2": "wrong_answer",
        "sandbox/1": "accepted",
        "sandbox/2": "memory_limit",
        "sandbox/3": "accepted",
        "sandbox/4": "accepted",
    }, result
    assert result["stopped"] is None and result["error"] is None
    assert result["values"] == {"log": "main.py: compiled for python"}
    assert all("time_ms" in row["values"] for row in result["tests"])

    logs = [
        base64.b64decode(r["body"]).decode() for r in received if r["method"] == "PUT"
    ]
    assert len(logs) == 1
    assert TOKEN not in logs[0] and "lab-secret" not in logs[0]
    assert result["run_log"] == "http://platform:8080/log"
    assert "4 of 6 tests accepted." in logs[0]
    assert "sha256:" not in logs[0] and "unicon-lab-" not in logs[0]
    assert "sha256:" in harness_log and "unicon-lab-" in harness_log
    assert TOKEN not in harness_log and "lab-secret" not in harness_log

    assert '"decision": "deny"' not in filter_log
    assert filter_log.count('"path": "/v1.45/containers/create"') == 7
    assert left == []


def test_a_harness_without_the_filter_cannot_start_a_step() -> None:
    """Pointed at a socket nobody serves, the harness has no path to Docker and
    the run is a system error, never a grade.
    """
    with lab() as made:
        grading = made.grading()
        fixture = fixture_image()
        network = made.network("net")
        workspace = made.volume("ws", _workspace(fixture))
        data = made.volume(
            "platform", {"envelope.json": json.dumps(_envelope(grading))}
        )
        made.run(
            "platform",
            "--network",
            network,
            "--network-alias",
            "platform",
            "-v",
            f"{data}:/data:ro",
            "--entrypoint",
            "python",
            fixture,
            "/opt/platform_server.py",
            "8080",
        )
        harness = made.run(
            "harness",
            "--network",
            network,
            "-v",
            f"{workspace}:/woodpecker",
            "-e",
            "DOCKER_HOST=unix:///run/unicon/docker.sock",
            "-e",
            f"UNICON_GRADING_ID={grading}",
            "-e",
            "UNICON_ENVELOPE_URL=http://platform:8080/envelope",
            image("harness"),
        )
        code = made.wait(harness)
        received = [
            json.loads(line)
            for line in docker("logs", made.name("platform")).splitlines()
            if line.startswith("{")
        ]

    assert code == 0
    finished = json.loads(base64.b64decode(received[-1]["body"]))
    assert finished["result"]["stopped"] == "system_error"
    assert "own container" in finished["result"]["error"]
