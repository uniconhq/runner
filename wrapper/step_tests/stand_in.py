"""A stand-in for the forge's step door, as stage 2's prototype data server
was: it answers one job at `/door`, serves files by link, hands out upload
links, keeps what is put to them, and records every request, so a test can
see what the wrapper asked, with which headers, and that the token went
only where it should. Standard library only.

    /door                GET: the job, with the token; 204 for nothing to do
    /door/files/<name>   GET: a file the forge serves itself, with the token
    /door/uploads        POST: {"files": [...]} -> {"files": {path: link}}
    /door/done           POST: the step is done
    /door/ended          POST: the pipeline ended (report mode)
    /files/<name>        GET: a file by presigned link, no token
    /put/<path>          PUT: an upload by presigned link, no token

A POST to the door must be sent as application/json and a PUT as
application/octet-stream, or it is answered 415; a PUT without the
x-amz-checksum-sha256 its link is signed for, the body's SHA-256 in
base64, is answered 400, as the store answers it. `cuts` cuts a file's
answers off midway, `moved` answers a path with a redirect, and
`done_after` holds the answer to /door/done back. As the door does, done
and ended end the token, once taken, though their answer is cut off.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
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
    kind: str | None = None
    checksum: str | None = None


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
    cuts: dict[str, list[str]] = field(default_factory=dict)
    """For a path, how to cut off each next answer to it: "chunked" ends a
    chunked body midway, "reset" resets the connection midway, "short"
    closes it midway after naming the whole length."""
    moved: dict[str, str] = field(default_factory=dict)
    """A path answered 302 to a link."""
    done_after: float = 0.0
    ended: bool = False
    """Whether done or ended was taken, which ends the token."""
    done_code: int = 204
    job_code: int = 200
    """What the door answers the job's GET: 204 for nothing to do, or a
    refusal."""
    raw: dict[str, str] = field(default_factory=dict)
    """Texts of the job written into its JSON as they are, unquoted, for
    numbers Python writes only one way."""

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
        kind = self.headers.get("Content-Type")
        checksum = self.headers.get("x-amz-checksum-sha256")
        self.state.seen.append(
            Seen(self.command, self.path, authorization, kind, checksum)
        )
        if self.path in self.state.moved:
            self.send_response(302)
            self.send_header("Location", self.state.moved[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        if self.state.failures:
            self.state.failures -= 1
            self._send(503, {})
            return False
        if self.path.startswith("/door") and (
            authorization != f"Bearer {TOKEN}" or self.state.ended
        ):
            self._send(401, {})
            return False
        wanted = {"POST": "application/json", "PUT": "application/octet-stream"}
        if self.command in wanted and kind != wanted[self.command]:
            self._send(415, {})
            return False
        return True

    def _cut(self, data: bytes) -> bool:
        """Cut off this answer as `cuts` says; False when it has no cut."""
        ways = self.state.cuts.get(self.path)
        if not ways:
            return False
        way, half = ways.pop(0), data[: len(data) // 2]
        self.send_response(200)
        if way == "chunked":
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(f"{len(half):x}\r\n".encode() + half + b"\r\n")
        else:
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(half)
        self.wfile.flush()
        if way == "reset":
            linger = struct.pack("ii", 1, 0)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        self.connection.close()
        self.close_connection = True
        return True

    def do_GET(self) -> None:
        if not self._start():
            return
        if self.path == "/door" and self.state.job_code == 204:
            self.send_response(204)
            self.end_headers()
        elif self.path == "/door" and self.state.job_code != 200:
            self._send(self.state.job_code, {})
        elif self.path == "/door":
            text = json.dumps(self.state.job)
            for name, written in self.state.raw.items():
                text = text.replace(json.dumps(name), written)
            job = text.encode()
            if not self._cut(job):
                self._send(200, raw=job)
        elif self.path.startswith(("/files/", "/door/files/")):
            name = self.path.rsplit("/", 1)[1]
            if name in self.state.files:
                if not self._cut(self.state.files[name]):
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
            if self.path == "/door/done":
                time.sleep(self.state.done_after)
            if self.state.done_code >= 300:
                self._send(self.state.done_code, {})
                return
            self.state.ended = True
            if not self._cut(b"{}"):
                self._send(204, raw=b"")
        else:
            self._send(404, {})

    def do_PUT(self) -> None:
        body = self._body()
        if not self._start():
            return
        digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
        if self.headers.get("x-amz-checksum-sha256") != digest:
            self._send(400, {})
        elif self.path.startswith("/put/"):
            self.state.put[self.path.removeprefix("/put/").split("?")[0]] = body
            self._send(200, raw=b"")
        else:
            self._send(404, {})
