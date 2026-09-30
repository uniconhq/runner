"""Where step containers get their working directory, and the host-path trap.

The Docker daemon resolves a mount's source on the machine, not inside the
harness, so a directory the harness made at its own /woodpecker/x means
nothing to the daemon: bind-mounting it gives the step an empty directory and
a silently wrong verdict. The harness therefore asks the daemon, through the
socket filter, how its own container is mounted, finds the volume the CI put
the checkouts in, and gives each step a directory of that volume as a volume
subpath mount at /work. A volume name and a subpath are something the socket
filter can check against the calling container's own mounts; a host path is
not.

Step directories live under `unicon-steps/` in that volume, one per container,
beside the two checkouts and never inside them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from unicon_harness.docker import Docker, DockerError
from unicon_harness.faults import GradingError

STEPS_DIR = "unicon-steps"
STEP_UID = 10001
"""The user every step runs as, and the user the harness becomes before it reads
anything a run supplies. It owns the step directories, so a step can write its
outputs and the harness can read them back.
"""

MOUNTINFO = Path("/proc/self/mountinfo")
CONTAINER_FILE = re.compile(
    r"/containers/([0-9a-f]{64})/(?:hostname|hosts|resolv\.conf)\s"
)


@dataclass(frozen=True)
class Workspace:
    """The volume the checkouts are in, where the harness sees it, and the user
    steps run as.
    """

    volume: str
    mount: Path
    user: str

    @property
    def steps(self) -> Path:
        return self.mount / STEPS_DIR

    def new_step_dir(self, name: str) -> Path:
        directory = self.steps / name
        try:
            directory.mkdir()
            (directory / "in").mkdir()
            (directory / "out").mkdir()
        except OSError as exc:
            raise GradingError(
                f"could not make the step directory {directory}: {exc.strerror}"
            ) from None
        return directory

    def subpath(self, directory: Path) -> str:
        """The volume subpath of a step directory, `unicon-steps/<name>`."""
        return directory.relative_to(self.mount).as_posix()


def own_container_id(mountinfo: str) -> str:
    """This container's id, from the files Docker bind-mounts into every
    container. The hostname is only a 12-character prefix, and the socket
    filter takes containers by full id.
    """
    found = CONTAINER_FILE.search(mountinfo)
    if found is None:
        raise GradingError(
            "the harness could not find its own container id; it must run as a "
            "Docker container"
        )
    return found.group(1)


def locate(docker: Docker, task_checkout: Path, mountinfo: str) -> tuple[str, Path]:
    """The volume holding the task checkout and where it is mounted here."""
    container_id = own_container_id(mountinfo)
    try:
        mounts = docker.inspect(container_id).get("Mounts") or []
    except DockerError as exc:
        raise GradingError(
            f"the harness could not inspect its own container: {exc.reason}"
        ) from None
    holding = [
        (Path(str(mount.get("Destination", ""))), mount)
        for mount in mounts
        if isinstance(mount, dict)
        and Path(str(mount.get("Destination", ""))).is_absolute()
        and task_checkout.is_relative_to(Path(str(mount["Destination"])))
    ]
    if not holding:
        raise GradingError(
            f"the task checkout {task_checkout} is not on a volume of this container"
        )
    destination, mount = max(holding, key=lambda pair: len(pair[0].parts))
    if mount.get("Type") != "volume" or not mount.get("Name"):
        raise GradingError(
            f"the task checkout is on a {mount.get('Type')} mount at {destination}; "
            "steps can only be given a directory of a Docker volume"
        )
    return str(mount["Name"]), destination


def prepare(volume: str, mount: Path) -> Workspace:
    """Make `unicon-steps/` and settle who the harness is from here on.

    The CI's workspace belongs to root, because the clone steps run as root, so
    the harness image starts as root to make its directory, hands it to
    STEP_UID and becomes STEP_UID for the rest of the run. Started as anyone
    else, it stays who it is and steps run as the same user.
    """
    steps = mount / STEPS_DIR
    try:
        steps.mkdir(exist_ok=True)
    except OSError as exc:
        raise GradingError(
            f"could not make {steps} in the run's workspace: {exc.strerror}"
        ) from None
    if not hasattr(os, "geteuid"):
        return Workspace(volume, mount, f"{STEP_UID}:{STEP_UID}")
    if os.geteuid() == 0:
        try:
            os.chown(steps, STEP_UID, STEP_UID)
            os.setgroups([])
            os.setgid(STEP_UID)
            os.setuid(STEP_UID)
        except OSError as exc:
            raise GradingError(f"could not give up root: {exc.strerror}") from None
    uid, gid = os.getuid(), os.getgid()
    if uid == 0 or gid == 0:
        raise GradingError("the harness would run steps as root, which it never does")
    return Workspace(volume, mount, f"{uid}:{gid}")


def read_mountinfo() -> str:
    try:
        return MOUNTINFO.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
