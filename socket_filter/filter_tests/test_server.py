"""The filter on a real unix socket in front of a stand-in daemon: every request
on a kept-alive connection judged, refusals as 403 with a reason, and only
containers it created for the caller's grading reachable afterwards.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from filter_tests.support import CALLER, CONFIG, GRADING, OTHER_GRADING, body
from unicon_filter.config import Config
from unicon_filter.policy import Caller
from unicon_filter.reaper import Record, Registry
from unicon_filter.server import EXIT_SOCKET_REPLACED, Filter, serve
from unicon_filter.upstream import Upstream

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix sockets")

CREATED = "d" * 64


class StandIn:
    """Answers like the daemon and remembers every request that reached it.
    `create_delay` holds each create's answer back that long, `create_status`
    is what a create answers, and each create after the first names a new
    container.
    """

    def __init__(
        self, create_delay: float = 0.0, create_status: str = "201 Created"
    ) -> None:
        self.seen: list[str] = []
        self.bodies: list[bytes] = []
        self.create_delay = create_delay
        self.create_status = create_status
        self.created = 0

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        length = next(
            (
                int(v.split(":")[1])
                for v in lines
                if v.lower().startswith("content-length")
            ),
            0,
        )
        self.bodies.append(await reader.readexactly(length))
        method, target, _ = lines[0].split(" ")
        self.seen.append(f"{method} {target}")
        if target.endswith("/containers/create"):
            await asyncio.sleep(self.create_delay)
            made = CREATED if not self.created else f"{self.created:064x}"
            self.created += 1
            status, answer = self.create_status, json.dumps({"Id": made}).encode()
        elif target.endswith("/wait"):
            status, answer = "200 OK", b'{"StatusCode":0}'
        else:
            status, answer = "200 OK", b"OK"
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
            f"Transfer-Encoding: chunked\r\nApi-Version: 1.47\r\n\r\n".encode()
            + f"{len(answer):x}\r\n".encode()
            + answer
            + b"\r\n0\r\n\r\n"
        )
        await writer.drain()
        writer.close()


Scenario = Callable[[str, StandIn, Registry], Awaitable[None]]


def run(
    tmp_path: Path,
    scenario: Scenario,
    caller: Caller = CALLER,
    config: Config = CONFIG,
    stand_in: StandIn | None = None,
) -> str:
    """Start the stand-in and the filter, run `scenario` against the filter's
    socket, and return the filter's decision log.
    """
    daemon_path = str(tmp_path / "daemon.sock")
    filter_path = str(tmp_path / "filter.sock")
    out = io.StringIO()

    async def identify(sock: Any) -> Caller:
        return caller

    daemon_side = stand_in or StandIn()

    async def go() -> None:
        daemon = await asyncio.start_unix_server(daemon_side.handle, path=daemon_path)
        registry = Registry()
        front = Filter(
            config, Upstream(daemon_path), registry, identifier=identify, out=out
        )
        server = await asyncio.start_unix_server(front.handle, path=filter_path)
        try:
            await scenario(filter_path, daemon_side, registry)
        finally:
            server.close()
            daemon.close()

    asyncio.run(go())
    return out.getvalue()


async def answers(
    path: str, data: bytes, count: int
) -> list[tuple[int, dict[str, str], bytes]]:
    """Send `data` on one connection and read up to `count` answers."""
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(data)
    await writer.drain()
    found = []
    for _ in range(count):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            break
        lines = head.decode().split("\r\n")
        headers = {
            k.lower(): v.strip()
            for k, _, v in (x.partition(":") for x in lines[1:] if x)
        }
        content = await reader.readexactly(int(headers.get("content-length", "0")))
        found.append((int(lines[0].split(" ")[1]), headers, content))
    writer.close()
    return found


def request(method: str, path: str, document: Any = None) -> bytes:
    data = b"" if document is None else json.dumps(document).encode()
    head = f"{method} {path} HTTP/1.1\r\nHost: docker\r\n"
    if data or method == "POST":
        head += f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
    return (head + "\r\n").encode() + data


def test_a_request_smuggled_after_an_allowed_one_is_judged_on_its_own(
    tmp_path: Path,
) -> None:
    """K1: ping, list, ping on one kept-alive connection, sent in one write."""
    seen: list[Any] = []

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        data = (
            request("GET", "/_ping")
            + request("GET", "/containers/json")
            + request("GET", "/_ping")
        )
        seen.extend(await answers(path, data, 3))
        seen.append(list(stand_in.seen))

    log = run(tmp_path, scenario)

    first, second = seen[0], seen[1]
    assert first[0] == 200 and first[2] == b"OK"
    assert first[1]["content-length"] == "2" and "transfer-encoding" not in first[1]
    assert second[0] == 403
    assert "is not something a grading run may do" in json.loads(second[2])["message"]
    assert len(seen) == 3
    assert seen[2] == ["GET /_ping"]
    assert '"decision": "deny"' in log


def test_a_chunked_body_is_refused_before_it_reaches_the_daemon(tmp_path: Path) -> None:
    seen: list[Any] = []

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        data = (
            b"POST /containers/create HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"2\r\n{}\r\n0\r\n\r\n"
        )
        seen.extend(await answers(path, data, 2))
        seen.append(list(stand_in.seen))

    run(tmp_path, scenario)

    assert seen[0][0] == 403
    assert "transfer-encoding" in json.loads(seen[0][2])["message"]
    assert seen[-1] == []


def test_a_run_creates_uses_and_removes_its_own_container(tmp_path: Path) -> None:
    statuses: list[int] = []
    left: list[int] = []

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        base = "/v1.45/containers"
        data = (
            request("POST", f"{base}/create", body())
            + request("POST", f"{base}/{CREATED}/start")
            + request("POST", f"{base}/{CREATED}/wait?condition=not-running")
            + request("GET", f"{base}/{CREATED}/json")
            + request("GET", f"{base}/{CREATED}/logs?stdout=1&stderr=1")
            + request("POST", f"{base}/{CREATED}/kill")
            + request("GET", f"{base}/{CALLER.container}/json")
        )
        statuses.extend(s for s, _, _ in await answers(path, data, 7))
        record = registry.get(CREATED)
        assert record is not None and record.started_at is not None
        assert record.time_ms == 60000 and record.caller == CALLER.container
        statuses.extend(
            s
            for s, _, _ in await answers(
                path, request("DELETE", f"{base}/{CREATED}?force=1"), 1
            )
        )
        left.append(len(registry))

    run(tmp_path, scenario)

    assert statuses == [201, 200, 200, 200, 200, 200, 200, 200]
    assert left == [0]


def test_a_create_short_of_the_sandbox_is_a_403_with_the_reason(tmp_path: Path) -> None:
    seen: list[Any] = []
    document = body()
    document["HostConfig"]["Privileged"] = True

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        seen.extend(
            await answers(path, request("POST", "/containers/create", document), 1)
        )
        seen.append(list(stand_in.seen))

    run(tmp_path, scenario)

    assert seen[0][0] == 403
    assert json.loads(seen[0][2]) == {
        "message": "unicon socket filter: HostConfig.Privileged must be empty"
    }
    assert seen[1] == []


def test_a_container_of_another_grading_is_out_of_reach(tmp_path: Path) -> None:
    seen: list[int] = []

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        registry.add(Record(CREATED, OTHER_GRADING, 1000, 0.0))
        found = await answers(path, request("POST", f"/containers/{CREATED}/kill"), 1)
        seen.extend(s for s, _, _ in found)
        registry.add(Record(CREATED, GRADING, 1000, 0.0))
        found = await answers(path, request("POST", f"/containers/{CREATED}/kill"), 1)
        seen.extend(s for s, _, _ in found)

    run(tmp_path, scenario)

    assert seen == [403, 200]


def test_a_body_on_a_verb_that_takes_none_is_refused(tmp_path: Path) -> None:
    seen: list[int] = []

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        registry.add(Record(CREATED, GRADING, 1000, 0.0))
        found = await answers(
            path, request("POST", f"/containers/{CREATED}/start", {"x": 1}), 1
        )
        seen.extend(s for s, _, _ in found)

    run(tmp_path, scenario)

    assert seen == [403]


def test_a_caller_that_is_not_a_grading_run_may_still_ping(tmp_path: Path) -> None:
    seen: list[int] = []
    stranger = Caller(unknown="the connecting process is not in a container")

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        data = request("GET", "/_ping") + request("POST", "/containers/create", body())
        seen.extend(s for s, _, _ in await answers(path, data, 2))

    run(tmp_path, scenario, caller=stranger)

    assert seen == [200, 403]


def test_a_forbidden_request_after_an_allowed_answer_is_refused(
    tmp_path: Path,
) -> None:
    """K2: the harness reads the answer to an allowed request, then sends a
    forbidden one on the same kept-alive connection.
    """
    seen: list[Any] = []

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        reader, writer = await asyncio.open_unix_connection(path)
        for data in (request("GET", "/_ping"), request("GET", "/containers/json")):
            writer.write(data)
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            length = next(
                int(line.split(":")[1])
                for line in lines
                if line.lower().startswith("content-length")
            )
            seen.append((int(lines[0].split(" ")[1]), await reader.readexactly(length)))
        seen.append(await reader.read())
        writer.close()
        seen.append(list(stand_in.seen))

    run(tmp_path, scenario)

    assert seen[0] == (200, b"OK")
    assert seen[1][0] == 403
    assert "is not something a grading run may do" in json.loads(seen[1][1])["message"]
    assert seen[2] == b""
    assert seen[3] == ["GET /_ping"]


def test_the_daemon_gets_the_judged_body_with_the_harness_label(
    tmp_path: Path,
) -> None:
    sent: list[bytes] = []

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        document = json.dumps(body()).encode()
        raw = (
            b"POST /containers/create HTTP/1.1\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(document) + 2}\r\n\r\n".encode()
            + document
            + b"\r\n"
        )
        await answers(path, raw, 1)
        sent.extend(stand_in.bodies)

    run(tmp_path, scenario)

    expected = body()
    expected["Labels"]["unicon.harness"] = CALLER.container
    assert [json.loads(b) for b in sent] == [expected]


def test_a_step_of_another_harness_of_the_same_grading_is_out_of_reach(
    tmp_path: Path,
) -> None:
    seen: list[int] = []

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        registry.add(Record(CREATED, GRADING, 1000, 0.0, "e" * 64))
        found = await answers(path, request("POST", f"/containers/{CREATED}/kill"), 1)
        seen.extend(s for s, _, _ in found)

    run(tmp_path, scenario)

    assert seen == [403]


def test_a_run_cannot_hold_more_step_containers_than_the_ceiling(
    tmp_path: Path,
) -> None:
    seen: list[Any] = []

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        for n in range(CONFIG.max_steps):
            registry.add(Record(f"{n}" * 64, GRADING, 1000, 0.0, CALLER.container))
        found = await answers(path, request("POST", "/containers/create", body()), 1)
        seen.extend(found)
        seen.append(list(stand_in.seen))

    run(tmp_path, scenario)

    assert seen[0][0] == 403
    assert "already has 4 step containers" in json.loads(seen[0][2])["message"]
    assert seen[1] == []


def test_a_caller_cannot_hold_more_connections_than_the_ceiling(
    tmp_path: Path,
) -> None:
    seen: list[Any] = []

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        held = [await asyncio.open_unix_connection(path) for _ in range(2)]
        await asyncio.sleep(0.2)
        found = await answers(path, request("GET", "/_ping"), 1)
        seen.extend(found)
        for _, writer in held:
            writer.close()
        await asyncio.sleep(0.2)
        seen.extend(await answers(path, request("GET", "/_ping"), 1))

    run(tmp_path, scenario, config=replace(CONFIG, max_connections=2))

    assert seen[0][0] == 403
    assert "already holds 2 connections" in json.loads(seen[0][2])["message"]
    assert seen[1][0] == 200


def test_creates_sent_at_once_on_many_connections_stop_at_the_ceiling(
    tmp_path: Path,
) -> None:
    """The ceiling is counted when a create is judged, not when the daemon
    answers it, so creates racing on many connections cannot all pass it.
    """
    statuses: list[int] = []
    counted: list[int] = []

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        found = await asyncio.gather(
            *(
                answers(path, request("POST", "/containers/create", body()), 1)
                for _ in range(12)
            )
        )
        statuses.extend(answer[0][0] for answer in found)
        counted.append(registry.count(GRADING))
        counted.append(stand_in.created)

    run(tmp_path, scenario, stand_in=StandIn(create_delay=0.3))

    assert sorted(statuses) == [201] * CONFIG.max_steps + [403] * (
        12 - CONFIG.max_steps
    )
    assert counted == [CONFIG.max_steps, CONFIG.max_steps]


def test_a_create_the_daemon_refuses_gives_its_place_back(tmp_path: Path) -> None:
    statuses: list[int] = []
    counted: list[int] = []

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        for _ in range(CONFIG.max_steps + 2):
            found = await answers(
                path, request("POST", "/containers/create", body()), 1
            )
            statuses.append(found[0][0])
        counted.append(registry.count(GRADING))

    run(
        tmp_path,
        scenario,
        stand_in=StandIn(create_status="500 Internal Server Error"),
    )

    assert statuses == [500] * (CONFIG.max_steps + 2)
    assert counted == [0]


def test_a_create_the_daemon_never_answers_gives_its_place_back(
    tmp_path: Path,
) -> None:
    statuses: list[int] = []
    counted: list[int] = []

    async def scenario(path: str, stand_in: StandIn, registry: Registry) -> None:
        (tmp_path / "daemon.sock").unlink()
        for _ in range(CONFIG.max_steps + 1):
            found = await answers(
                path, request("POST", "/containers/create", body()), 1
            )
            statuses.append(found[0][0])
        counted.append(registry.count(GRADING))

    run(tmp_path, scenario)

    assert statuses == [502] * (CONFIG.max_steps + 1)
    assert counted == [0]


def test_the_filter_exits_when_its_socket_is_replaced(tmp_path: Path) -> None:
    """Something that unlinks the filter's socket and binds its own at the same
    path is noticed: the filter says so and exits 3, and the machine restarts
    it, which takes the path back.
    """
    daemon_path = str(tmp_path / "daemon.sock")
    listen = tmp_path / "unicon" / "docker.sock"
    config = replace(
        CONFIG,
        upstream=daemon_path,
        listen=str(listen),
        socket_check_seconds=0.05,
        reap_every_seconds=60.0,
    )
    outcome: list[int] = []

    async def go() -> None:
        stand_in = StandIn()
        daemon = await asyncio.start_unix_server(stand_in.handle, path=daemon_path)
        ready = asyncio.Event()
        serving = asyncio.create_task(serve(config, ready=ready))
        await asyncio.wait_for(ready.wait(), 5)
        await asyncio.sleep(0.2)
        assert not serving.done()
        listen.unlink()

        async def impostor(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            writer.close()

        fake = await asyncio.start_unix_server(impostor, path=str(listen))
        try:
            outcome.append(await asyncio.wait_for(serving, 5))
        finally:
            fake.close()
            daemon.close()

    asyncio.run(go())

    assert outcome == [EXIT_SOCKET_REPLACED]


def test_the_filter_keeps_serving_while_its_socket_is_its_own(tmp_path: Path) -> None:
    daemon_path = str(tmp_path / "daemon.sock")
    listen = tmp_path / "unicon" / "docker.sock"
    config = replace(
        CONFIG, upstream=daemon_path, listen=str(listen), socket_check_seconds=0.05
    )
    outcome: list[Any] = []

    async def go() -> None:
        stand_in = StandIn()
        daemon = await asyncio.start_unix_server(stand_in.handle, path=daemon_path)
        ready = asyncio.Event()
        stop = asyncio.Event()
        serving = asyncio.create_task(serve(config, ready=ready, stop=stop))
        await asyncio.wait_for(ready.wait(), 5)
        await asyncio.sleep(0.3)
        outcome.append(serving.done())
        outcome.append(oct(listen.stat().st_mode & 0o777))
        stop.set()
        outcome.append(await asyncio.wait_for(serving, 5))
        daemon.close()

    asyncio.run(go())

    assert outcome == [False, "0o666", 0]
