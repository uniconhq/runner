"""Who is on the other end of a connection.

Every harness on the machine shares the filter's socket, so the filter must
tell one grading run from another by more than what a request says about
itself. The kernel knows which process connected; the filter runs in the
machine's pid namespace (`pid: host`), so that process is visible to it, and
its cgroup names the container it runs in. The daemon then says what that
container is: its UNICON_GRADING_ID, and the volume it has at the workspace
path, the one volume a step of that run may mount.

The process is held by a pidfd from the moment of the question, SO_PEERPIDFD
where the kernel has it (Linux 6.5), so a process that exits and has its pid
reused cannot hand its identity to another; the cgroup read is trusted only if
the pidfd still refers to a live process afterwards. On older kernels the pid
comes from SO_PEERCRED and the pidfd is opened from it, which leaves a short
window between the two in which a pid could be reused.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from unicon_filter.policy import Caller, normal_grading

SO_PEERPIDFD = 77
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
NAMES_AN_ID = re.compile(r"(?:^|[^0-9a-f])[0-9a-f]{64}(?:$|[^0-9a-f])")
SYSTEMD_SCOPE = re.compile(r"docker-([0-9a-f]{64})\.scope")
SYSTEMD_PARENT = re.compile(r"[^/]+\.(?:slice|service)")
GRADING_VARIABLE = "UNICON_GRADING_ID"

Inspect = Callable[[str], Awaitable[dict[str, Any] | None]]


def container_of(cgroup: str) -> str | None:
    """The container id in a /proc/<pid>/cgroup file, or None.

    A container's processes sit in its cgroup and nowhere below it, so a path
    is taken only in the shapes Docker makes, with the id as the last segment:
    `/docker/<id>` under cgroupfs (Docker Desktop among them), `docker-<id>.scope`
    under systemd slices, and either one seen from the filter's own cgroup
    namespace, which starts with `..` (`/../<id>`, `/../docker-<id>.scope`).
    A path that goes deeper, or names an id anywhere else, is a cgroup the
    caller could have made itself and named after another container, and
    names none. Under cgroup v1 every controller has a line; lines that name
    two different containers, or any line of the wrong shape, name none.
    """
    found: set[str] = set()
    for line in cgroup.splitlines():
        path = line.split(":", 2)[-1]
        named = _named_by(path)
        if named is None:
            if NAMES_AN_ID.search(path):
                return None
            continue
        found.add(named)
    return found.pop() if len(found) == 1 else None


def _named_by(path: str) -> str | None:
    """The container a cgroup path is, in one of the shapes Docker makes."""
    segments = [segment for segment in path.split("/") if segment]
    climbed = 0
    while climbed < len(segments) and segments[climbed] == "..":
        climbed += 1
    rest = segments[climbed:]
    if not rest:
        return None
    last = rest[-1]
    parents = rest[:-1]
    scope = SYSTEMD_SCOPE.fullmatch(last)
    if scope is not None:
        if all(SYSTEMD_PARENT.fullmatch(parent) for parent in parents):
            return scope.group(1)
        return None
    if CONTAINER_ID.fullmatch(last) and (
        parents == ["docker"] or (climbed and not parents)
    ):
        return last
    return None


def peer_process(sock: socket.socket, proc: Path) -> tuple[int, int] | None:
    """The connecting process as (pid, pidfd), or None when it is not visible
    from here. The caller closes the pidfd.
    """
    try:
        pidfd = sock.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD)
    except OSError:
        pidfd = -1
    if pidfd >= 0:
        pid = _pid_of(pidfd, proc)
        if pid is None:
            os.close(pidfd)
            return None
        return pid, pidfd
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid = struct.unpack("3i", raw)[0]
    if pid <= 0:
        return None
    try:
        return pid, os.pidfd_open(pid)
    except OSError:
        return None


def _pid_of(pidfd: int, proc: Path) -> int | None:
    try:
        info = (proc / "self" / "fdinfo" / str(pidfd)).read_text()
    except OSError:
        return None
    for line in info.splitlines():
        if line.startswith("Pid:"):
            pid = int(line.split()[1])
            return pid if pid > 0 else None
    return None


def alive(pidfd: int) -> bool:
    try:
        signal.pidfd_send_signal(pidfd, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def identify(
    sock: socket.socket, proc: Path, inspect: Inspect, workspace: str
) -> Caller:
    found = peer_process(sock, proc)
    if found is None:
        return Caller(unknown="the connecting process is not visible to the filter")
    pid, pidfd = found
    try:
        try:
            cgroup = (proc / str(pid) / "cgroup").read_text()
        except OSError:
            return Caller(unknown="the connecting process has no readable cgroup")
        if not alive(pidfd):
            return Caller(unknown="the connecting process exited")
    finally:
        os.close(pidfd)
    container = container_of(cgroup)
    if container is None:
        return Caller(unknown="the connecting process is not in a container")
    return describe(container, await inspect(container), workspace)


def describe(container: str, details: dict[str, Any] | None, workspace: str) -> Caller:
    """A caller from the daemon's own description of its container."""
    if details is None:
        return Caller(
            container, unknown="the daemon does not know the caller's container"
        )
    if not (details.get("State") or {}).get("Running"):
        return Caller(container, unknown="the caller's container is not running")
    variables = (details.get("Config") or {}).get("Env") or []
    raw = next(
        (v.split("=", 1)[1] for v in variables if v.startswith(f"{GRADING_VARIABLE}=")),
        None,
    )
    grading = normal_grading(raw)
    if grading is None:
        return Caller(
            container, unknown=f"the caller's container has no {GRADING_VARIABLE}"
        )
    volume = next(
        (
            str(m.get("Name"))
            for m in details.get("Mounts") or []
            if m.get("Destination") == workspace
            and m.get("Type") == "volume"
            and m.get("Name")
        ),
        None,
    )
    if volume is None:
        return Caller(
            container,
            grading,
            unknown=f"the caller has no volume mounted at {workspace}",
        )
    return Caller(container, grading, volume)
