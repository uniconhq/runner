"""The docker CLI for the image tests: whether a daemon answers, a command
run through it, and the repo's images built once.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]


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


def docker(*args: str, check: bool = True) -> str:
    finished = subprocess.run(["docker", *args], capture_output=True, check=False)
    if check and finished.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args[:3])} failed: {finished.stderr.decode()[-2000:]}"
        )
    return finished.stdout.decode()


@cache
def image(name: str) -> str:
    """The repo's image `name`, such as primitive-base, built once."""
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
