"""The filter's own calls to the daemon's socket: forwarding a judged request,
and the few questions the filter asks for itself.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

from unicon_filter.wire import Response, read_response

TIMEOUT_SECONDS = 30.0


class Upstream:
    def __init__(self, path: str) -> None:
        self.path = path

    async def exchange(
        self, request: bytes, method: str, timeout: float | None
    ) -> Response:
        """Send one request on a fresh connection and read the whole answer."""
        reader, writer = await asyncio.open_unix_connection(self.path)
        try:
            writer.write(request)
            await writer.drain()
            return await asyncio.wait_for(read_response(reader, method), timeout)
        finally:
            writer.close()

    async def call(self, method: str, path: str) -> Response:
        request = f"{method} {path} HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n"
        if method in ("POST", "PUT"):
            request += "Content-Length: 0\r\n"
        return await self.exchange((request + "\r\n").encode(), method, TIMEOUT_SECONDS)

    async def inspect(self, container: str) -> dict[str, Any] | None:
        """The daemon's description of a container, or None when it has none."""
        response = await self.call("GET", f"/containers/{container}/json")
        if response.status != 200:
            return None
        document: dict[str, Any] = json.loads(response.body)
        return document

    async def labelled(self, label: str) -> list[dict[str, Any]]:
        """Every container carrying `label`, running or not."""
        filters = json.dumps({"label": [label]}, separators=(",", ":"))
        response = await self.call(
            "GET", f"/containers/json?all=1&filters={quote(filters)}"
        )
        if response.status != 200:
            raise ConnectionError(f"listing containers answered {response.status}")
        found: list[dict[str, Any]] = json.loads(response.body)
        return found

    async def kill_and_remove(self, container: str) -> int:
        await self.call("POST", f"/containers/{container}/kill")
        return (
            await self.call("DELETE", f"/containers/{container}?force=1&v=1")
        ).status

    async def ping(self) -> bool:
        try:
            return (await self.call("GET", "/_ping")).status == 200
        except OSError:
            return False
