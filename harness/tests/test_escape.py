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
how sandbox-run's own CI runs these against the image it just built. The
fixtures speak the contract the image speaks: each image is asked in
contract version 5 first, and one that answers with an error about the
contract version is asked again in version 4, the version the pinned
releases speak. In 5 a batch item is keyed `test`, a test id
`escape/<attack>`, and compile's `source` is a folder holding the one file;
in 4 an item is keyed `id`, the attack's name, and `source` is the file.
Every output is read at the path the primitive reports. The inputs.json
files are written here, not by the harness: the escape fixtures test the
container the harness builds, not the files it writes.
sandbox-run refuses to run anything on a kernel without Landlock, so on such
a kernel the first test fails rather than passing on nothing. Run with
`pytest -m docker harness/tests/test_escape.py`; it needs the daemon's unix
socket on this machine (DOCKER_HOST, or /var/run/docker.sock).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from filter_tests.lab import PYTHON, Lab, daemon_socket, docker, docker_ready, lab
from unicon_harness.contracts import violation
from unicon_harness.docker import DockerClient
from unicon_harness.plan import Limits, Step
from unicon_harness.sandbox import create_body
from unicon_harness.workspace import STEP_UID, Workspace

COMPILE_IMAGE = os.environ.get("COMPILE_IMAGE") or (
    "ghcr.io/uniconhq/primitive-compile@sha256:"
    "f66e9ae767e19e95ed5fa2c60386cf1444b0a62ea3f66a73f9b94d1d8f26e0de"
)
"""primitive-compile v1.1.1."""
SANDBOX_RUN_IMAGE = os.environ.get("SANDBOX_RUN_IMAGE") or (
    "ghcr.io/uniconhq/primitive-sandbox-run@sha256:"
    "bd4f45b3dc02647b6cf1168fb5e4dc14877be48983ff73b4daf864a3cffe0943"
)
"""primitive-sandbox-run v1.3.0."""

SOURCE = (Path(__file__).parent / "escape" / "escape.c").read_text(encoding="utf-8")
MOUNT = Path("/w")
"""Where a Workspace would see the test's volume; nothing mounts it there."""
LIMITS = Limits(
    time_ms=120_000, cpu_ms=6000, memory_mb=400, pids=128, output_mb=64, gpus=0
)
"""The step's limits: a CPU limit the batch's own work stays well inside; a
memory limit below the 512 MB the fixture holds when it is not stopped, with
room beside the 256 MB `/tmp` the disk attack fills, since a tmpfs's pages
count against the container's memory; and an output limit below the
fixture's one 128 MB file."""
DISK_MEMORY_MB = 300
"""The disk attack's own memory limit, above the 256 MB `/tmp` it fills:
sandbox-run counts a run's files in its directory, which is on that tmpfs,
as memory, so under the other attacks' 64 MB it would be stopped for memory
before it could show the directory is bounded by the tmpfs."""
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


def without_ipc_none(body: dict[str, Any]) -> None:
    """The daemon's default ipc mode, which mounts a writable /dev/shm."""
    del body["HostConfig"]["IpcMode"]


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
    "ipc none": (without_ipc_none, "files", "write-dev-shm"),
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
    "HostConfig.Init",
    "HostConfig.IpcMode",
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
CPU share, the mounts and the log's size, and `Init: false`, which only
differs from leaving it out on a daemon configured to add an init)."""


VERSIONS = (5, 4)
"""The contract versions the fixtures speak, in the order an image is asked:
the harness's own first, then the one the pinned releases speak."""
SOURCE_FOLDER = "in/source"
"""Where compile finds the fixture's source, `escape.c`."""


def compile_inputs(version: int) -> dict[str, Any]:
    """compile's inputs.json in `version`: the source a folder holding the one
    file in 5, the file itself in 4.
    """
    source = (
        {"folder": SOURCE_FOLDER}
        if version >= 5
        else {"file": f"{SOURCE_FOLDER}/escape.c"}
    )
    return {"schema_version": version, "inputs": {"source": source, "language": "c"}}


def item_key(attack: str, version: int) -> str:
    """The batch key of an attack: a test id, `<group>/<test>`, in 5."""
    return f"escape/{attack}" if version >= 5 else attack


def batch_inputs(version: int) -> dict[str, Any]:
    """sandbox-run's inputs.json in `version`: one item per attack, keyed
    `test` in 5 and `id` in 4.
    """
    key = "test" if version >= 5 else "id"
    return {
        "schema_version": version,
        "batch": [
            {
                key: item_key(attack, version),
                "inputs": {
                    "binary": {"file": "in/binary"},
                    "input": {"file": f"in/{attack}"},
                    "time_limit": 1,
                    "memory_limit": DISK_MEMORY_MB if attack == "disk" else 64,
                },
            }
            for attack in ATTACKS
        ],
    }


def by_attack(outputs: dict[str, Any], version: int) -> dict[str, dict[str, Any]]:
    """A batch's outputs by the attack each item answers for."""
    key = "test" if version >= 5 else "id"
    found: dict[str, dict[str, Any]] = {}
    for item in outputs["batch"]:
        attack = str(item[key]).rpartition("/")[2]
        assert item_key(attack, version) == item[key], item
        found[attack] = item["outputs"]
    return found


READ_OUTPUTS = (
    "import json, pathlib, sys; base = pathlib.Path(sys.argv[1]);"
    " paths = json.loads(sys.argv[2]);"
    " print(json.dumps({attack: (base / path).read_text(errors='replace')"
    " for attack, path in paths.items()}))"
)
"""A program that prints, as JSON, the text of each file of `paths` (attack
to a path under the directory `base`), given as `base` and `paths` in JSON."""


def another_version(outputs: dict[str, Any]) -> bool:
    """Whether the primitive refused inputs.json for its contract version."""
    return "contract version" in str(outputs.get("error", ""))


class Fixtures:
    """The fixture binary, built once, and the means to run it: a volume of
    the test's own whose `unicon-steps/` directories are mounted the way the
    harness mounts a step's, and a client on the daemon's own socket. Each
    step directory holds inputs.json in every version the fixtures speak, as
    `inputs-<version>.json`, and the one an image is asked with is copied to
    inputs.json.
    """

    def __init__(self, made: Lab, client: DockerClient) -> None:
        self.made = made
        self.client = client
        self.versions: dict[str, int] = {}
        files: dict[str, bytes | str] = {
            f"unicon-steps/compile/{SOURCE_FOLDER}/escape.c": SOURCE,
            "unicon-steps/direct/in/1.ans": "the answer\n",
        }
        for version in VERSIONS:
            files[f"unicon-steps/compile/inputs-{version}.json"] = json.dumps(
                compile_inputs(version)
            )
            files[f"unicon-steps/batch/inputs-{version}.json"] = json.dumps(
                batch_inputs(version)
            )
        for attack in ATTACKS:
            files[f"unicon-steps/batch/in/{attack}"] = f"{attack}\n"
        self.volume = made.volume("escape", files)
        self.workspace = Workspace(
            volume=self.volume, mount=MOUNT, user=f"{STEP_UID}:{STEP_UID}"
        )
        self._in_volume(f"chown -R {STEP_UID}:{STEP_UID} /v/unicon-steps")

    def build(self) -> None:
        code, printed, outputs = self._speaking(COMPILE_IMAGE, "compile")
        assert code == 0 and outputs["outputs"]["outcome"] == "accepted", (
            printed,
            outputs,
        )
        binary = outputs["outputs"]["binary"]["file"]
        self._in_volume(
            "cd /v/unicon-steps"
            f" && cp compile/{binary} batch/in/binary"
            f" && cp compile/{binary} direct/escape"
            " && chmod 0755 batch/in/binary direct/escape"
            f" && chown {STEP_UID}:{STEP_UID} batch/in/binary direct/escape"
        )

    def through_sandbox_run(self) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """sandbox-run's outputs for the batch by attack, and what each attack
        printed, read at the path sandbox-run reports for it.
        """
        code, printed, outputs = self._speaking(SANDBOX_RUN_IMAGE, "batch")
        assert code == 0 and "batch" in outputs, (printed, outputs)
        found = by_attack(outputs, self.versions["batch"])
        paths = {attack: item["output"]["file"] for attack, item in found.items()}
        texts: dict[str, str] = json.loads(
            self._in_volume(
                READ_OUTPUTS, "/v/unicon-steps/batch", json.dumps(paths), python=True
            )
        )
        return found, texts

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

    def _speaking(
        self, image: str, directory: str
    ) -> tuple[int | None, str, dict[str, Any]]:
        """Run `image` over a step directory in the first version it does not
        refuse as another contract version, and remember that version.
        """
        for version in VERSIONS:
            self._in_volume(
                f"cd /v/unicon-steps/{directory}"
                f" && cp inputs-{version}.json inputs.json"
                " && rm -f outputs.json"
                f" && chown {STEP_UID}:{STEP_UID} inputs.json"
            )
            code, printed = self._run(self._body(image, directory), 300)
            outputs = self.read_json(f"{directory}/outputs.json")
            if not another_version(outputs):
                break
        self.versions[directory] = version
        return code, printed, outputs

    def _body(self, image: str, directory: str) -> dict[str, Any]:
        step = _step(image)
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

    def _in_volume(self, script: str, *arguments: str, python: bool = False) -> str:
        program = ["python", "-c"] if python else ["sh", "-c"]
        return docker(
            "run",
            "--rm",
            "-v",
            f"{self.volume}:/v",
            PYTHON,
            *program,
            script,
            *arguments,
        )


def _step(image: str) -> Step:
    return Step(
        index=0,
        id="escape",
        primitive="unicon/escape",
        image=image,
        network=False,
        limits=LIMITS,
        outputs={},
        folders=frozenset(),
        kind="once",
        items=(),
    )


def _have(image: str) -> None:
    """Pull the image unless the daemon has it. `docker image inspect` prints
    an empty line even for an image it does not have, so its output is read
    stripped.
    """
    if not docker(
        "image", "inspect", "--format", "{{.Id}}", image, check=False
    ).strip():
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
    outcomes = {attack: item["outcome"] for attack, item in outputs.items()}

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
    step = _step("image")
    workspace = Workspace(volume="v", mount=MOUNT, user="10001:10001")
    body = create_body(step, workspace, MOUNT / "unicon-steps" / "x", "g")
    fields = {k for k in body if k != "HostConfig"} | {
        f"HostConfig.{k}" for k in body["HostConfig"]
    }
    assert fields == COVERED


def test_the_version_5_documents_keep_the_primitive_contract() -> None:
    assert violation(compile_inputs(5), "primitive", "inputs_file") is None
    assert violation(batch_inputs(5), "primitive", "inputs_file") is None
    assert compile_inputs(5)["inputs"]["source"] == {"folder": SOURCE_FOLDER}
    items = batch_inputs(5)["batch"]
    assert [item["test"] for item in items] == [f"escape/{a}" for a in ATTACKS]


def test_the_version_4_documents_are_the_ones_the_released_images_take() -> None:
    assert compile_inputs(4) == {
        "schema_version": 4,
        "inputs": {"source": {"file": "in/source/escape.c"}, "language": "c"},
    }
    items = batch_inputs(4)["batch"]
    assert [item["id"] for item in items] == list(ATTACKS)
    assert all("test" not in item for item in items)
    disk = next(item for item in items if item["id"] == "disk")
    assert disk["inputs"]["memory_limit"] == DISK_MEMORY_MB


@pytest.mark.parametrize("version", VERSIONS)
def test_a_batch_answer_is_read_by_attack_at_the_paths_it_reports(
    version: int, tmp_path: Path
) -> None:
    key = "test" if version >= 5 else "id"
    answer = {
        "schema_version": version,
        "batch": [
            {
                key: item[key],
                "outputs": {
                    "output": {"file": f"out/{n}/output"},
                    "outcome": "accepted",
                },
            }
            for n, item in enumerate(batch_inputs(version)["batch"], start=1)
        ],
    }
    found = by_attack(answer, version)
    assert list(found) == list(ATTACKS)
    paths = {attack: item["output"]["file"] for attack, item in found.items()}
    for attack, path in paths.items():
        (tmp_path / path).parent.mkdir(parents=True)
        (tmp_path / path).write_text(f"{attack} said\n", encoding="utf-8")

    printed = subprocess.run(
        [sys.executable, "-c", READ_OUTPUTS, str(tmp_path), json.dumps(paths)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout

    assert json.loads(printed) == {attack: f"{attack} said\n" for attack in ATTACKS}


def test_an_image_of_another_contract_version_is_told_apart() -> None:
    released = {
        "schema_version": 4,
        "error": "inputs.json is not written against contract version 4",
    }
    assert another_version(released)
    assert not another_version({"schema_version": 5, "error": "the disk broke"})
    assert not another_version({"schema_version": 5, "outputs": {"outcome": "x"}})
