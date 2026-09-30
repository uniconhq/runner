"""The clone image on a real Docker, as the CI's checkout step runs it: a task
repo whose big file is in git-lfs is checked out with the file's real bytes,
the object lands in the /lfs-cache volume, a second checkout into another
workspace with the same cache downloads nothing, and a checkout with another
cache volume, one Docker makes on first use as the CI's answer names one per
org, is served nothing from the first and downloads the file itself. Run with
`pytest -m docker`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from filter_tests.lab import PYTHON, Lab, docker, docker_ready, image, lab

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_ready(), reason="needs a Docker daemon"),
]

SERVER = (Path(__file__).parent / "lfs_server.py").read_text(encoding="utf-8")

MAKE_REPO = """
set -e
export GIT_CONFIG_NOSYSTEM=1
git config --global user.email lab@example.invalid
git config --global user.name lab
git config --global init.defaultBranch main
mkdir -p /tmp/w /src/lfs && cd /tmp/w
git init -q
git lfs install --local >/dev/null
git lfs track '*.bin' >/dev/null
printf '[lfs]\\n\\turl = http://lfs:8080/\\n' > .lfsconfig
head -c 200000 /dev/urandom > data.bin
git add .gitattributes .lfsconfig data.bin
git commit -qm first
cp $(find .git/lfs/objects -type f) /src/lfs/
git init -q --bare /src/repo.git
git tag -a published/1 -m one
git push -q --no-verify file:///src/repo.git main published/1
sha256sum data.bin | cut -d' ' -f1 > /src/data.sha
git rev-parse HEAD > /src/head
"""


def _shell(volumes: list[str], script: str) -> str:
    mounts = [flag for volume in volumes for flag in ("-v", volume)]
    return docker(
        "run", "--rm", *mounts, "--entrypoint", "sh", image("clone"), "-c", script
    )


def _clone(made: Lab, what: str, network: str, source: str, cache: str) -> str:
    """Check the task out into a fresh workspace volume with the clone image,
    the way the forge's CI answer sets its clone step. Returns the volume.
    """
    workspace = made.volume(what)
    head = _shell([f"{source}:/src"], "cat /src/head").strip()
    made.run(
        what,
        "--network",
        network,
        "-v",
        f"{source}:/src:ro",
        "-v",
        f"{workspace}:/woodpecker",
        "-v",
        f"{cache}:/lfs-cache",
        "-e",
        "PLUGIN_REMOTE=file:///src/repo.git",
        "-e",
        f"PLUGIN_SHA={head}",
        "-e",
        "PLUGIN_REF=refs/tags/published/1",
        "-e",
        "PLUGIN_PATH=/woodpecker/task",
        "-e",
        "PLUGIN_LFS=true",
        image("clone"),
    )
    code = made.wait(made.name(what))
    assert code == 0, docker("logs", made.name(what), check=False)
    return workspace


def _downloads(server: str) -> list[str]:
    return [
        json.loads(line)["download"]
        for line in docker("logs", server).splitlines()
        if line.startswith('{"download"')
    ]


def test_big_files_are_checked_out_and_downloaded_once_per_machine() -> None:
    with lab() as made:
        source = made.volume("source")
        _shell([f"{source}:/src"], MAKE_REPO)
        code = made.volume("lfs-code", {"lfs_server.py": SERVER})
        network = made.network("net")
        server = made.run(
            "lfs",
            "--network",
            network,
            "--network-alias",
            "lfs",
            "-v",
            f"{source}:/src:ro",
            "-v",
            f"{code}:/code:ro",
            PYTHON,
            "python",
            "/code/lfs_server.py",
            "8080",
            "/src/lfs",
            "http://lfs:8080",
        )
        cache = made.volume("lfs-cache")
        expected = _shell([f"{source}:/src"], "cat /src/data.sha").strip()

        first = _clone(made, "first", network, source, cache)
        after_first = _downloads(server)
        checked_out = _shell([f"{first}:/w"], "sha256sum /w/task/data.bin")
        cached = _shell([f"{cache}:/c"], "find /c/objects -type f")

        second = _clone(made, "second", network, source, cache)
        after_second = _downloads(server)
        again = _shell([f"{second}:/w"], "sha256sum /w/task/data.bin")

        other_cache = made.name("lfs-other-org")
        made.volumes.append(other_cache)
        third = _clone(made, "third", network, source, other_cache)
        after_third = _downloads(server)
        elsewhere = _shell([f"{third}:/w"], "sha256sum /w/task/data.bin")

    assert checked_out.split()[0] == expected
    assert cached.strip().endswith(f"/{expected}")
    assert after_first == [expected]
    assert again.split()[0] == expected
    assert after_second == [expected], "the second checkout downloaded again"
    assert elsewhere.split()[0] == expected
    assert after_third == [expected, expected], "another cache was served the file"


def test_the_clone_image_runs_as_root() -> None:
    """So a cache volume Docker makes on first use, which belongs to root, is
    one the checkout can write to, whatever the CI's answer names it.
    """
    assert _shell([], "id -u").strip() == "0"
