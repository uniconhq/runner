"""The escape fixtures (`escape/escape.c`) on a real Docker, so that the
sandbox's results of 2026-10-04 (experiment A5) cannot quietly regress: a
protection nobody tests disappears the day someone edits a flag.

The fixture program is built by the compile primitive and run these ways,
each time in a container made from exactly the body the harness sends
(`sandbox.create_body`), straight to the daemon's own socket:

1. through the run primitive, sandbox-run or exec, as a contestant's program
   is run: one batch, one item per attack. Every attack is refused. Against
   exec, which speaks contract 6, the batch also holds the attacks exec's
   isolate boxes refuse: a connection out and an io_uring ring, refused
   again with the container's network and process limit left off, and a
   System V segment and an attribute on the program left for the next item.
   exec's container is changed to the shape its step runs in, which the
   harness does not make yet: privileged, as root, its root writable, given
   its box users, and on root Docker started through primitive-exec's
   `tests/userns.py`, since exec refuses to start as the machine's own root.
2. as the container's own process, without the primitive in front, so the
   container's protections are tested alone. Every attack is refused except
   what belongs to the step: its own `/work`, which it may write and read.
3. as the container's own process with one protection taken away at a time.
   Each removal lets a named attack through; if one ever does not, the
   protection is no longer covered and the fixture set is missing a case.
   exec's own protections, isolate's settings for its boxes, are taken
   away in an image made from exec's with that one setting changed: the
   box's network namespace, its process limit and a fresh box per item.
4. through exec's programs form, when EXEC_IMAGE names one, in the same
   shape as its one form, as the entrant of a partner given a hidden file:
   the entrant reaches none of it, nor sees the partner's process. Its
   inputs.json is checked against exec's own `read_inputs`, from
   EXEC_SOURCE or a primitive-exec checkout beside this repo, as well as
   the schema.

The primitives are the released images by digest, the ones the platform's
image manifest pins; COMPILE_IMAGE and EXEC_IMAGE (or SANDBOX_RUN_IMAGE)
name others, which is how a primitive's own CI runs these against the image
it just built. EXEC_SOURCE names a primitive-exec checkout, or one beside
this repo is used, for its `tests/userns.py`. The fixtures speak
the contract the image speaks: each image is asked in contract version 6
first, then 5, and one that answers with an error about the contract
version is asked again in the next, down to version 4, the version the
pinned releases speak. In 6 a batch item is keyed `run`, the attack's name,
its program is `program` and its input `stdin`, and compile answers with
`program`; in 5 an item is keyed `test`, a test id `escape/<attack>`, and
compile's `source` is a folder holding the one file; in 4 an item is keyed
`id`, the attack's name, and `source` is the file. Every output is read at
the path the primitive reports. The inputs.json
files are written here, not by the harness: the escape fixtures test the
container the harness builds, not the files it writes.
sandbox-run refuses to run anything on a kernel without Landlock, so on such
a kernel the first test fails rather than passing on nothing. Run with
`pytest -m docker harness/tests/test_escape.py`; it needs the daemon's unix
socket on this machine (DOCKER_HOST, or /var/run/docker.sock).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
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
RUN_IMAGE = (
    os.environ.get("EXEC_IMAGE")
    or os.environ.get("SANDBOX_RUN_IMAGE")
    or (
        "ghcr.io/uniconhq/primitive-sandbox-run@sha256:"
        "bd4f45b3dc02647b6cf1168fb5e4dc14877be48983ff73b4daf864a3cffe0943"
    )
)
"""The run primitive: primitive-sandbox-run v1.3.0, unless exec is named."""
RUN_IS_EXEC = bool(os.environ.get("EXEC_IMAGE"))
"""Whether the run primitive is exec, whose container is made privileged."""
EXEC_SOURCE = Path(
    os.environ.get("EXEC_SOURCE")
    or Path(__file__).resolve().parents[3] / "primitive-exec"
)
"""A primitive-exec checkout, whose `tests/userns.py` starts exec on root
Docker, and whose `read_inputs` the programs-form document is checked
against."""
EXEC_BOX_USERS = "60000-60009"
"""The box users an exec step is given, as whoever starts the step gives
them."""

SOURCE = (Path(__file__).parents[2] / "escape" / "escape.c").read_text(encoding="utf-8")
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
FORK_MEMORY_MB = 360
"""The fork attack's own memory limit: an exec box counts its processes'
kernel memory, about 100 KB each (304 MB for 3,000 on Docker Desktop's
Linux VM), so with its process limit taken away the fixture's would be
stopped for memory under 64 MB before they showed it."""
MEMORY_MB = {"disk": DISK_MEMORY_MB, "files": DISK_MEMORY_MB, "fork": FORK_MEMORY_MB}
"""The attacks with a memory limit of their own; the others have 64 MB. The
files attack's one big file goes to `/tmp` in an exec box, whose pages count
against the box's memory, so under 64 MB the run would be stopped for memory
before the file size limit showed, and before its later checks."""
STEP_MEMORY_MB = 256
"""What an exec step's container is given above its largest item's memory
limit, as its `limits_from` declares. exec refuses a batch with an item
that, with the files copied in beside it, does not fit."""
PROGRAMS_MEMORY_MB = 128 + 512
"""What the programs form's step is given: its item's memory limits
together, the entrant's and the partner's 64 MB, and the 512 MB its
`limits_from` adds."""


def step_memory_mb(attacks: Iterable[str]) -> int:
    """The memory an exec step running `attacks` is given: its largest
    item's memory limit and the step's own."""
    return max(MEMORY_MB.get(attack, 64) for attack in attacks) + STEP_MEMORY_MB


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

EXEC_ATTACKS = {
    "sockets": {
        "inet-lookup",
        "inet-connect",
        "inet-route",
        "inet6-connect",
        "io-uring",
    },
    "leave-sysv": {"sysv-made"},
    "find-sysv": {"sysv-left"},
    "stdin": {"write-stdin"},
    "mark": {"mark"},
    "read-mark": {"mark-left"},
}
"""The attacks exec's boxes refuse, from contract 6: a box has a network
namespace of its own, so no connection goes out, isolate's filter refuses
the ring, the box cannot write its input by opening it again through
`/proc/self/fd/0`, whatever the input's mode, and what one item leaves, a
System V segment or an attribute on the program, is gone for the next,
which runs right after it in a fresh box. The container alone does not
refuse them all: they are exec's."""

PARTNER_CHECKS = {
    "partner-process",
    "partner-secret",
    "partner-folder",
    "partner-tmp",
}
"""What the programs form's entrant tries on its partner, each of which must be
refused: seeing the partner's process at all, the hidden file by the path
the partner tells it, that path's folder listing the file, and a file the
partner left in its `/tmp`. Each program has an isolate box of its own,
with its own PID namespace, files and `/tmp`, so the entrant tries the
attacks through the partner's process, its directory, root, environment,
memory and open files through `/proc`, tracing and signalling it, only
when it sees that process, and then each must be refused too."""

STEPS_OWN = {
    "write-work-root",
    "write-work-in",
    "read-task-answers",
    "read-other-test-input",
    "read-other-run-output",
}
"""What a container without sandbox-run may do: `/work` is the step's own.
Through sandbox-run, Landlock keeps a contestant's program out of it."""

BOXES_OWN = {"write-dev-shm", "capability-bounding-set"}
"""What a program in an exec box may do: write the box's own `/dev/shm`, a
tmpfs isolate makes for each run, as it does `/tmp`, whose pages count
against the box's memory; and keep the full bounding set isolate leaves it,
which gives it nothing, since isolate sets `no_new_privs` on every box, so
no program it starts gains a capability."""


def machines_own() -> set[str]:
    """What a program in an exec box may do where the machine allows it:
    make a BPF map, where `kernel.unprivileged_bpf_disabled` is 0, as in
    Docker Desktop's VM. isolate's filter does not refuse `bpf`, and the
    privileged container has no seccomp profile, so the machine's setting
    decides; Ubuntu's default, 2, refuses it."""
    setting = docker(
        "run", "--rm", PYTHON, "cat", "/proc/sys/kernel/unprivileged_bpf_disabled"
    )
    return {"bpf"} if setting.strip() == "0" else set()


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


def attacks(version: int) -> dict[str, set[str]]:
    """The attacks a batch in `version` holds: exec's own from 6."""
    return ATTACKS | EXEC_ATTACKS if version >= 6 else dict(ATTACKS)


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
    "HostConfig.CgroupnsMode",
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
CPU share, the mounts and the log's size, `Init: false`, which only
differs from leaving it out on a daemon configured to add an init, and the
private cgroup namespace, which is for measuring memory)."""


def without_pids_or_network(body: dict[str, Any]) -> None:
    without_network(body)
    without_pids(body)


def as_exec_runs(
    body: dict[str, Any], memory_mb: int, program: str = "/usr/local/bin/exec"
) -> None:
    """The harness's body changed to the shape an exec step runs
    in, which the harness does not make yet: privileged, as root, its root
    writable, given its box users, and its memory `memory_mb`, as the step
    would be given for its largest item, both the container's and the
    step's. A privileged container has no seccomp profile, and the daemon
    refuses to start one named `seccomp=builtin`. On
    root Docker, where a privileged container's root is the machine's and
    the primitive refuses to start, its `program` is started in a user
    namespace of its own by primitive-exec's `tests/userns.py`.
    """
    host = body["HostConfig"]
    host["Privileged"] = True
    host["Memory"] = host["MemorySwap"] = memory_mb * 1024 * 1024
    body["User"] = "0:0"
    del host["ReadonlyRootfs"]
    del host["CapDrop"]
    host["SecurityOpt"].remove("seccomp=builtin")
    body["Env"] = [
        *body["Env"],
        f"UNICON_BOX_USERS={EXEC_BOX_USERS}",
        f"UNICON_STEP_MEMORY_MB={memory_mb}",
    ]
    if "rootless" not in docker("info", "--format", "{{json .SecurityOptions}}"):
        userns = EXEC_SOURCE / "tests" / "userns.py"
        if not userns.is_file():
            pytest.skip(f"no primitive-exec checkout at {EXEC_SOURCE}; set EXEC_SOURCE")
        host["Mounts"] = [
            *host["Mounts"],
            {
                "Type": "bind",
                "Source": str(userns),
                "Target": "/userns.py",
                "ReadOnly": True,
            },
        ]
        body["Entrypoint"] = ["python3", "/userns.py", program]


CHANGED_EXEC = """\
FROM {image}
COPY changes.json /changes.json
RUN python3 -c "import functools, json, pathlib; \\
p = pathlib.Path('/usr/local/bin/exec'); t = p.read_text(); \\
c = json.loads(pathlib.Path('/changes.json').read_text()); \\
assert all(t.count(old) == 1 for old, _ in c), c; \\
p.write_text(functools.reduce(lambda s, r: s.replace(*r), c, t))" \\
    && rm /changes.json
"""
"""exec's image with `changes.json`'s replacements made in its program, each
of a text found there exactly once."""

SHARE_NET = (
    (
        "        f\"--dir=tmp={box / 'tmp'}:rw\",\n"
        '        f"--dir=dev={BOX_DEV}:dev",\n',
        "        f\"--dir=tmp={box / 'tmp'}:rw\",\n"
        '        "--share-net",\n'
        '        f"--dir=dev={BOX_DEV}:dev",\n',
    ),
)
"""A box that shares the container's network namespace: `--share-net` in the
command a test is run under, found by the line before it, as the launcher's
self-test names the same device folder."""
NO_PROCESS_LIMIT = (('f"--processes={PROCESS_LIMIT}",', '"--processes",'),)
"""A box with no process limit: `--processes` with no number."""
KEPT_BOX = (
    (
        "    if folder.is_mount():\n        unmount(folder)\n",
        "    if folder.is_mount():\n        yield folder\n        return\n",
    ),
    (
        "def close_box(number: int, folder: Path) -> None:\n",
        "def close_box(number: int, folder: Path) -> None:\n    return\n",
    ),
    ("    cwd.mkdir(mode=0o755)\n", "    cwd.mkdir(mode=0o755, exist_ok=True)\n"),
    (
        "\n            (box / name).mkdir()\n",
        "\n            (box / name).mkdir(exist_ok=True)\n",
    ),
)
"""One box for the whole batch: made for the first item, then neither
unmounted nor cleaned, and found still mounted by each item after."""

EXEC_REMOVALS: dict[
    str,
    tuple[
        tuple[tuple[str, str], ...],
        Callable[[dict[str, Any]], None] | None,
        tuple[str, ...],
        str,
    ],
] = {
    "network namespace": (
        SHARE_NET,
        without_network,
        ("sockets",),
        "inet-route",
    ),
    "process limit": (NO_PROCESS_LIMIT, without_pids, ("fork",), "fork"),
    "fresh box": (KEPT_BOX, None, ("mark", "read-mark"), "mark-left"),
}
"""REMOVALS' rows for the protections exec gives its boxes: the changes to
exec's program that take one away, the change to the harness's body the
attack needs (the network left on, so a box sharing it has a route out;
no process limit on the container, so only the box's stops a fork bomb),
the attacks run as one batch, and the check that then escapes."""


VERSIONS = (6, 5, 4)
"""The contract versions the fixtures speak, in the order an image is asked:
exec's and compile@v3's first, then the harness's own, then the one the
pinned releases speak."""
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


def batch_key(version: int) -> str:
    """What a batch item is keyed by: `run` in 6, `test` in 5 and `id` in 4."""
    return {6: "run", 5: "test"}.get(version, "id")


def item_key(attack: str, version: int) -> str:
    """The batch key of an attack: a test id, `<group>/<test>`, in 5, and
    the attack's name, a run id, in 6 and 4."""
    return f"escape/{attack}" if version == 5 else attack


def batch_inputs(version: int, only: tuple[str, ...] = ()) -> dict[str, Any]:
    """The run primitive's inputs.json in `version`: one item per attack,
    or per attack of `only`, in order. From 6 the program is `program` and
    its input `stdin`; before, `binary` and `input`.
    """
    program, stdin = ("program", "stdin") if version >= 6 else ("binary", "input")
    return {
        "schema_version": version,
        "batch": [
            {
                batch_key(version): item_key(attack, version),
                "inputs": {
                    program: {"file": "in/binary"},
                    stdin: {"file": f"in/{attack}"},
                    "time_limit": 1,
                    "memory_limit": MEMORY_MB.get(attack, 64),
                },
            }
            for attack in (only or attacks(version))
        ],
    }


def programs_inputs() -> dict[str, Any]:
    """The programs form's inputs.json: the fixture as two programs linked both ways,
    the entrant and its partner, the partner given `secret` as a named file
    and a manifest, as `unicon/interactive@v1` gives its judge the hidden
    input, and a longer time limit, so the entrant ends first."""
    binary = {"file": "in/binary"}
    return {
        "schema_version": 6,
        "batch": [
            {
                "run": "partner-files",
                "inputs": {
                    "programs": {
                        "programs": {
                            "entrant": {
                                "program": binary,
                                "time_limit": 1,
                                "memory_limit": 64,
                            },
                            "partner": {
                                "program": binary,
                                "args": "partner",
                                "files": {"files": {"secret": {"file": "in/secret"}}},
                                "manifest": {"file": "in/manifest.json"},
                                "time_limit": 2,
                                "memory_limit": 64,
                            },
                        }
                    },
                    "links": ["partner -> entrant", "entrant -> partner"],
                },
            }
        ],
    }


def by_attack(outputs: dict[str, Any], version: int) -> dict[str, dict[str, Any]]:
    """A batch's outputs by the attack each item answers for."""
    key = batch_key(version)
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


BATCHES = {
    "batch": (),
    "container-open": ("network", "sockets", "fork"),
    **{removed: only for removed, (_, _, only, _) in EXEC_REMOVALS.items()},
}
"""The run primitive's step directories, each with the attacks of its batch:
every attack, or those named."""


def step_directory(name: str) -> str:
    """The step directory of a batch, a plain name."""
    return name.replace(" ", "-").lower()


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
        self.variants: dict[str, str] = {}
        files: dict[str, bytes | str] = {
            f"unicon-steps/compile/{SOURCE_FOLDER}/escape.c": SOURCE,
            "unicon-steps/direct/in/1.ans": "the answer\n",
            "unicon-steps/programs/inputs-6.json": json.dumps(programs_inputs()),
            "unicon-steps/programs/in/secret": "the hidden input\n",
            "unicon-steps/programs/in/manifest.json": json.dumps({"reports": {}}),
        }
        for version in VERSIONS:
            files[f"unicon-steps/compile/inputs-{version}.json"] = json.dumps(
                compile_inputs(version)
            )
            for name, only in BATCHES.items():
                directory = step_directory(name)
                files[f"unicon-steps/{directory}/inputs-{version}.json"] = json.dumps(
                    batch_inputs(version, only)
                )
        for name in BATCHES:
            for attack in ATTACKS | EXEC_ATTACKS:
                path = f"unicon-steps/{step_directory(name)}/in/{attack}"
                files[path] = f"{attack}\n"
        self.volume = made.volume("escape", files)
        self.workspace = Workspace(
            volume=self.volume, mount=MOUNT, user=f"{STEP_UID}:{STEP_UID}"
        )
        self._in_volume(f"chown -R {STEP_UID}:{STEP_UID} /v/unicon-steps")
        # Writable by anyone, so the stdin attack shows exec takes that away.
        self._in_volume("chmod 0666 /v/unicon-steps/*/in/stdin")

    def build(self) -> None:
        code, printed, outputs = self._speaking(COMPILE_IMAGE, "compile")
        assert code == 0 and outputs["outputs"]["outcome"] == "accepted", (
            printed,
            outputs,
        )
        made = outputs["outputs"]
        binary = (made["program"] if "program" in made else made["binary"])["file"]
        copies = [f"{step_directory(name)}/in/binary" for name in BATCHES]
        copies += ["programs/in/binary", "direct/escape"]
        self._in_volume(
            "cd /v/unicon-steps"
            + "".join(f" && cp compile/{binary} {copy}" for copy in copies)
            + f" && chmod 0755 {' '.join(copies)}"
            + f" && chown {STEP_UID}:{STEP_UID} {' '.join(copies)}"
        )

    def through_run(
        self,
        name: str = "batch",
        image: str = RUN_IMAGE,
        change: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """The run primitive's outputs for batch `name` by attack, and what
        each attack printed, read at the path the primitive reports for it,
        under the harness's body, in the shape exec runs in when the run
        image is exec, changed by `change`.
        """

        def changed(body: dict[str, Any]) -> None:
            if RUN_IS_EXEC:
                as_exec_runs(
                    body, step_memory_mb(BATCHES[name] or ATTACKS | EXEC_ATTACKS)
                )
            if change is not None:
                change(body)

        directory = step_directory(name)
        code, printed, outputs = self._speaking(image, directory, changed)
        assert code == 0 and "batch" in outputs, (printed, outputs)
        found = by_attack(outputs, self.versions[directory])
        paths = {attack: item["output"]["file"] for attack, item in found.items()}
        texts: dict[str, str] = json.loads(
            self._in_volume(
                READ_OUTPUTS,
                f"/v/unicon-steps/{directory}",
                json.dumps(paths),
                python=True,
            )
        )
        return found, texts

    def through_programs(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """The programs form's outputs for its one item, run in the shape
        its step runs in, and the facts of each program."""
        code, printed, outputs = self._speaking(
            os.environ["EXEC_IMAGE"],
            "programs",
            lambda body: as_exec_runs(
                body, PROGRAMS_MEMORY_MB, "/usr/local/bin/exec"
            ),
            versions=(6,),
        )
        assert code == 0 and "batch" in outputs, (printed, outputs)
        found: dict[str, Any] = outputs["batch"][0]["outputs"]
        return found, self.read_json(f"programs/{found['facts']['file']}")

    def without(self, removed: str) -> str:
        """exec's image with the protection `removed` left out, built once."""
        if removed not in self.variants:
            changes, _, _, _ = EXEC_REMOVALS[removed]
            tag = f"unicon-escape-{step_directory(removed)}:{self.made.suffix}"
            with tempfile.TemporaryDirectory() as context:
                (Path(context) / "changes.json").write_text(json.dumps(changes))
                docker(
                    "build",
                    "-q",
                    "-t",
                    tag,
                    "-f",
                    "-",
                    context,
                    stdin=CHANGED_EXEC.format(image=RUN_IMAGE).encode(),
                )
            self.variants[removed] = tag
        return self.variants[removed]

    def close(self) -> None:
        """Remove the images made without a protection."""
        for tag in self.variants.values():
            docker("image", "rm", "-f", tag, check=False)

    def alone(
        self,
        attack: str,
        change: Callable[[dict[str, Any]], None] | None = None,
    ) -> Ran:
        """The fixture as the container's own process, under the harness's body
        changed by `change`, killed by the test after DIRECT_SECONDS.
        """
        body = self._body(RUN_IMAGE, "direct")
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
        self,
        image: str,
        directory: str,
        change: Callable[[dict[str, Any]], None] | None = None,
        versions: tuple[int, ...] = VERSIONS,
    ) -> tuple[int | None, str, dict[str, Any]]:
        """Run `image` over a step directory, under the harness's body changed
        by `change`, in the first version it does not refuse as another
        contract version, and remember that version.
        """
        for version in versions:
            self._in_volume(
                f"cd /v/unicon-steps/{directory}"
                f" && cp inputs-{version}.json inputs.json"
                " && rm -f outputs.json"
                f" && chown {STEP_UID}:{STEP_UID} inputs.json"
            )
            body = self._body(image, directory)
            if change is not None:
                change(body)
            code, printed = self._run(body, 300)
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
    _have(RUN_IMAGE)
    client = DockerClient(daemon_socket())
    try:
        with lab() as made:
            ready = Fixtures(made, client)
            try:
                ready.build()
                yield ready
            finally:
                ready.close()
    finally:
        client.close()


@pytest.mark.docker
@pytest.mark.skipif(not docker_ready(), reason="needs a Docker daemon")
def test_through_the_run_primitive_every_attack_is_refused(
    fixtures: Fixtures,
) -> None:
    outputs, printed = fixtures.through_run()
    outcomes = {attack: item["outcome"] for attack, item in outputs.items()}
    allowed = machines_own() if RUN_IS_EXEC else set()

    assert outcomes["memory"] == "memory_limit"
    assert outcomes["spin"] == "time_limit"
    assert outcomes["sleep"] == "time_limit"
    assert outcomes["output"] == "output_limit"
    expected_of = attacks(fixtures.versions["batch"])
    if RUN_IS_EXEC:
        # A box's files are its memory, and its tmpfs is no smaller than its
        # memory limit, so filling the disk ends at that limit.
        assert outcomes["disk"] == "memory_limit"
        expected_of["disk"] = set()
    for attack, expected in expected_of.items():
        checks = checks_of(printed.get(attack, ""))
        escaped = {n for n, (verdict, _) in checks.items() if verdict == "ESCAPED"}
        if RUN_IS_EXEC:
            escaped -= BOXES_OWN | allowed
        assert escaped == set(), (attack, {n: checks[n] for n in escaped})
        assert expected <= checks.keys(), (attack, outcomes[attack], sorted(checks))
    # The mark attack tests something only if the run could set the mark, on
    # its own copy of the program.
    if "mark" in printed:
        assert "mark INFO /proc/self/exe: marked" in printed["mark"], printed["mark"]
    # Landlock, or exec's box, keeps the program out of /work: not even the
    # step's own directory is written.
    files = checks_of(printed["files"])
    assert files["write-work-root"][0] == "REFUSED"


@pytest.mark.docker
@pytest.mark.skipif(not docker_ready(), reason="needs a Docker daemon")
def test_through_exec_with_the_container_open_its_boxes_hold(
    fixtures: Fixtures,
) -> None:
    """exec's boxes, not its container, keep a run off the network and its
    processes down: with the container's network left on, as a Woodpecker
    step may have it, and its process limit off, no connection goes out, the
    box sees no interface but its own namespace's, no ring is made and the
    fork bomb stops. The removals below show the network was on."""
    if not RUN_IS_EXEC:
        pytest.skip("the run image is not exec")
    _, printed = fixtures.through_run("container-open", change=without_pids_or_network)
    for attack in ("network", "sockets", "fork"):
        checks = checks_of(printed[attack])
        escaped = {n for n, (verdict, _) in checks.items() if verdict == "ESCAPED"}
        assert escaped == set(), (attack, checks)
        assert (ATTACKS | EXEC_ATTACKS)[attack] <= checks.keys(), (attack, checks)


@pytest.mark.docker
@pytest.mark.skipif(not docker_ready(), reason="needs a Docker daemon")
def test_the_container_alone_refuses_every_attack_but_its_own_work(
    fixtures: Fixtures,
) -> None:
    # Filling the disk and a huge output are left out: alone, the program's
    # writable place is the step's /work, which nothing limits yet, so they
    # would only write the fixture's cap. The endless sleep is left out
    # because only the caller's clock stops it, as the harness's does.
    # exec's own attacks are left out too: what refuses them is exec's.
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


@pytest.mark.docker
@pytest.mark.skipif(not docker_ready(), reason="needs a Docker daemon")
@pytest.mark.parametrize("removed", list(EXEC_REMOVALS))
def test_taking_away_one_of_execs_protections_lets_an_attack_through(
    fixtures: Fixtures, removed: str
) -> None:
    if not RUN_IS_EXEC:
        pytest.skip("the run image is not exec")
    _, change, attacks_run, check = EXEC_REMOVALS[removed]
    image = fixtures.without(removed)
    outputs, printed = fixtures.through_run(removed, image=image, change=change)
    escaped = {
        name.rstrip("+")
        for attack in attacks_run
        for name, (verdict, _) in checks_of(printed[attack]).items()
        if verdict == "ESCAPED"
    }
    outcomes = {attack: item["outcome"] for attack, item in outputs.items()}
    assert check in escaped, (removed, outcomes, printed)


@pytest.mark.docker
@pytest.mark.skipif(not docker_ready(), reason="needs a Docker daemon")
def test_through_the_programs_form_the_entrant_reaches_none_of_its_partner(
    fixtures: Fixtures,
) -> None:
    """The partner holds a hidden file open in its own directory, leaves a
    file in its `/tmp` and tells the entrant where it is; the entrant
    reaches none of it and sees no process of the partner's, and the
    partner reports what it tried as its message."""
    if not os.environ.get("EXEC_IMAGE"):
        pytest.skip("no exec image; set EXEC_IMAGE")
    _have(os.environ["EXEC_IMAGE"])
    outputs, facts = fixtures.through_programs()
    assert outputs["outcome"] == "accepted", outputs
    assert facts["entrant"]["ended"] == "finished", facts
    assert facts["partner"]["verdict"] == "0", facts
    checks = checks_of(facts["partner"]["message"])
    escaped = {n for n, (verdict, _) in checks.items() if verdict == "ESCAPED"}
    assert escaped == set(), {n: checks[n] for n in escaped}
    assert PARTNER_CHECKS <= checks.keys(), sorted(checks)


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


def test_a_step_has_a_cgroup_namespace_of_its_own_whatever_the_daemon_default() -> None:
    """sandbox-run measures a run's memory by its container's cgroup, which it
    finds as the root of a private cgroup namespace.
    """
    step = _step("image")
    workspace = Workspace(volume="v", mount=MOUNT, user="10001:10001")
    body = create_body(step, workspace, MOUNT / "unicon-steps" / "x", "g")
    assert body["HostConfig"]["CgroupnsMode"] == "private"


def test_the_version_6_documents_keep_the_primitive_contract() -> None:
    assert violation(compile_inputs(6), "primitive", "inputs_file") is None
    assert violation(batch_inputs(6), "primitive", "inputs_file") is None
    assert violation(programs_inputs(), "primitive", "inputs_file") is None
    items = batch_inputs(6)["batch"]
    assert [item["run"] for item in items] == list(ATTACKS | EXEC_ATTACKS)
    assert set(items[0]["inputs"]) == {"program", "stdin", "time_limit", "memory_limit"}
    for name, only in BATCHES.items():
        assert violation(batch_inputs(6, only), "primitive", "inputs_file") is None, (
            name
        )


def test_exec_reads_the_programs_document(tmp_path: Path) -> None:
    """The document is one exec's programs form takes, read by its own
    `read_inputs` from a primitive-exec checkout, which checks more than
    the schema: the programs' inputs and the links between them."""
    source = EXEC_SOURCE / "src" / "exec.py"
    if not source.is_file():
        pytest.skip(f"no primitive-exec checkout at {EXEC_SOURCE}")
    spec = importlib.util.spec_from_file_location("exec", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "binary").write_bytes(b"")
    (tmp_path / "in" / "secret").write_text("the hidden input\n")
    (tmp_path / "in" / "manifest.json").write_text(json.dumps({"reports": {}}))
    (tmp_path / "inputs.json").write_text(json.dumps(programs_inputs()))
    [item] = module.read_inputs(tmp_path)
    entrant, partner = item.programs
    assert (entrant.name, partner.name) == ("entrant", "partner")
    assert partner.args == ("partner",)
    assert set(partner.files) == {"secret"}
    assert partner.manifest == {}
    assert partner.time_limit > entrant.time_limit
    assert module.pipe_names(item) == {"entrant": [], "partner": []}


def test_exec_s_attacks_leave_before_the_next_item_finds() -> None:
    """Each attack that leaves something runs just before the one that looks
    for it."""
    order = list(attacks(6))
    assert order.index("find-sysv") == order.index("leave-sysv") + 1
    assert order.index("read-mark") == order.index("mark") + 1
    assert list(attacks(5)) == list(ATTACKS)


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
    key = batch_key(version)
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
    assert list(found) == list(attacks(version))
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

    assert json.loads(printed) == {
        attack: f"{attack} said\n" for attack in attacks(version)
    }


def test_an_image_of_another_contract_version_is_told_apart() -> None:
    released = {
        "schema_version": 4,
        "error": "inputs.json is not written against contract version 4",
    }
    assert another_version(released)
    assert not another_version({"schema_version": 5, "error": "the disk broke"})
    assert not another_version({"schema_version": 5, "outputs": {"outcome": "x"}})
