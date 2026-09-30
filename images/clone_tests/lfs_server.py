"""A git-lfs server of the smallest kind, for the clone image's Docker test: the
batch API for downloads over the objects in one directory, and the objects
themselves. Every object request is printed as one line, so the test can
count what a checkout downloaded. Standard library only.

    python lfs_server.py <port> <objects dir> <own base url>
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OBJECTS = Path(sys.argv[2])
BASE = sys.argv[3].rstrip("/")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        answered = []
        for wanted in request.get("objects", []):
            oid = str(wanted["oid"])
            found = OBJECTS / oid
            entry: dict[str, object] = {"oid": oid, "size": wanted["size"]}
            if request.get("operation") == "download" and found.is_file():
                entry["actions"] = {"download": {"href": f"{BASE}/objects/{oid}"}}
            else:
                entry["error"] = {"code": 404, "message": "no such object"}
            answered.append(entry)
        self._send(
            200,
            json.dumps({"transfer": "basic", "objects": answered}).encode(),
            "application/vnd.git-lfs+json",
        )

    def do_GET(self) -> None:
        oid = self.path.rsplit("/", 1)[-1]
        found = OBJECTS / oid
        print(json.dumps({"download": oid}), flush=True)
        if not found.is_file():
            self._send(404, b"", "text/plain")
            return
        self._send(200, found.read_bytes(), "application/octet-stream")

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        """Only the download lines reach the log."""


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), Handler).serve_forever()
