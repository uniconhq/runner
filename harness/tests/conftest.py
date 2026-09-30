"""Shared fixtures: the example envelope, the platform's HTTP routes, a fake
Docker over a temporary volume, and the two checkouts in it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.support import (
    Checkouts,
    FakeDocker,
    Platform,
    checkouts,
    example,
    platform_server,
)


@pytest.fixture
def example_envelope() -> dict[str, Any]:
    return example("envelope.json")


@pytest.fixture
def platform() -> Iterator[Platform]:
    with platform_server() as server:
        yield server


@pytest.fixture
def volume(tmp_path: Path) -> Path:
    root = tmp_path / "woodpecker"
    root.mkdir()
    return root


@pytest.fixture
def docker(volume: Path) -> FakeDocker:
    return FakeDocker(volume)


@pytest.fixture
def made(volume: Path) -> Checkouts:
    """Three tests of the sum task and a submission that solves them."""
    return checkouts(
        volume,
        {"1": ("1 2\n", "3\n"), "2": ("10 20 30\n", "60\n"), "10": ("5\n", "5\n")},
    )
