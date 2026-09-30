"""Finding the run's volume through the harness's own container, and reaching
files without being walked out of their directory.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.support import SELF_ID, VOLUME, FakeDocker, mountinfo
from unicon_harness.faults import GradingError
from unicon_harness.files import inside
from unicon_harness.workspace import STEPS_DIR, locate, own_container_id, prepare


def test_the_own_container_id_comes_from_the_hostname_mount() -> None:
    assert own_container_id(mountinfo()) == SELF_ID


def test_outside_a_container_there_is_no_id() -> None:
    with pytest.raises(GradingError, match="own container id"):
        own_container_id("812 790 0:52 / / rw - overlay overlay rw\n")


def test_the_task_checkout_is_found_on_its_volume(volume: Path) -> None:
    docker = FakeDocker(volume)

    found = locate(docker, volume / "task", mountinfo())

    assert found == (VOLUME, volume)
    assert f"inspect {SELF_ID[:12]}" in docker.calls


def test_a_checkout_on_a_bind_mount_is_refused(volume: Path) -> None:
    docker = FakeDocker(volume)
    docker.own_mounts = [{"Type": "bind", "Source": "/srv", "Destination": str(volume)}]

    with pytest.raises(GradingError, match="bind mount"):
        locate(docker, volume / "task", mountinfo())


def test_a_checkout_on_no_mount_is_refused(volume: Path, tmp_path: Path) -> None:
    with pytest.raises(GradingError, match="not on a volume"):
        locate(FakeDocker(volume), tmp_path / "elsewhere", mountinfo())


def test_preparing_makes_the_step_root_and_never_runs_steps_as_root(
    volume: Path,
) -> None:
    workspace = prepare(VOLUME, volume)

    assert (volume / STEPS_DIR).is_dir()
    assert not workspace.user.startswith("0:")
    step = workspace.new_step_dir("001-compile")
    assert (step / "in").is_dir() and (step / "out").is_dir()
    assert workspace.subpath(step) == f"{STEPS_DIR}/001-compile"


def test_a_step_directory_is_never_reused(volume: Path) -> None:
    workspace = prepare(VOLUME, volume)
    workspace.new_step_dir("001-compile")

    with pytest.raises(GradingError, match="step directory"):
        workspace.new_step_dir("001-compile")


@pytest.mark.parametrize("relative", ["../x", "/etc/passwd", "a//b", "a/./b", ""])
def test_a_path_that_is_not_plain_is_refused(tmp_path: Path, relative: str) -> None:
    with pytest.raises(GradingError, match="not a plain relative path"):
        inside(tmp_path, relative, "the file")


def test_a_directory_is_not_a_file(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    with pytest.raises(GradingError, match="not a regular file"):
        inside(tmp_path, "d", "the file")


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
def test_a_link_anywhere_on_the_way_is_refused(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(GradingError, match="symbolic link"):
        inside(tmp_path, "link/f", "the file")
