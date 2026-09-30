"""The containers this filter created, and the reaper that holds them to their
clocks.

A step container outlives its harness when the harness dies mid-run, and
nothing else on a platform machine would clean it up until the worker exists.
So the filter records every container it lets a harness create, with the
grading it belongs to, the harness that asked for it and its wall clock, and
every few seconds kills and removes one that has outlived its time limit plus
a grace period, or whose harness is gone. At start, and on every pass, it also
takes on any container carrying both of its labels that it has no record of,
which is what a filter restarted mid-run finds; the harness label the filter
wrote on each of them says whose it is.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from unicon_filter.policy import (
    CLOCK,
    CONTAINER,
    GRADING_LABEL,
    HARNESS_LABEL,
    TIME_LABEL,
    normal_grading,
)


class Daemon(Protocol):
    """The three questions the reaper asks the daemon."""

    async def labelled(self, label: str) -> list[dict[str, Any]]: ...

    async def inspect(self, container: str) -> dict[str, Any] | None: ...

    async def kill_and_remove(self, container: str) -> int: ...


@dataclass
class Record:
    container: str
    grading: str
    time_ms: int
    created_at: float
    caller: str | None = None
    started_at: float | None = None

    def due(self, grace_seconds: float) -> float:
        """When the reaper may take it: its clock from the start, or from its
        creation when it never started, plus the grace.
        """
        began = self.started_at if self.started_at is not None else self.created_at
        return began + self.time_ms / 1000 + grace_seconds


class Registry:
    """The step containers the filter knows of, and the creates it has passed
    to the daemon and not yet heard back on. A grading's count holds both, so
    a ceiling checked against it cannot be walked past by many creates sent
    at once on many connections.
    """

    def __init__(self) -> None:
        self._records: dict[str, Record] = {}
        self._pending: dict[str, int] = {}

    def add(self, record: Record) -> None:
        self._records[record.container] = record

    def get(self, container: str) -> Record | None:
        return self._records.get(container)

    def forget(self, container: str) -> None:
        self._records.pop(container, None)

    def count(self, grading: str) -> int:
        """How many step containers a grading has on the machine, or may have
        once the daemon answers the creates still in flight.
        """
        held = sum(1 for record in self._records.values() if record.grading == grading)
        return held + self._pending.get(grading, 0)

    def reserve(self, grading: str) -> None:
        """Count a create about to be sent to the daemon, before anything is
        awaited, so the next create is judged with it counted.
        """
        self._pending[grading] = self._pending.get(grading, 0) + 1

    def release(self, grading: str) -> None:
        """Stop counting a create the daemon has answered, whatever it said:
        a container it made is counted by its record from then on.
        """
        left = self._pending.get(grading, 0) - 1
        if left > 0:
            self._pending[grading] = left
        else:
            self._pending.pop(grading, None)

    def __iter__(self) -> Iterator[Record]:
        return iter(list(self._records.values()))

    def __len__(self) -> int:
        return len(self._records)


def adopt(
    registry: Registry, listed: list[dict[str, Any]], listed_at: float
) -> list[str]:
    """Take on labelled containers with no record; forget records of containers
    the daemon no longer has, if they were made before the listing was asked
    for. Returns the ids taken on.
    """
    present: set[str] = set()
    taken: list[str] = []
    for entry in listed:
        container = str(entry.get("Id", ""))
        labels = entry.get("Labels") or {}
        grading = normal_grading(labels.get(GRADING_LABEL))
        raw_time = str(labels.get(TIME_LABEL, ""))
        if not container or grading is None or not CLOCK.fullmatch(raw_time):
            continue
        present.add(container)
        if registry.get(container) is None:
            created = float(entry.get("Created") or time.time())
            harness = str(labels.get(HARNESS_LABEL, ""))
            caller = harness if CONTAINER.fullmatch(harness) else None
            registry.add(
                Record(container, grading, int(raw_time), created, caller, created)
            )
            taken.append(container)
    for record in registry:
        if record.container not in present and record.created_at < listed_at:
            registry.forget(record.container)
    return taken


class Reaper:
    def __init__(
        self,
        registry: Registry,
        upstream: Daemon,
        grace_seconds: float,
        log: Callable[..., None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._registry = registry
        self._upstream = upstream
        self._grace = grace_seconds
        self._log = log
        self._clock = clock

    async def sweep(self) -> list[str]:
        """One pass. Returns the containers it removed."""
        listed_at = self._clock()
        listed = await self._upstream.labelled(GRADING_LABEL)
        for container in adopt(self._registry, listed, listed_at):
            self._log("adopt", container=container)
        running = {
            str(entry.get("Id")) for entry in listed if entry.get("State") == "running"
        }
        callers: dict[str, bool] = {}
        removed: list[str] = []
        now = self._clock()
        for record in self._registry:
            why = None
            if now > record.due(self._grace):
                why = f"outlived its {record.time_ms} ms and the grace period"
            elif record.caller is not None:
                if record.caller not in callers:
                    callers[record.caller] = await self._caller_running(record.caller)
                if not callers[record.caller]:
                    why = "its harness is gone"
            if why is None:
                continue
            status = await self._upstream.kill_and_remove(record.container)
            self._log(
                "reap",
                container=record.container,
                reason=why,
                running=record.container in running,
                status=status,
            )
            if status < 300 or status == 404:
                self._registry.forget(record.container)
                removed.append(record.container)
        return removed

    async def _caller_running(self, container: str) -> bool:
        details = await self._upstream.inspect(container)
        return bool(details and (details.get("State") or {}).get("Running"))
