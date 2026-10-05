"""The escape fixtures (`escape/escape.c`) on a real Docker, so that the
sandbox's results of 2026-10-04 (experiment A5) cannot quietly regress: a
protection nobody tests disappears the day someone edits a flag.

The fixture program is built by the compile primitive and run three ways,
each time in a container made from exactly the body the harness sends
(`sandbox.create_body`), straight to the daemon's own socket:

1. through sandbox-run, as a contestant's program is run: one batch, one test
   per attack. Every attack is refused.
2. as the container's own process, without sandbox-run in front, so the
   container's protections are tested alone. Every attack is refused except
   what belongs to the step: its own `/work`, which it may write and read.
3. as the container's own process with one protection taken away at a time.
   Each removal lets a named attack through; if one ever does not, the
   protection is no longer covered and the fixture set is missing a case.

The primitives are the released images by digest, the ones the platform's
image manifest pins; COMPILE_IMAGE and SANDBOX_RUN_IMAGE name others, which is
how sandbox-run's own CI runs these against the image it just built.
sandbox-run refuses to run anything on a kernel without Landlock, so on such
a kernel the first test fails rather than passing on nothing. Run with
`pytest -m docker harness/tests/test_escape.py`; it needs the daemon's unix
socket on this machine (DOCKER_HOST, or /var/run/docker.sock).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from filter_tests.lab import PYTHON, Lab, daemon_socket, docker, docker_ready, lab
from unicon_harness.docker import DockerClient
from unicon_harness.plan import Limits, Step
from unicon_harness.sandbox import create_body
from unicon_harness.workspace import STEP_UID, Workspace

COMPILE_IMAGE = os.environ.get("COMPILE_IMAGE") or (
    "ghcr.io/uniconhq/primitive-compile@sha256:"
    "943b37fb4f17187ab2e13fecafc682f61b4461fcf73a4954f9069c8c6d1b73e1"
)
"""primitive-compile v1.1.0."""
SANDBOX_RUN_IMAGE = os.environ.get("SANDBOX_RUN_IMAGE") or (
    "ghcr.io/uniconhq/primitive-sandbox-run@sha256:"
    "757eee935e2d722775eab54226fb5758ee349271024cc566e5ad4ca889b8de4b"
)
"""primitive-sandbox-run v1.1.0."""

SOURCE = (Path(__file__).parent / "escape" / "escape.c").read_text(encoding="utf-8")
MOUNT = Path("/w")
"""Where a Workspace would see the test's volume; nothing mounts it there."""
LIMITS = Limits(time_ms=120_000, cpu_ms=6000, memory_mb=400, pids=128, output_mb=64)
"""The step's limits: a CPU limit the batch's own work stays well inside; a
memory limit below the 512 MB the fixture holds when it is not stopped, with
room beside the 256 MB `/tmp` the disk attack fills, since a tmpfs's pages
count against the container's memory; and an output limit below the
fixture's one 128 MB file."""
DIRECT_SECONDS = 20.0
"""How long a run of the fixture alone may take before the test, standing in
for the harness's wall clock, kills it."""

ATTACKS = {
    "network": {"internet", "metadata", "dns", "network-interfaces"},
    "files": {"write-root", "write-var-tmp", "one-big-file", "write-work-root"},
    "kernel": {"ptrace-pid1", "mount", "unshare-user", "keyctl", "root-user"},
    "secrets": {"secrets"},
    "memory": set(),
    "fork": {"fork"},
    "threads": {"threads"},
    "spin": set(),
    "sleep": set(),
    "output": set(),
    "disk": {"disk"},
}
"""Each attack, and the checks it must print, so that a fixture that crashed
before trying anything does not pass as refused. The resource attacks that
print nothing are judged by their outcome."""

STEPS_OWN = {
    "write-work-root",
    "write-work-in",
    "read-task-answers",
    "read-other-test-input",
    "read-other-run-output",
}
"""What a container without sandbox-run may do: `/work` is the step's own.
Through sandbox-run, Landlock keeps a contestant's program out of it."""


@dataclass(frozen=True)
class Ran:
    """One run of the fixture alone: its exit code, or None when the test had
    to kill it, whether the kernel killed something at the memory limit, and
    what it printed as {check: (verdict, detail)}.
    """

    exit_code: int | None
    out_of_memory: bool
    checks: dict[str, tuple[str, str]]

    @property
    def escaped(self) -> set[str]:
        return {
            name for name, (verdict, _) in self.checks.items() if verdict == "ESCAPED"
        }


def checks_of(printed: str) -> dict[str, tuple[str, str]]:
    """{check: (verdict, detail)} from the fixture's lines. A check printed
    more than once (one per directory tried) keeps every result, the later
    ones under the name with `+` added.
    """
    found: dict[str, tuple[str, str]] = {}
    for line in printed.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2 and parts[1] in ("REFUSED", "ESCAPED", "INFO"):
            name = parts[0]
            while name in found:
                name += "+"
            found[name] = (parts[1], parts[2] if len(parts) > 2 else "")
    return found


def without_network(body: dict[str, Any]) -> None:
    del body["NetworkDisabled"]
    del body["HostConfig"]["NetworkMode"]


def without_read_only(body: dict[str, Any]) -> None:
    del body["HostConfig"]["ReadonlyRootfs"]


def without_cap_drop(body: dict[str, Any]) -> None:
    del body["HostConfig"]["CapDrop"]


def without_no_new_privileges(body: dict[str, Any]) -> None:
    body["HostConfig"]["SecurityOpt"].remove("no-new-privileges")


def without_seccomp(body: dict[str, Any]) -> None:
    """The daemon's default may itself be the built-in profile, so taking the
    option away proves nothing there; unconfined is what a daemon whose
    default is unconfined would give.
    """
    options = body["HostConfig"]["SecurityOpt"]
    options[options.index("seccomp=builtin")] = "seccomp=unconfined"


def without_user(body: dict[str, Any]) -> None:
    body["User"] = "0:0"


def without_memory(body: dict[str, Any]) -> None:
    del body["HostConfig"]["Memory"]
    del body["HostConfig"]["MemorySwap"]


def without_pids(body: dict[str, Any]) -> None:
    del body["HostConfig"]["PidsLimit"]


def without_ulimit(name: str) -> Callable[[dict[str, Any]], None]:
    def change(body: dict[str, Any]) -> None:
        host = body["HostConfig"]
        host["Ulimits"] = [u for u in host["Ulimits"] if u["Name"] != name]

    return change


REMOVALS: dict[str, tuple[Callable[[dict[str, Any]], None], str, str]] = {
    "network none": (without_network, "network", "network-interfaces"),
    "read-only root": (without_read_only, "files", "write-var-tmp"),
    "cap-drop ALL": (without_cap_drop, "kernel", "capability-bounding-set"),
    "no-new-privileges": (without_no_new_privileges, "kernel", "no-new-privileges"),
    "seccomp=builtin": (without_seccomp, "kernel", "keyctl"),
    "non-root user": (without_user, "kernel", "root-user"),
    "memory limit": (without_memory, "memory", "memory"),
    "pids limit": (without_pids, "fork", "fork"),
    "cpu ulimit": (without_ulimit("cpu"), "spin", ""),
    "fsize ulimit": (without_ulimit("fsize"), "files", "one-big-file"),
}
"""Each protection the harness sets that keeps a program in its box, how it
is taken away, the attack that shows it gone and the check that then
escapes. The CPU limit has no check: without it the endless loop runs until
the test kills it."""

COVERED = {
    "Image",
    "User",
    "WorkingDir",
    "Env",
    "Labels",
    "NetworkDisabled",
    "HostConfig.NetworkMode",
    "HostConfig.ReadonlyRootfs",
    "HostConfig.CapDrop",
    "HostConfig.SecurityOpt",
    "HostConfig.Memory",
    "HostConfig.MemorySwap",
    "HostConfig.NanoCpus",
    "HostConfig.PidsLimit",
    "HostConfig.Ulimits",
    "HostConfig.Mounts",
    "HostConfig.LogConfig",
}
"""Every field of the harness's create body this file has accounted for:
those REMOVALS takes away, and the rest, which are not protections an attack
can show missing (the image, labels, working directory, environment, the
CPU share, the mounts and the log's size)."""


class Fixtures:
    """The fixture binary, built once, and the means to run it: a volume of
    the test's own whose `unicon-steps/` directories are mounted the way the
    harness mounts a step's, and a client on the daemon's own socket.
    """

    def __init__(self, made: Lab, client: DockerClient) -> None:
        self.made = made
        self.client = client
        inputs = {
            "schema_version": 4,
            "batch": [
                {
                    "id": attack,
                    "inputs": {
                        "binary": {"file": "in/binary"},
                        "input": {"file": f"in/{attack}"},
                        "time_limit": 1,
                        "memory_limit": 64,
                    },
                }
                for attack in ATTACKS
            ],
        }
        files: dict[str, bytes | str] = {
            "unicon-steps/compile/in/escape.c": SOURCE,
            "unicon-steps/compile/inputs.json": json.dumps(
                {
                    "schema_version": 4,
                    "inputs": {"source": {"file": "in/escape.c"}, "language": "c"},
                }
            ),
            "unicon-steps/batch/inputs.json": json.dumps(inputs),
            "unicon-steps/direct/in/1.ans": "the answer\n",
        }
        for attack in ATTACKS:
            files[f"unicon-steps/batch/in/{attack}"] = f"{attack}\n"
        self.volume = made.volume("escape", files)
        self.workspace = Workspace(
            volume=self.volume, mount=MOUNT, user=f"{STEP_UID}:{STEP_UID}"
        )
        self._in_volume(f"chown -R {STEP_UID}:{STEP_UID} /v/unicon-steps")

    def build(self) -> None:
        code, printed = self._run(self._body(COMPILE_IMAGE, "compile"), 300)
        outputs = self.read_json("compile/outputs.json")
        assert code == 0 and outputs["outputs"]["outcome"] == "accepted", (
            printed,
            outputs,
        )
        self._in_volume(
            "cd /v/unicon-steps"
            " && cp compile/out/binary batch/in/binary"
            " && cp compile/out/binary direct/escape"
            " && chmod 0755 batch/in/binary direct/escape"
            f" && chown {STEP_UID}:{STEP_UID} batch/in/binary direct/escape"
        )

    def through_sandbox_run(self) -> tuple[dict[str, Any], dict[str, str]]:
        """sandbox-run's outputs.json for the batch, and what each attack
        printed.
        """
        code, printed = self._run(self._body(SANDBOX_RUN_IMAGE, "batch"), 300)
        outputs = self.read_json("batch/outputs.json")
        assert code == 0 and "batch" in outputs, (printed, outputs)
        texts = json.loads(
            self._in_volume(
                "import json, pathlib; base = pathlib.Path('/v/unicon-steps/batch');"
                " print(json.dumps({p.parent.name: p.read_text(errors='replace')"
                " for p in base.glob('out/*/output')}))",
                python=True,
            )
        )
        return outputs, texts

    def alone(
        self,
        attack: str,
        change: Callable[[dict[str, Any]], None] | None = None,
    ) -> Ran:
        """The fixture as the container's own process, under the harness's body
        changed by `change`, killed by the test after DIRECT_SECONDS.
        """
        body = self._body(SANDBOX_RUN_IMAGE, "direct")
        body["Entrypoint"] = ["sh", "-c", f"echo {attack} | /work/escape"]
        if change is not None:
            change(body)
        container = self.client.create(body)
        try:
            self.client.start(container)
            code = self.client.wait(container, DIRECT_SECONDS)
            if code is None:
                self.client.kill(container)
                self.client.wait(container, 30)
            state = self.client.inspect(container).get("State") or {}
            printed = self.client.logs(container, 1024 * 1024)
        finally:
            self.client.remove(container)
        return Ran(code, bool(state.get("OOMKilled")), checks_of(printed))

    def read_json(self, path: str) -> dict[str, Any]:
        found: dict[str, Any] = json.loads(
            self._in_volume(f"cat /v/unicon-steps/{path}")
        )
        return found

    def _body(self, image: str, directory: str) -> dict[str, Any]:
        step = Step(
            index=0,
            id="escape",
            primitive="unicon/escape",
            image=image,
            limits=LIMITS,
            kind="once",
            items=(),
        )
        return create_body(
            step,
            self.workspace,
            MOUNT / "unicon-steps" / directory,
            self.made.grading(),
        )

    def _run(self, body: dict[str, Any], seconds: float) -> tuple[int | None, str]:
        container = self.client.create(body)
        try:
            self.client.start(container)
            code = self.client.wait(container, seconds)
            printed = self.client.logs(container, 64 * 1024)
        finally:
            self.client.remove(container)
        return code, printed

    def _in_volume(self, script: str, python: bool = False) -> str:
        program = ["python", "-c"] if python else ["sh", "-c"]
        return docker(
            "run", "--rm", "-v", f"{self.volume}:/v", PYTHON, *program, script
        )


def _have(image: str) -> None:
    """Pull the image unless the daemon has it. `docker image inspect` prints
    an empty line even for an image it does not have, so its output is read
    stripped.
    """
    if not docker("image", "inspect", "--format", "{{.Id}}", image, check=False).strip():
        docker("pull", "-q", image)


@pytest.fixture(scope="module")
def fixtures() -> Iterator[Fixtures]:
    if not Path(daemon_socket()).exists():
        pytest.skip("needs the daemon's unix socket on this machine")
    _have(COMPILE_IMAGE)
    _have(SANDBOX_RUN_IMAGE)
    client = DockerClient(daemon_socket())
    try:
        with lab() as made:
            ready = Fixtures(made, client)
            ready.build()
            yield ready
    finally:
        client.close()


@pytest.mark.docker
@pytest.mark.skipif(not docker_ready(), reason="needs a Docker daemon")
def test_through_sandbox_run_every_attack_is_refused(fixtures: Fixtures) -> None:
    outputs, printed = fixtures.through_sandbox_run()
    outcomes = {item["id"]: item["outputs"]["outcome"] for item in outputs["batch"]}

    assert outcomes["memory"] == "memory_limit"
    assert outcomes["spin"] == "time_limit"
    assert outcomes["sleep"] == "time_limit"
    assert outcomes["output"] == "output_limit"
    for attack, expected in ATTACKS.items():
        checks = checks_of(printed.get(attack, ""))
        escaped = {n for n, (verdict, _) in checks.items() if verdict == "ESCAPED"}
        assert escaped == set(), (attack, {n: checks[n] for n in escaped})
        assert expected <= checks.keys(), (attack, outcomes[attack], sorted(checks))
    # Landlock keeps the program out of /work: not even the step's own
    # directory is written.
    files = checks_of(printed["files"])
    assert files["write-work-root"][0] == "REFUSED"


@pytest.mark.docker
@pytest.mark.skipif(not docker_ready(), reason="needs a Docker daemon")
def test_the_container_alone_refuses_every_attack_but_its_own_work(
    fixtures: Fixtures,
) -> None:
    # Filling the disk and a huge output are left out: alone, the program's
    # writable place is the step's /work, which nothing limits yet, so they
    # would only write the fixture's cap. The endless sleep is left out
    # because only the caller's clock stops it, as the harness's does.
    for attack, expected in ATTACKS.items():
        if attack in ("disk", "output", "sleep"):
            continue
        ran = fixtures.alone(attack)
        assert ran.escaped - STEPS_OWN == set(), (attack, ran.checks)
        assert expected <= ran.checks.keys(), (attack, sorted(ran.checks))
        if attack == "memory":
            assert ran.out_of_memory, ran
        if attack == "spin":
            assert ran.exit_code not in (None, 0), "the CPU limit did not stop it"


@pytest.mark.docker
@pytest.mark.skipif(not docker_ready(), reason="needs a Docker daemon")
@pytest.mark.parametrize("removed", list(REMOVALS))
def test_taking_away_one_protection_lets_an_attack_through(
    fixtures: Fixtures, removed: str
) -> None:
    change, attack, check = REMOVALS[removed]
    ran = fixtures.alone(attack, change)
    if check:
        assert check in ran.escaped, (removed, ran.checks)
    else:
        assert ran.exit_code is None, f"without the {removed} the loop still ended"


def test_every_field_of_the_create_body_is_accounted_for() -> None:
    """A field added to the harness's create body is either a protection, and
    gets a removal above with an attack that shows it gone, or is said here
    not to be one.
    """
    step = Step(0, "escape", "unicon/escape", "image", LIMITS, "once", ())
    workspace = Workspace(volume="v", mount=MOUNT, user="10001:10001")
    body = create_body(step, workspace, MOUNT / "unicon-steps" / "x", "g")
    fields = {k for k in body if k != "HostConfig"} | {
        f"HostConfig.{k}" for k in body["HostConfig"]
    }
    assert fields == COVERED
