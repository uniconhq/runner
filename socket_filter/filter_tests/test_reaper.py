"""The reaper against a daemon that only answers the three questions it asks."""

from __future__ import annotations

import asyncio
from typing import Any

from filter_tests.support import GRADING, HARNESS
from unicon_filter.policy import GRADING_LABEL, HARNESS_LABEL, TIME_LABEL
from unicon_filter.reaper import Reaper, Record, Registry, adopt


class Daemon:
    def __init__(self) -> None:
        self.listed: list[dict[str, Any]] = []
        self.running: set[str] = {HARNESS}
        self.removed: list[str] = []

    async def labelled(self, label: str) -> list[dict[str, Any]]:
        return [e for e in self.listed if e["Id"] not in self.removed]

    async def inspect(self, container: str) -> dict[str, Any] | None:
        return {"State": {"Running": container in self.running}}

    async def kill_and_remove(self, container: str) -> int:
        self.removed.append(container)
        return 204


def _entry(container: str, created: float, time_ms: str = "1000") -> dict[str, Any]:
    return {
        "Id": container,
        "Created": created,
        "State": "running",
        "Labels": {GRADING_LABEL: GRADING, TIME_LABEL: time_ms},
    }


def _sweep(registry: Registry, daemon: Daemon, now: float) -> list[str]:
    reaper = Reaper(registry, daemon, 30.0, lambda *a, **k: None, clock=lambda: now)
    return asyncio.run(reaper.sweep())


def test_a_container_within_its_clock_is_left_alone() -> None:
    registry = Registry()
    registry.add(Record("s1", GRADING, 1000, 100.0, HARNESS, 100.0))
    daemon = Daemon()
    daemon.listed = [_entry("s1", 100.0)]

    assert _sweep(registry, daemon, now=120.0) == []
    assert registry.get("s1") is not None


def test_a_container_past_its_clock_and_the_grace_is_removed() -> None:
    registry = Registry()
    registry.add(Record("s1", GRADING, 1000, 100.0, HARNESS, 100.0))
    daemon = Daemon()
    daemon.listed = [_entry("s1", 100.0)]

    assert _sweep(registry, daemon, now=131.5) == ["s1"]
    assert daemon.removed == ["s1"]
    assert registry.get("s1") is None


def test_the_clock_runs_from_the_start_not_the_create() -> None:
    registry = Registry()
    registry.add(Record("s1", GRADING, 1000, 100.0, HARNESS, 125.0))
    daemon = Daemon()
    daemon.listed = [_entry("s1", 100.0)]

    assert _sweep(registry, daemon, now=140.0) == []


def test_a_container_whose_harness_is_gone_is_removed_at_once() -> None:
    registry = Registry()
    registry.add(Record("s1", GRADING, 600_000, 100.0, HARNESS, 100.0))
    daemon = Daemon()
    daemon.listed = [_entry("s1", 100.0, "600000")]
    daemon.running = set()

    assert _sweep(registry, daemon, now=101.0) == ["s1"]


def test_a_filter_restarted_mid_run_takes_on_its_containers() -> None:
    registry = Registry()
    daemon = Daemon()
    daemon.listed = [_entry("s1", 100.0), _entry("s2", 100.0, "600000")]

    assert _sweep(registry, daemon, now=200.0) == ["s1"]
    record = registry.get("s2")
    assert record is not None and record.grading == GRADING and record.caller is None


def test_a_container_without_both_labels_is_not_taken_on() -> None:
    registry = Registry()
    listed = [{"Id": "x", "Labels": {GRADING_LABEL: GRADING}}]

    assert adopt(registry, listed, listed_at=100.0) == []
    assert len(registry) == 0


def test_a_record_made_while_the_list_was_asked_for_is_kept() -> None:
    registry = Registry()
    registry.add(Record("old", GRADING, 1000, 90.0))
    registry.add(Record("new", GRADING, 1000, 101.0))

    adopt(registry, [], listed_at=100.0)

    assert registry.get("old") is None
    assert registry.get("new") is not None


def test_a_restarted_filter_knows_each_step_by_its_harness_label() -> None:
    """The label the filter wrote at the create names the harness, so a step
    whose harness died while the filter was down is taken at once.
    """
    registry = Registry()
    daemon = Daemon()
    entry = _entry("s1", 100.0, "600000")
    entry["Labels"][HARNESS_LABEL] = HARNESS
    daemon.listed = [entry]
    daemon.running = set()

    assert _sweep(registry, daemon, now=101.0) == ["s1"]


def test_a_harness_label_that_is_not_a_container_id_is_ignored() -> None:
    registry = Registry()
    entry = _entry("s1", 100.0, "600000")
    entry["Labels"][HARNESS_LABEL] = "not-an-id"

    adopt(registry, [entry], listed_at=200.0)

    record = registry.get("s1")
    assert record is not None and record.caller is None


def test_a_clock_label_in_other_digits_is_not_taken_on() -> None:
    registry = Registry()

    assert adopt(registry, [_entry("s1", 100.0, "٣")], listed_at=200.0) == []


def test_the_registry_counts_a_gradings_steps() -> None:
    registry = Registry()
    registry.add(Record("s1", GRADING, 1000, 1.0))
    registry.add(Record("s2", GRADING, 1000, 1.0))
    registry.add(Record("s3", "other", 1000, 1.0))

    assert registry.count(GRADING) == 2
