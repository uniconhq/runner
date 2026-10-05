"""One sandboxed container per step, started through the socket filter.

Every step container, whether or not it runs contestant code, gets the same
set: no network, a read-only root, every capability dropped,
no-new-privileges, Docker's built-in seccomp profile named explicitly (a
daemon's own default can be unconfined, and under the rest of the set alone
contestant code became root in a user namespace of its own), a non-root user,
no swap, and the plan's memory, CPU, pids and wall-clock limits. The socket
filter refuses a create that carries less, so the set is enforced on the
machine and not only here.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from unicon_harness.docker import Docker, DockerError
from unicon_harness.faults import GradingError
from unicon_harness.plan import Step
from unicon_harness.runlog import RunLog
from unicon_harness.workspace import Workspace

GRADING_LABEL = "unicon.grading"
STEP_LABEL = "unicon.step"
TIME_LABEL = "unicon.time_ms"
"""The container's wall clock, which the socket filter's reaper holds it to."""

SECURITY_OPTIONS = ("no-new-privileges", "seccomp=builtin")
CPUS = 1
TMP_LIMIT_MB = 256
LOG_DRIVER = {"Type": "json-file", "Config": {"max-size": "8m", "max-file": "1"}}
LOG_LIMIT_BYTES = 256 * 1024
KILL_WAIT_SECONDS = 15.0
MIB = 1024 * 1024

Killed = Literal["time", "run_clock"]


@dataclass(frozen=True)
class Ended:
    """How a container finished. `killed` is set when the harness killed it: at
    the step's own time limit, or at the run's wall clock. `out_of_memory` is
    Docker's word that the kernel killed something in it at the memory limit.
    """

    exit_code: int | None
    killed: Killed | None
    out_of_memory: bool
    elapsed_ms: int


def create_body(
    step: Step, workspace: Workspace, directory: Path, grading_id: str
) -> dict[str, Any]:
    limits = step.limits
    memory = limits.memory_mb * MIB
    cpu_seconds = math.ceil(limits.cpu_ms / 1000)
    file_bytes = limits.output_mb * MIB
    return {
        "Image": step.image,
        "User": workspace.user,
        "WorkingDir": "/work",
        "Env": ["HOME=/tmp"],
        "Labels": {
            GRADING_LABEL: grading_id,
            STEP_LABEL: step.id,
            TIME_LABEL: str(limits.time_ms),
        },
        "NetworkDisabled": True,
        "HostConfig": {
            "NetworkMode": "none",
            # The step's own program is the container's first process, which
            # the program it runs cannot signal, whatever the daemon's
            # default init; and no /dev/shm, so /work and /tmp are the only
            # places a step writes.
            "Init": False,
            "IpcMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": list(SECURITY_OPTIONS),
            "Memory": memory,
            "MemorySwap": memory,
            "NanoCpus": CPUS * 1_000_000_000,
            "PidsLimit": limits.pids,
            "Ulimits": [
                {"Name": "cpu", "Soft": cpu_seconds, "Hard": cpu_seconds},
                {"Name": "fsize", "Soft": file_bytes, "Hard": file_bytes},
            ],
            "Mounts": [
                {
                    "Type": "volume",
                    "Source": workspace.volume,
                    "Target": "/work",
                    "VolumeOptions": {
                        "Subpath": workspace.subpath(directory),
                        "NoCopy": True,
                    },
                },
                {
                    "Type": "tmpfs",
                    "Target": "/tmp",
                    "TmpfsOptions": {
                        "SizeBytes": min(limits.memory_mb, TMP_LIMIT_MB) * MIB,
                        "Mode": 0o1777,
                    },
                },
            ],
            "LogConfig": LOG_DRIVER,
        },
    }


class Sandbox:
    """Runs step containers and remembers the ones still on the machine, so a
    run that fails half way leaves nothing behind.
    """

    def __init__(
        self,
        docker: Docker,
        workspace: Workspace,
        grading_id: str,
        log: RunLog,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._docker = docker
        self._workspace = workspace
        self._grading_id = grading_id
        self._log = log
        self._clock = clock
        self._live: list[str] = []

    @property
    def clock(self) -> Callable[[], float]:
        return self._clock

    def run(self, step: Step, directory: Path, run_ends: float) -> Ended:
        """Create, start and wait on one container, kill it at whichever comes
        first of the step's time limit and the run's wall clock (`run_ends`, on
        the sandbox's clock), then read its log into the run log and remove it.
        """
        body = create_body(step, self._workspace, directory, self._grading_id)
        container = self._call(
            f"create the container for step {step.label}",
            lambda: self._docker.create(body),
        )
        self._live.append(container)
        self._call(f"start step {step.label}", lambda: self._docker.start(container))
        began = self._clock()
        limit = step.limits.time_ms / 1000
        allowed = min(limit, run_ends - began)
        exit_code = self._call(
            f"wait on step {step.label}", lambda: self._docker.wait(container, allowed)
        )
        killed: Killed | None = None
        if exit_code is None:
            killed = "time" if allowed >= limit else "run_clock"
            self._call(f"kill step {step.label}", lambda: self._docker.kill(container))
            exit_code = self._call(
                f"wait on step {step.label}",
                lambda: self._docker.wait(container, KILL_WAIT_SECONDS),
            )
        elapsed_ms = round((self._clock() - began) * 1000)
        state = (
            self._call(
                f"inspect step {step.label}", lambda: self._docker.inspect(container)
            ).get("State")
            or {}
        )
        try:
            printed = self._docker.logs(container, LOG_LIMIT_BYTES)
        except DockerError as exc:
            printed = f"(the log could not be read: {exc.reason})"
        self._log.block(f"step {step.label} printed", printed or "(nothing)")
        self.remove(container)
        return Ended(
            exit_code=exit_code,
            killed=killed,
            out_of_memory=bool(state.get("OOMKilled")),
            elapsed_ms=elapsed_ms,
        )

    def remove(self, container: str) -> None:
        self._call("remove a step container", lambda: self._docker.remove(container))
        if container in self._live:
            self._live.remove(container)

    def clean_up(self) -> None:
        """Remove every container this run still has. Called in a finally, so it
        must not raise: what it cannot remove the socket filter's reaper will.
        """
        for container in list(self._live):
            try:
                self._docker.remove(container)
            except DockerError as exc:
                self._log.event(
                    f"could not remove container {container[:12]}: {exc.reason}"
                )
            self._live.remove(container)

    def _call[T](self, doing: str, call: Callable[[], T]) -> T:
        try:
            return call()
        except DockerError as exc:
            raise GradingError(f"could not {doing}: {exc.reason}") from None
