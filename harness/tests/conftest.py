"""Shared fixtures: the example contract files, and a server that hands one out."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

EXAMPLES = Path(__file__).parents[2] / "examples"


@pytest.fixture
def example_envelope() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (EXAMPLES / "envelope.json").read_text(encoding="utf-8")
    )
    return document


class EnvelopeServer:
    """A real HTTP server handing out whatever bytes the test last set, so the
    timeout, status code and redirect behaviour under test are the ones the
    harness will meet.
    """

    def __init__(self, url: str, state: dict[str, Any]) -> None:
        self.url = url
        self._state = state

    def serve(self, body: bytes, status: int = 200, delay: float = 0.0) -> None:
        self._state["body"] = body
        self._state["status"] = status
        self._state["delay"] = delay


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client that gave up mid-response is what some tests are for. The
        default prints a traceback from a thread pytest is capturing, landing
        in the middle of an unrelated assertion.
        """


@pytest.fixture
def envelope_server() -> Iterator[EnvelopeServer]:
    state: dict[str, Any] = {"body": b"{}", "status": 200, "delay": 0.0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            time.sleep(state["delay"])
            body: bytes = state["body"]
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """Silence the default stderr access log; it only noises up pytest."""

    server = _QuietServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    port = int(server.server_address[1])
    try:
        yield EnvelopeServer(f"http://127.0.0.1:{port}/envelope.json", state)
    finally:
        server.shutdown()
        server.server_close()
