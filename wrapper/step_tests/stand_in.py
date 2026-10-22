"""A stand-in for the forge's step door, as stage 2's prototype data server
was: it answers one job at `/door`, serves files by link, hands out upload
links, keeps what is put to them, and records every request, so a test can
see what the wrapper asked, with which headers, and that the token went
only where it should. Standard library only.

    /door                GET: the job, with the token
    /door/files/<name>   GET: a file the forge serves itself, with the token
    /door/uploads        POST: {"files": [...]} -> {"files": {path: link}}
    /door/done           POST: the step is done
    /door/ended          POST: the pipeline ended (report mode)
    /files/<name>        GET: a file by presigned link, no token
    /put/<path>          PUT: an upload by presigned link, no token
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = "tok-3f9a1c7e5b2d"


@dataclass
class Seen:
    method: str
    path: str
    authorization: str | None


@dataclass
class StandIn:
    base: str
    job: dict[str, Any] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    put: dict[str, bytes] = field(default_factory=dict)
    seen: list[Seen] = field(default_factory=list)
    asked: list[dict[str, Any]] = field(default_factory=list)
    failures: int = 0
    """How many requests to answer 503 first, to see the wrapper retry."""

    @property
    def door(self) -> str:
        return f"{self.base}/door"

    def link(self, name: str, by_door: bool = False) -> str:
        """A link to a file served here, a presigned one unless `by_door`."""
        return (
            f"{self.base}/door/files/{name}" if by_door else f"{self.base}/files/{name}"
        )

    def outputs(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.put["outputs.json"])
        return document

    def calls(self, method: str, path: str) -> int:
        return sum(1 for s in self.seen if s.method == method and s.path == path)


@contextmanager
def stand_in(host: str = "127.0.0.1", name: str | None = None) -> Iterator[StandIn]:
    """Serve on `host`, linked to as `name`, the host's name where the
    wrapper runs, such as host.docker.internal from a container."""
    server = ThreadingHTTPServer((host, 0), Handler)
    state = StandIn(base=f"http://{name or host}:{server.server_address[1]}")
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        pass

    @property
    def state(self) -> StandIn:
        state: StandIn = self.server.state  # type: ignore[attr-defined]
        return state

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _send(self, status: int, payload: Any = None, raw: bytes | None = None) -> None:
        data = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _start(self) -> bool:
        """Record the request; False when it was answered already."""
        authorization = self.headers.get("Authorization")
        self.state.seen.append(Seen(self.command, self.path, authorization))
        if self.state.failures:
            self.state.failures -= 1
            self._send(503, {})
            return False
        if self.path.startswith("/door") and authorization != f"Bearer {TOKEN}":
            self._send(401, {})
            return False
        return True

    def do_GET(self) -> None:
        if not self._start():
            return
        if self.path == "/door":
            self._send(200, self.state.job)
        elif self.path.startswith(("/files/", "/door/files/")):
            name = self.path.rsplit("/", 1)[1]
            if name in self.state.files:
                self._send(200, raw=self.state.files[name])
            else:
                self._send(404, {})
        else:
            self._send(404, {})

    def do_POST(self) -> None:
        body = self._body()
        if not self._start():
            return
        if self.path == "/door/uploads":
            asked = json.loads(body)
            self.state.asked.append(asked)
            links = {
                f["path"]: f"{self.state.base}/put/{f['path']}?sig=x"
                for f in asked["files"]
            }
            self._send(200, {"files": links})
        elif self.path in ("/door/done", "/door/ended"):
            self._send(204, raw=b"")
        else:
            self._send(404, {})

    def do_PUT(self) -> None:
        body = self._body()
        if not self._start():
            return
        if self.path.startswith("/put/"):
            self.state.put[self.path.removeprefix("/put/").split("?")[0]] = body
            self._send(200, raw=b"")
        else:
            self._send(404, {})
