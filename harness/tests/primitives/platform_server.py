"""The forge's machine routes, for the Docker integration test: GET /envelope
serves /data/envelope.json, POST /callback and PUT /log answer 200. Every
request is printed as one JSON line, which the test reads from the log.
Standard library only; run as a container on the harness's network.
"""

from __future__ import annotations

import base64
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = Path("/data/envelope.json").read_bytes()
        self._record(b"")
        self._answer(body)

    def do_POST(self) -> None:
        self._record(self._body())
        self._answer(b"{}")

    def do_PUT(self) -> None:
        self._record(self._body())
        self._answer(b"")

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def _record(self, body: bytes) -> None:
        entry = {
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "body": base64.b64encode(body).decode(),
        }
        print(json.dumps(entry), flush=True)

    def _answer(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Only the JSON lines go to stdout."""


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), Handler).serve_forever()
