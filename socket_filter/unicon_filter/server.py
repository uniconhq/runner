"""The filter's socket: every request on every connection read, judged and
either passed to the daemon or refused with a 403 that names the reason.

A connection is identified once, when it opens, from the process that opened
it, and one caller may hold only so many connections at a time. After a
refusal the connection closes. Every decision is printed as one JSON line.

The socket is the filter's own file, in a directory its own uid owns, and the
harness has that directory read-only. Should the file at the socket's path
ever be another than the one the filter bound, something has put a socket of
its own in the filter's place: the filter says so and exits 3, so the machine
restarts it and notices.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TextIO

from unicon_filter.config import Config, ConfigError
from unicon_filter.peer import identify
from unicon_filter.policy import (
    Caller,
    RefusedError,
    Route,
    Ticket,
    check_create,
    parse_body,
    route,
)
from unicon_filter.reaper import Reaper, Record, Registry
from unicon_filter.upstream import Upstream
from unicon_filter.wire import (
    HEAD_LIMIT,
    BadRequestError,
    Request,
    Response,
    encode_request,
    encode_response,
    read_request,
    refusal,
)

EXIT_OK = 0
EXIT_CANNOT_START = 2
EXIT_SOCKET_REPLACED = 3
FORWARD_TIMEOUT_SECONDS = 120.0
PROC = Path("/proc")
UNANSWERED = b'{"message": "unicon socket filter: the daemon did not answer"}'


Identifier = Callable[[Any], Awaitable[Caller]]


class Filter:
    def __init__(
        self,
        config: Config,
        upstream: Upstream,
        registry: Registry,
        identifier: Identifier | None = None,
        out: TextIO = sys.stdout,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.upstream = upstream
        self.registry = registry
        self._identify = identifier or self._peer
        self._out = out
        self._clock = clock
        self._open: dict[str | None, int] = {}

    async def _peer(self, sock: Any) -> Caller:
        try:
            return await identify(
                sock, PROC, self.upstream.inspect, self.config.workspace
            )
        except (OSError, TimeoutError, ValueError) as exc:
            return Caller(unknown=f"the daemon could not describe it: {exc}")

    def log(self, decision: str, **fields: Any) -> None:
        entry = {"ts": round(self._clock(), 3), "decision": decision, **fields}
        print(json.dumps(entry), file=self._out, flush=True)

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        caller: Caller | None = None
        counted = False
        try:
            caller = await self._identify(writer.get_extra_info("socket"))
            held = self._open.get(caller.container, 0)
            if held >= self.config.max_connections:
                await self._refuse(
                    writer,
                    caller,
                    "-",
                    "-",
                    f"the caller already holds {held} connections to the filter",
                )
                return
            self._open[caller.container] = held + 1
            counted = True
            await self._serve(reader, writer, caller)
        except ConnectionError, asyncio.IncompleteReadError:
            return
        except Exception as exc:
            self.log(
                "fail",
                caller=_short(caller),
                reason=f"the connection failed: {type(exc).__name__}: {exc}",
            )
        finally:
            if counted and caller is not None:
                left = self._open.get(caller.container, 1) - 1
                if left > 0:
                    self._open[caller.container] = left
                else:
                    self._open.pop(caller.container, None)
            writer.close()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, caller: Caller
    ) -> None:
        while True:
            try:
                request = await read_request(reader)
            except BadRequestError as bad:
                await self._refuse(writer, caller, "-", "-", bad.reason)
                return
            if request is None:
                return
            try:
                found = route(request.method, request.path, request.query)
                ticket = self._judge(found, request, caller)
            except RefusedError as refused:
                await self._refuse(
                    writer, caller, request.method, request.path, refused.reason
                )
                return
            try:
                forwarded = await self._forward(found, request, writer, caller, ticket)
            finally:
                if found.verb == "create" and ticket is not None:
                    self.registry.release(ticket.grading)
            if not forwarded:
                return

    def _judge(self, found: Route, request: Request, caller: Caller) -> Ticket | None:
        """Whether a request may pass, with what an allowed create commits the
        filter to. Nothing here awaits: an allowed create is counted against
        its grading's ceiling before the next request anywhere is judged, and
        `_serve` stops counting it once the daemon has answered.
        """
        if found.verb == "create":
            ticket = check_create(parse_body(request.body), caller, self.config)
            held = self.registry.count(ticket.grading)
            if held >= self.config.max_steps:
                raise RefusedError(
                    f"the run already has {held} step containers on the machine; "
                    "remove one first"
                )
            self.registry.reserve(ticket.grading)
            return ticket
        if request.body:
            raise RefusedError(f"{found.verb} takes no body")
        if found.verb in ("ping", "version"):
            return None
        own, grading, _ = caller.require()
        if found.verb == "inspect" and found.container == own:
            return None
        record = self.registry.get(found.container or "")
        if (
            record is None
            or record.grading != grading
            or (record.caller is not None and record.caller != own)
        ):
            raise RefusedError(
                f"container {(found.container or '')[:12]} was not created by this "
                "filter for this harness"
            )
        return None

    async def _forward(
        self,
        found: Route,
        request: Request,
        writer: asyncio.StreamWriter,
        caller: Caller | None,
        ticket: Ticket | None,
    ) -> bool:
        timeout = None if found.verb == "wait" else FORWARD_TIMEOUT_SECONDS
        body = ticket.body if ticket is not None else None
        try:
            response = await self.upstream.exchange(
                encode_request(request, body), request.method, timeout
            )
        except (OSError, TimeoutError, ValueError) as exc:
            self.log(
                "fail",
                caller=_short(caller),
                method=request.method,
                path=request.path,
                reason=f"the daemon did not answer: {exc}",
            )
            writer.write(
                encode_response(
                    Response(
                        502,
                        "Bad Gateway",
                        {"content-type": "application/json"},
                        UNANSWERED,
                    ),
                    request.method,
                    keep_alive=False,
                )
            )
            await writer.drain()
            return False
        self._remember(found, response, caller, ticket)
        self.log(
            "allow",
            caller=_short(caller),
            method=request.method,
            path=request.path,
            status=response.status,
        )
        writer.write(encode_response(response, request.method, request.keep_alive))
        await writer.drain()
        return request.keep_alive

    def _remember(
        self,
        found: Route,
        response: Response,
        caller: Caller | None,
        ticket: Ticket | None,
    ) -> None:
        now = self._clock()
        if found.verb == "create" and ticket is not None and response.status == 201:
            try:
                container = str(json.loads(response.body)["Id"])
            except ValueError, KeyError, TypeError:
                return
            owner = caller.container if caller is not None else None
            self.registry.add(
                Record(container, ticket.grading, ticket.time_ms, now, owner)
            )
        elif found.verb == "start" and response.status < 300:
            record = self.registry.get(found.container or "")
            if record is not None and record.started_at is None:
                record.started_at = now
        elif found.verb == "delete" and (
            response.status < 300 or response.status == 404
        ):
            self.registry.forget(found.container or "")

    async def _refuse(
        self,
        writer: asyncio.StreamWriter,
        caller: Caller | None,
        method: str,
        path: str,
        reason: str,
    ) -> None:
        self.log("deny", caller=_short(caller), method=method, path=path, reason=reason)
        writer.write(refusal(reason))
        with contextlib.suppress(ConnectionError):
            await writer.drain()


def _short(caller: Caller | None) -> str | None:
    return caller.container[:12] if caller is not None and caller.container else None


async def serve(
    config: Config,
    ready: asyncio.Event | None = None,
    stop: asyncio.Event | None = None,
) -> int:
    upstream = Upstream(config.upstream)
    if not await upstream.ping():
        return _cannot_start(
            "upstream_unreachable",
            f"the daemon's socket {config.upstream} does not answer",
        )
    registry = Registry()
    front = Filter(config, upstream, registry)
    reaper = Reaper(registry, upstream, config.grace_seconds, front.log)
    listen = Path(config.listen)
    listen.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        listen.unlink()
    server = await asyncio.start_unix_server(
        front.handle, path=str(listen), limit=HEAD_LIMIT
    )
    listen.chmod(0o666)
    bound = _identity(listen)
    front.log(
        "start",
        listen=str(listen),
        images=sorted(config.images),
        workspace=config.workspace,
    )
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, stop.set)
    reaping = asyncio.create_task(
        _reap_forever(reaper, config.reap_every_seconds, front)
    )
    replaced = asyncio.Event()
    watching = asyncio.create_task(
        _watch_socket(listen, bound, config.socket_check_seconds, front, replaced)
    )
    if ready is not None:
        ready.set()
    stopping = asyncio.create_task(stop.wait())
    noticing = asyncio.create_task(replaced.wait())
    try:
        await asyncio.wait({stopping, noticing}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (reaping, watching, stopping, noticing):
            task.cancel()
        server.close()
        await server.wait_closed()
    return EXIT_SOCKET_REPLACED if replaced.is_set() else EXIT_OK


def _identity(path: Path) -> tuple[int, int] | None:
    """The device and inode of the file at `path`, not following a link, or
    None when there is none.
    """
    try:
        found = path.lstat()
    except OSError:
        return None
    return found.st_dev, found.st_ino


async def _watch_socket(
    listen: Path,
    bound: tuple[int, int] | None,
    every: float,
    front: Filter,
    replaced: asyncio.Event,
) -> None:
    """Look at the socket's path every `every` seconds and set `replaced` once
    it is not the socket the filter bound.
    """
    while True:
        await asyncio.sleep(every)
        found = _identity(listen)
        if found != bound:
            front.log(
                "fail",
                listen=str(listen),
                reason="the socket is not the one the filter bound; exiting",
            )
            print(
                f"unicon-socket-filter: socket_replaced: {listen} is not the "
                "socket the filter bound",
                file=sys.stderr,
                flush=True,
            )
            replaced.set()
            return


async def _reap_forever(reaper: Reaper, every: float, front: Filter) -> None:
    while True:
        try:
            await reaper.sweep()
        except (OSError, ValueError) as exc:
            front.log("fail", reason=f"the reaper could not sweep: {exc}")
        await asyncio.sleep(every)


def _cannot_start(code: str, message: str) -> int:
    print(f"unicon-socket-filter: {code}: {message}", file=sys.stderr)
    return EXIT_CANNOT_START


def main() -> int:
    try:
        config = Config.from_environment()
    except ConfigError as exc:
        return _cannot_start(exc.code, str(exc))
    return asyncio.run(serve(config))
