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
import subprocess
import time
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
    head = _shell([f"{source}:/src"], "cat /src/head").strip()
    name, workspace = _start_clone(made, what, network, source, cache, head)
    code = made.wait(name)
    assert code == 0, _log(name)
    return workspace


def _start_clone(
    made: Lab, what: str, network: str, source: str, cache: str, head: str
) -> tuple[str, str]:
    """Start a checkout of commit `head` and leave it running. Returns the
    container and its workspace volume.
    """
    workspace = made.volume(what)
    name = made.run(
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
    return name, workspace


def _server(made: Lab, network: str, source: str, hold_seconds: float = 0) -> str:
    """The stand-in git-lfs server, reachable as http://lfs:8080 on `network`,
    serving the objects MAKE_REPO left in the source volume. Returns once it
    is listening: a checkout that asked any sooner would be refused.
    """
    code = made.volume("lfs-code", {"lfs_server.py": SERVER})
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
        str(hold_seconds),
    )
    for _ in range(100):
        if '{"listening"' in docker("logs", server, check=False):
            return server
        time.sleep(0.1)
    raise AssertionError(f"the git-lfs server did not start: {_log(server)}")


def _log(container: str) -> str:
    """Everything a container printed, both streams: the clone plugin and git
    write their progress to standard error.
    """
    printed = subprocess.run(
        ["docker", "logs", container], capture_output=True, text=True, check=False
    )
    return printed.stdout + printed.stderr


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
        network = made.network("net")
        server = _server(made, network, source)
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


def test_checkouts_started_together_download_a_big_file_once() -> None:
    """The first gradings of a task on a machine whose cache does not hold its
    big file yet start together. The server holds the object back for a few
    seconds, a slow link in small, so every checkout is already asking for it
    before the first download ends: without the image's lock around the fetch
    each of them downloaded it (experiment D2, 2026-10-04, eight checkouts and
    eight downloads). With it the first downloads and the others wait, then
    copy the file out of the cache.
    """
    with lab() as made:
        source = made.volume("source")
        _shell([f"{source}:/src"], MAKE_REPO)
        network = made.network("net")
        server = _server(made, network, source, hold_seconds=5)
        cache = made.volume("lfs-cache")
        expected = _shell([f"{source}:/src"], "cat /src/data.sha").strip()
        head = _shell([f"{source}:/src"], "cat /src/head").strip()

        started = [
            _start_clone(made, f"together-{n}", network, source, cache, head)
            for n in range(4)
        ]
        codes = [made.wait(name) for name, _ in started]
        logs = [_log(name) for name, _ in started]
        checked_out = [
            _shell([f"{workspace}:/w"], "sha256sum /w/task/data.bin").split()[0]
            for _, workspace in started
        ]
        downloads = _downloads(server)

    assert codes == [0, 0, 0, 0], logs
    assert checked_out == [expected] * 4
    assert downloads == [expected], "checkouts started together each downloaded"
    # The checkouts did overlap, so the single download is the lock's doing
    # and not the checkouts happening to run one after another.
    waited = [log for log in logs if "waiting for another checkout's download" in log]
    assert waited, logs


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
        network = made.network("net")
        _server(made, network, source)
        cache = made.volume("lfs-cache")
        expected = _shell([f"{source}:/src"], "cat /src/data.sha").strip()

        workspace = _clone(made, "only", network, source, cache)

        checked_out = _shell([f"{workspace}:/w"], "sha256sum /w/task/data.bin")
        ordinary = _shell([f"{workspace}:/w"], "cat /w/task/notes.txt")

    assert checked_out.split()[0] == expected
    # A file that is not a pointer passes through the filter as it was
    # committed, which is what everything the platform writes as content is.
    assert ordinary.strip() == "an ordinary file"
