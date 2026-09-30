"""A small lab on a real Docker for the integration tests: the repo's images
built once, a registry of the lab's own to give images real digests, and
names that are the lab's alone, removed at the end.

It drives the docker CLI, so it works from any machine the CLI does, and every
program under test runs in a container, as on a grading machine.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import tarfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from io import BytesIO
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
REGISTRY_IMAGE = "registry:2.8.3"
REGISTRY_PORT = 5056
PYTHON = (
    "python:3.14-slim@sha256:"
    "cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6"
)
FIXTURE = REPO_ROOT / "harness" / "tests" / "primitives"
FILTER_UID = "10002"
"""The socket filter image's uid, which no harness or step runs as."""


def docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    found = subprocess.run(
        ["docker", "version", "--format", "{{.Server.APIVersion}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return found.returncode == 0


def docker(*args: str, stdin: bytes | None = None, check: bool = True) -> str:
    finished = subprocess.run(
        ["docker", *args], input=stdin, capture_output=True, check=False
    )
    if check and finished.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args[:3])} failed: {finished.stderr.decode()[-2000:]}"
        )
    return finished.stdout.decode()


@cache
def image(name: str) -> str:
    """The repo's image `name` (harness, socket-filter, clone), built once."""
    tag = f"unicon-{name}:lab"
    docker(
        "build",
        "-q",
        "-f",
        str(REPO_ROOT / "images" / name / "Dockerfile"),
        "-t",
        tag,
        str(REPO_ROOT),
    )
    return tag


@cache
def fixture_image() -> str:
    """The fixture primitives as an image, pushed to the lab registry so it has
    a digest, and named by it.
    """
    registry = "unicon-lab-registry"
    if not docker("ps", "-q", "--filter", f"name=^{registry}$"):
        docker("rm", "-f", registry, check=False)
        docker(
            "run",
            "-d",
            "--name",
            registry,
            "-p",
            f"127.0.0.1:{REGISTRY_PORT}:5000",
            REGISTRY_IMAGE,
        )
        time.sleep(1)
    tag = f"localhost:{REGISTRY_PORT}/unicon-lab/fixture:lab"
    docker("build", "-q", "-t", tag, str(FIXTURE))
    docker("push", "-q", tag)
    digests = json.loads(
        docker("image", "inspect", tag, "--format", "{{json .RepoDigests}}")
    )
    return str(next(d for d in digests if d.startswith(f"localhost:{REGISTRY_PORT}/")))


@cache
def socket_group() -> str:
    """The group that owns the daemon's socket inside a container."""
    return docker(
        "run", "--rm", "-v", "/var/run/docker.sock:/s", PYTHON, "stat", "-c", "%g", "/s"
    ).strip()


def tar_of(files: dict[str, bytes | str]) -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, BytesIO(data))
    return buffer.getvalue()


class Lab:
    """Names made here are removed when the lab closes, whatever happened."""

    def __init__(self) -> None:
        self.suffix = secrets.token_hex(4)
        self.containers: list[str] = []
        self.volumes: list[str] = []
        self.networks: list[str] = []
        self.gradings: list[str] = []

    def grading(self) -> str:
        """A fresh grading id, whose step containers the lab removes at the end."""
        grading = str(uuid.uuid4())
        self.gradings.append(grading)
        return grading

    def name(self, what: str) -> str:
        return f"unicon-lab-{what}-{self.suffix}"

    def volume(self, what: str, files: dict[str, bytes | str] | None = None) -> str:
        name = self.name(what)
        docker("volume", "create", name)
        self.volumes.append(name)
        if files:
            docker(
                "run",
                "--rm",
                "-i",
                "-v",
                f"{name}:/v",
                PYTHON,
                "tar",
                "-x",
                "-C",
                "/v",
                stdin=tar_of(files),
            )
        return name

    def network(self, what: str) -> str:
        name = self.name(what)
        docker("network", "create", name)
        self.networks.append(name)
        return name

    def run(self, what: str, *args: str, detach: bool = True) -> str:
        name = self.name(what)
        self.containers.append(name)
        docker("run", *(["-d"] if detach else []), "--name", name, *args)
        return name

    def filter(self, images: list[str], *extra: str) -> tuple[str, str]:
        """A socket filter on the real socket, its own socket in a new volume
        that belongs to the filter's uid, as on a grading machine. Returns
        (container, volume); a harness mounts the volume read-only.
        """
        volume = self.volume("filter")
        docker("run", "--rm", "-v", f"{volume}:/v", PYTHON, "chown", FILTER_UID, "/v")
        name = self.run(
            "filter",
            "--pid",
            "host",
            "--group-add",
            socket_group(),
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            f"{volume}:/run/unicon",
            "-e",
            f"UNICON_FILTER_IMAGES={','.join(images)}",
            *extra,
            image("socket-filter"),
        )
        for _ in range(50):
            if '"decision": "start"' in docker("logs", name, check=False):
                return name, volume
            time.sleep(0.2)
        raise AssertionError(
            f"the filter did not start: {docker('logs', name, check=False)}"
        )

    def wait(self, container: str, timeout: float = 300) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = docker(
                "inspect",
                container,
                "--format",
                "{{.State.Status}} {{.State.ExitCode}}",
            )
            status, code = state.split()
            if status == "exited":
                return int(code)
            time.sleep(0.5)
        raise AssertionError(f"{container} did not finish in {timeout} s")

    def close(self) -> None:
        for grading in self.gradings:
            for step in labelled(grading):
                docker("rm", "-f", step, check=False)
        for container in self.containers:
            docker("rm", "-f", container, check=False)
        for volume in self.volumes:
            docker("volume", "rm", "-f", volume, check=False)
        for network in self.networks:
            docker("network", "rm", network, check=False)


@contextmanager
def lab() -> Iterator[Lab]:
    made = Lab()
    try:
        yield made
    finally:
        made.close()


def labelled(grading: str) -> list[str]:
    """Containers still carrying a grading's label."""
    found = docker("ps", "-aq", "--filter", f"label=unicon.grading={grading}")
    return found.split()
