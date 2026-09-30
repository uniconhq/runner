"""The harness's Docker client: where it connects, and how it reads the Engine
API's answers, against a small server on a real unix socket.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import pytest

from unicon_harness.docker import DockerClient, DockerError, _demultiplexed, socket_path


def test_docker_host_must_name_a_unix_socket() -> None:
    assert socket_path({"DOCKER_HOST": "unix:///run/unicon/docker.sock"}) == (
        "/run/unicon/docker.sock"
    )
    for value in ("", "tcp://127.0.0.1:2375", "unix://", "/var/run/docker.sock"):
        with pytest.raises(DockerError, match="socket filter"):
            socket_path({"DOCKER_HOST": value})
    with pytest.raises(DockerError):
        socket_path({})


def test_the_multiplexed_log_stream_is_read_frame_by_frame() -> None:
    frames = (
        b"\x01\x00\x00\x00\x00\x00\x00\x03out"
        + b"\x02\x00\x00\x00\x00\x00\x00\x04err\n"
    )
    assert _demultiplexed(frames) == b"outerr\n"


needs_unix = pytest.mark.skipif(
    sys.platform == "win32", reason="unix sockets for httpx exist on Linux only"
)


if sys.platform != "win32":
    from socketserver import ThreadingUnixStreamServer

    class _Daemon(ThreadingUnixStreamServer):
        daemon_threads = True
        answers: dict[tuple[str, str], tuple[int, Any, str]]
        seen: list[tuple[str, str, bytes]]


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[tuple[str, _Daemon]]:
    path = str(tmp_path / "docker.sock")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            route = self.path.split("?")[0]
            server = self.server
            assert isinstance(server, _Daemon)
            server.seen.append((self.command, self.path, body))
            status, document, kind = server.answers.get(
                (self.command, route),
                (404, {"message": "no such route"}, "application/json"),
            )
            data = (
                document
                if isinstance(document, bytes)
                else json.dumps(document).encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._answer()

        def do_POST(self) -> None:
            self._answer()

        def do_DELETE(self) -> None:
            self._answer()

        def address_string(self) -> str:
            return "unix"

        def log_message(self, format: str, *args: Any) -> None:
            """Quiet."""

    server = _Daemon(path, Handler)
    server.answers = {}
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield path, server
    finally:
        server.shutdown()
        server.server_close()


@needs_unix
def test_create_start_wait_logs_and_remove(daemon: tuple[str, _Daemon]) -> None:
    path, server = daemon
    base = "/v1.45/containers"
    cid = "a" * 64
    server.answers = {
        ("POST", f"{base}/create"): (
            201,
            {"Id": cid, "Warnings": []},
            "application/json",
        ),
        ("POST", f"{base}/{cid}/start"): (204, b"", "text/plain"),
        ("POST", f"{base}/{cid}/wait"): (200, {"StatusCode": 3}, "application/json"),
        ("GET", f"{base}/{cid}/logs"): (
            200,
            b"\x01\x00\x00\x00\x00\x00\x00\x02hi",
            "application/vnd.docker.multiplexed-stream",
        ),
        ("DELETE", f"{base}/{cid}"): (204, b"", "text/plain"),
    }
    client = DockerClient(path)
    try:
        assert client.create({"Image": "x"}) == cid
        client.start(cid)
        assert client.wait(cid, 5) == 3
        assert client.logs(cid, 100) == "hi"
        client.remove(cid)
    finally:
        client.close()
    methods = [(m, p.split("?")[0]) for m, p, _ in server.seen]
    assert methods[0] == ("POST", f"{base}/create")
    assert json.loads(server.seen[0][2]) == {"Image": "x"}
    assert ("DELETE", f"{base}/{cid}") in methods
    assert "force=1" in server.seen[-1][1]


@needs_unix
def test_a_refusal_carries_the_filter_reason(daemon: tuple[str, _Daemon]) -> None:
    path, server = daemon
    server.answers = {
        ("POST", "/v1.45/containers/create"): (
            403,
            {"message": "unicon socket filter: privileged"},
            "application/json",
        )
    }
    client = DockerClient(path)
    try:
        with pytest.raises(DockerError) as refused:
            client.create({"Image": "x"})
    finally:
        client.close()
    assert refused.value.status == 403
    assert refused.value.reason == "unicon socket filter: privileged"


@needs_unix
def test_a_wait_past_its_time_is_none(tmp_path: Path) -> None:
    """A socket that accepts and never answers stands in for a container that is
    still running.
    """
    path = str(tmp_path / "silent.sock")
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(path)
    listener.listen()
    client = DockerClient(path)
    try:
        assert client.wait("b" * 64, 0.2) is None
    finally:
        client.close()
        listener.close()


@needs_unix
def test_a_socket_that_is_not_there_is_an_error(tmp_path: Path) -> None:
    client = DockerClient(str(tmp_path / "missing.sock"))
    try:
        with pytest.raises(DockerError) as failed:
            client.start("c" * 64)
    finally:
        client.close()
    assert failed.value.status == 0
