"""The wrapper in its image, primitive-base, on a real Docker, against the
stand-in for the forge's door served on this machine and reached from the
container as host.docker.internal: a primitive reading the wrapper's
environment from /proc gets nothing, nor does a compile that includes it,
the org's cache volume works for a step that is not root, and exec, built on
the base image from a primitive-exec checkout beside this one, runs a batch
on isolate through the wrapper, in a privileged container as a grading
machine runs it. Run with `pytest -m docker`.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
import zipapp
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from filter_tests.lab import docker, docker_ready, image
from step_tests.stand_in import TOKEN, StandIn, stand_in

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_ready(), reason="needs a Docker daemon"),
]

SIBLINGS = Path(__file__).resolve().parents[3]
HOST = "host.docker.internal"
ACCEPTED = '{"schema_version": 6, "outputs": {"outcome": "accepted"}}'


@pytest.fixture
def server() -> Iterator[StandIn]:
    with stand_in("0.0.0.0", HOST) as state:
        yield state


@cache
def on_base(primitive: str) -> str:
    """The primitive's image built on this checkout's primitive-base, from
    its checkout beside this one."""
    context = SIBLINGS / f"primitive-{primitive}"
    if not (context / "Dockerfile").is_file():
        pytest.skip(f"no primitive-{primitive} checkout beside this one")
    tag = f"unicon-primitive-{primitive}:lab"
    base = f"BASE_IMAGE={image('primitive-base')}"
    docker("build", "-q", "--build-arg", base, "-t", tag, str(context))
    return tag


def a_file(server: StandIn, path: str, content: bytes, **entry: Any) -> dict[str, Any]:
    name = uuid.uuid4().hex
    server.files[name] = content
    return {
        "path": path,
        "url": server.link(name),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    } | entry


def step(
    server: StandIn,
    tag: str,
    inputs: dict[str, Any],
    files: list[dict[str, Any]],
    flags: list[str],
    command: list[str] | None = None,
    **job: Any,
) -> subprocess.CompletedProcess[str]:
    """Start the image as a Woodpecker plugin step with the stand-in's job."""
    server.job = {
        "schema_version": 1,
        "inputs": inputs,
        "files": files,
        "uploads": f"{server.door}/uploads",
        "done": f"{server.door}/done",
        "time_limit": 120,
        "fail_on_non_accepted": True,
    } | job
    run = ["docker", "run", "--rm", f"--add-host={HOST}:host-gateway"]
    run += ["--env", f"PLUGIN_DATA_URL={server.door}"]
    run += ["--env", f"PLUGIN_DATA_TOKEN={TOKEN}", *flags, tag, *(command or [])]
    return subprocess.run(run, capture_output=True, text=True, timeout=600)


PROBE = f"""
import json, os
seen = {{"own": dict(os.environ)}}
try:
    seen["environ"] = open("/proc/1/environ", "rb").read().decode("latin-1")
except OSError as error:
    seen["environ"] = type(error).__name__
status = open("/proc/1/status").read()
seen["peak_kb"] = int(status.split("VmHWM:")[1].split()[0])
os.makedirs("out", exist_ok=True)
json.dump(seen, open("out/seen.json", "w"))
open("outputs.json", "w").write({ACCEPTED!r})
"""


def test_a_primitive_does_not_see_the_wrappers_environment(server: StandIn) -> None:
    """A primitive that is not root, as compile is, reading the wrapper's
    /proc/1/environ is refused, its own environment holds neither the link
    nor the token, and the wrapper is small beside it."""
    probe = a_file(server, "in/probe.py", PROBE.encode())
    inputs = {"schema_version": 6, "inputs": {}}
    flags = ["--user=65532:65532", "--cap-drop=ALL"]
    command = ["python3", "/work/in/probe.py"]
    done = step(server, image("primitive-base"), inputs, [probe], flags, command)
    assert done.returncode == 0, done.stdout + done.stderr
    seen = json.loads(server.put["out/seen.json"])
    assert seen["environ"] == "PermissionError"
    assert not [name for name in seen["own"] if name.startswith("PLUGIN_")]
    assert TOKEN not in json.dumps(seen["own"])
    assert server.door not in json.dumps(seen["own"])
    print(f"the wrapper's peak resident memory: {seen['peak_kb']} kB")
    assert seen["peak_kb"] < 48 * 1024


@pytest.mark.parametrize("environ", ["/proc/1/environ", "/proc/self/environ"])
def test_a_compile_including_an_environment_gets_nothing(
    server: StandIn, environ: str
) -> None:
    """compile@v3 on the base image, given a source that includes the
    wrapper's environment or its own: the first is refused, a compile error
    saying so, and no file the step uploads, the program among them, holds
    the link or the token, nor needed masking."""
    source = f'#include "{environ}"\nint main(void) {{ return 0; }}\n'.encode()
    files = [a_file(server, "in/1/source/main.c", source)]
    inputs = {
        "schema_version": 6,
        "inputs": {"source": {"folder": "in/1/source"}, "language": "c"},
    }
    flags = ["--cap-drop=ALL", "--security-opt=no-new-privileges"]
    done = step(server, on_base("compile"), inputs, files, flags)
    if environ == "/proc/1/environ":
        assert done.returncode == 1, done.stdout + done.stderr
        written = json.loads(server.put["outputs.json"])
        assert written["outputs"]["outcome"] == "compile_error"
        assert "Permission denied" in json.dumps(written)
    assert server.put
    for content in [*server.put.values(), (done.stdout + done.stderr).encode()]:
        assert TOKEN.encode() not in content and b"PLUGIN_" not in content
        assert server.door.encode() not in content
    assert b"***" not in server.put["outputs.json"]


CACHED = f"""
import json, os
name = json.load(open("inputs.json"))["inputs"]["data"]["file"]
os.makedirs("out", exist_ok=True)
open("out/read", "wb").write(open(name, "rb").read())
open("out/where", "w").write(os.path.realpath(name))
open("outputs.json", "w").write({ACCEPTED!r})
"""


def test_the_orgs_cache_volume_serves_a_step_that_is_not_root(
    server: StandIn,
) -> None:
    """Two steps as a user other than root on one new cache volume: the
    first fetches the cached file into it, the second only links it, and
    each reads it through its own item folder, gone once it ends."""
    volume = f"unicon-cache-lab-{uuid.uuid4().hex[:8]}"
    data = b"a task file\n" * 1000
    try:
        for _ in range(2):
            program = a_file(server, "in/program.py", CACHED.encode())
            cached = a_file(server, "cache/q7Lm2/data", data, cached=True)
            inputs = {"schema_version": 6, "inputs": {"data": {"file": cached["path"]}}}
            flags = ["--user=65532:65532", "--volume", f"{volume}:/cache"]
            command = ["python3", "/work/in/program.py"]
            done = step(
                server,
                image("primitive-base"),
                inputs,
                [program, cached],
                flags,
                command,
            )
            assert done.returncode == 0, done.stdout + done.stderr
            assert server.put["out/read"] == data
            where = server.put["out/where"].decode()
            assert re.fullmatch(r"/cache/items/step-[^/]+/q7Lm2/data", where), where
        fetched = [s for s in server.seen if s.path.startswith("/files/")]
        assert len(fetched) == 3, fetched
        listing = docker(
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "--volume",
            f"{volume}:/cache",
            image("primitive-base"),
            "-c",
            "ls /cache/objects; ls -A /cache/items",
        )
        assert listing.split() == [hashlib.sha256(data).hexdigest()]
    finally:
        docker("volume", "rm", "--force", volume, check=False)


DOUBLE = """
import sys
print(int(sys.stdin.readline()) * 2)
"""


def test_exec_on_the_base_image_runs_a_batch_through_the_wrapper(
    server: StandIn, tmp_path: Path
) -> None:
    """exec on the base image, privileged and with exec's own memory for the
    batch, as a grading machine starts it: the wrapper fetches the program and
    inputs, hands exec the job's box users, uploads every output with its
    hash and posts done, and the token goes only to the door."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "__main__.py").write_text(DOUBLE)
    zipapp.create_archive(app, tmp_path / "program", interpreter="/usr/bin/env python3")
    files = [a_file(server, "in/p/program", (tmp_path / "program").read_bytes())]
    batch = []
    for run, number in (("r1", 21), ("r2", 50)):
        files.append(a_file(server, f"in/{run}/stdin", f"{number}\n".encode()))
        program = {"program": {"file": "in/p/program"}}
        limits = {"time_limit": 1, "memory_limit": 64}
        stdin = {"stdin": {"file": f"in/{run}/stdin"}}
        batch.append({"run": run, "inputs": program | stdin | limits})
    inputs = {"schema_version": 6, "batch": batch}
    userns = SIBLINGS / "primitive-exec" / "tests" / "userns.py"
    flags = ["--privileged", "--memory=320m", "--memory-swap=320m"]
    flags += ["--pids-limit=160", "--cpus=1", "--ulimit=cpu=5:5"]
    flags += ["--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m"]
    flags += ["--volume", f"{userns}:/userns.py:ro"]
    command = ["python3", "/userns.py", "/usr/local/bin/exec"]
    done = step(
        server,
        on_base("exec"),
        inputs,
        files,
        flags,
        command,
        box_users={"first": 60000, "last": 60009},
        fail_on_non_accepted=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    result = json.loads(server.put["outputs.json"])
    outcomes = {e["run"]: e["outputs"]["outcome"] for e in result["batch"]}
    assert outcomes == {"r1": "accepted", "r2": "accepted"}, result
    printed = sorted(v for k, v in server.put.items() if k.startswith("out/"))
    assert b"42\n" in printed and b"100\n" in printed
    (asked,) = server.asked
    for entry in asked["files"]:
        content = server.put[entry["path"]]
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()
        assert entry["size"] == len(content)
    assert server.calls("POST", "/door/done") == 1
    for seen in server.seen:
        expected = f"Bearer {TOKEN}" if seen.path.startswith("/door") else None
        assert seen.authorization == expected, seen
