"""Telling the calling container from its cgroup and the daemon's description."""

from __future__ import annotations

from typing import Any

import pytest

from filter_tests.support import GRADING, HARNESS, WORKSPACE
from unicon_filter.peer import container_of, describe


@pytest.mark.parametrize(
    "cgroup",
    [
        f"0::/../{HARNESS}\n",
        f"0::/docker/{HARNESS}\n",
        f"0::/system.slice/docker-{HARNESS}.scope\n",
        f"0::/user.slice/user-1000.slice/user@1000.service/docker-{HARNESS}.scope\n",
        f"12:memory:/docker/{HARNESS}\n11:cpu:/docker/{HARNESS}\n",
        f"12:pids:/docker/{HARNESS}\n1:name=systemd:/docker/{HARNESS}\n0::/\n",
        f"0::/../docker-{HARNESS}.scope\n",
        f"0::/../../docker/{HARNESS}\n",
    ],
)
def test_the_container_is_read_from_the_cgroup(cgroup: str) -> None:
    """The shapes Docker makes. Docker Desktop's cgroupfs, as the filter sees
    it from its own cgroup namespace, is `/../<id>`; from the machine's, it is
    `/docker/<id>`.
    """
    assert container_of(cgroup) == HARNESS


OTHER = "b" * 64


@pytest.mark.parametrize(
    "cgroup",
    [
        f"0::/docker/{HARNESS}/init.scope\n",
        f"0::/docker/{HARNESS}/{OTHER}\n",
        f"0::/../{HARNESS}/{OTHER}\n",
        f"0::/../{HARNESS}/docker/{OTHER}\n",
        f"0::/../{HARNESS}/x/{OTHER}\n",
        f"0::/system.slice/docker-{HARNESS}.scope/docker-{OTHER}.scope\n",
        f"0::/system.slice/docker-{HARNESS}.scope/x.slice/docker-{OTHER}.scope\n",
        f"0::/docker/{HARNESS}/x.slice/docker-{OTHER}.scope\n",
        f"0::/../docker-{HARNESS}.scope/{OTHER}\n",
        f"0::/unicon/{OTHER}\n",
        f"0::/{OTHER}\n",
        f"0::/docker/x{OTHER}\n",
        f"12:memory:/docker/{HARNESS}\n11:cpu:/docker/{HARNESS}/{OTHER}\n",
    ],
)
def test_a_cgroup_below_a_container_names_none(cgroup: str) -> None:
    """A process in a cgroup of its own below its container's, named after
    another container, is taken for neither container.
    """
    assert container_of(cgroup) is None


@pytest.mark.parametrize(
    "cgroup",
    [
        "0::/\n",
        "0::/user.slice/session-3.scope\n",
        "0::/docker/abc\n",
        "",
        f"12:memory:/docker/{HARNESS}\n11:cpu:/docker/{'b' * 64}\n",
    ],
)
def test_a_process_outside_a_container_has_none(cgroup: str) -> None:
    assert container_of(cgroup) is None


def _details(**change: Any) -> dict[str, Any]:
    details: dict[str, Any] = {
        "State": {"Running": True},
        "Config": {"Env": ["PATH=/bin", f"UNICON_GRADING_ID={GRADING.upper()}"]},
        "Mounts": [
            {"Type": "volume", "Name": "unicon-filter", "Destination": "/run/unicon"},
            {"Type": "volume", "Name": WORKSPACE, "Destination": "/woodpecker"},
        ],
    }
    details.update(change)
    return details


def test_a_harness_is_its_grading_and_its_workspace_volume() -> None:
    caller = describe(HARNESS, _details(), "/woodpecker")
    assert caller.require() == (HARNESS, GRADING, WORKSPACE)


@pytest.mark.parametrize(
    ("details", "why"),
    [
        (None, "does not know"),
        (_details(State={"Running": False}), "not running"),
        (_details(Config={"Env": ["PATH=/bin"]}), "no UNICON_GRADING_ID"),
        (_details(Config={"Env": ["UNICON_GRADING_ID=seven"]}), "no UNICON_GRADING_ID"),
        (_details(Mounts=[]), "no volume mounted at /woodpecker"),
        (
            _details(
                Mounts=[
                    {"Type": "bind", "Source": "/srv", "Destination": "/woodpecker"}
                ]
            ),
            "no volume mounted",
        ),
    ],
)
def test_anything_else_is_not_a_grading_run(
    details: dict[str, Any] | None, why: str
) -> None:
    caller = describe(HARNESS, details, "/woodpecker")
    assert caller.unknown is not None and why in caller.unknown
