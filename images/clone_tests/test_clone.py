"""The clone image on a real Docker, as the CI's checkout step runs it: a task
repo whose big file is in git-lfs is checked out with the file's real bytes,
the object lands in the /lfs-cache volume, a second checkout into another
workspace with the same cache downloads nothing, and a checkout with another
cache volume, one Docker makes on first use as the CI's answer names one per
org, is served nothing from the first and downloads the file itself.

Then the case the platform's own repositories are: one that says nothing
about its files, whose commit holds a pointer anyway. git-lfs resolves a
pointer only where an attributes file says that path is a large file, so
without the one in this image a checkout leaves the pointer's text where the
file should be and the grading of it fails as though that text had been
submitted. Run with `pytest -m docker`.
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
    # The big file is in the cache. So are copies of the repository's own
    # small files: the image reads every path as a large file, so git-lfs
    # keeps what it cleans, and a few hundred bytes each in a store that
    # exists for gigabytes is the price of no path convention being able
    # to break a checkout silently (`findings/upload-door-test.md` 7).
    assert f"/{expected}" in cached
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


MAKE_REPO_WITHOUT_ATTRIBUTES = """
set -e
export GIT_CONFIG_NOSYSTEM=1
git config --global user.email lab@example.invalid
git config --global user.name lab
git config --global init.defaultBranch main
mkdir -p /tmp/w /src/lfs && cd /tmp/w
git init -q
printf '[lfs]\\n\\turl = http://lfs:8080/\\n' > .lfsconfig
head -c 200000 /dev/urandom > /tmp/real.bin
sha256sum /tmp/real.bin | cut -d' ' -f1 > /src/data.sha
OID=$(cut -d' ' -f1 /src/data.sha)
SIZE=$(wc -c < /tmp/real.bin | tr -d ' ')
mkdir -p /src/lfs
cp /tmp/real.bin /src/lfs/$OID
{
  echo 'version https://git-lfs.github.com/spec/v1'
  echo "oid sha256:$OID"
  echo "size $SIZE"
} > data.bin
printf 'an ordinary file\\n' > notes.txt
git add .lfsconfig data.bin notes.txt
git commit -qm first
git init -q --bare /src/repo.git
git tag -a published/1 -m one
git push -q --no-verify file:///src/repo.git main published/1
git rev-parse HEAD > /src/head
"""
"""A repository the platform writes: the commit holds the pointer's own three
lines and the repository carries no `.gitattributes`, because the forge's
file API would wrap a pointer written under one into a second object."""


def test_a_pointer_resolves_although_the_repository_says_nothing_about_it() -> None:
    # The platform's repositories carry no attributes of their own, so the
    # image has to be what tells git-lfs that a pointer is one. Without this
    # the workspace gets the pointer's text where the file should be and the
    # grading fails as though that text had been submitted.
    with lab() as made:
        source = made.volume("source")
        _shell([f"{source}:/src"], MAKE_REPO_WITHOUT_ATTRIBUTES)
        code = made.volume("lfs-code", {"lfs_server.py": SERVER})
        network = made.network("net")
        made.run(
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

        workspace = _clone(made, "only", network, source, cache)

        checked_out = _shell([f"{workspace}:/w"], "sha256sum /w/task/data.bin")
        ordinary = _shell([f"{workspace}:/w"], "cat /w/task/notes.txt")

    assert checked_out.split()[0] == expected
    # A file that is not a pointer passes through the filter as it was
    # committed, which is what everything the platform writes as content is.
    assert ordinary.strip() == "an ordinary file"
