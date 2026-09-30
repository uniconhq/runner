"""HTTP/1.1 as the filter speaks it: strict on the harness's side, plain on the
daemon's.

A filter that parses a request one way while the daemon parses it another can
be walked past, so a request is read strictly and anything a lenient parser
might read differently is refused: a bare CR or LF, a folded or duplicated
header, whitespace before a colon, any Transfer-Encoding, Expect or Upgrade,
a percent-encoded path. What the daemon is sent is not the harness's bytes but
a request the filter writes afresh from what it parsed, with only the headers
it needs, so nothing it did not judge can ride along.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field

HEAD_LIMIT = 64 * 1024
BODY_LIMIT = 1024 * 1024
RESPONSE_LIMIT = 64 * 1024 * 1024
IDLE_SECONDS = 300.0

TOKEN = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
REQUEST_LINE = re.compile(rb"([A-Z]+) (/[!-~]*) HTTP/1\.([01])")
PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@/-]*")
QUERY = re.compile(r"[A-Za-z0-9._~=&-]*")
LENGTH = re.compile(r"[0-9]{1,9}")
REFUSED_HEADERS = ("transfer-encoding", "expect", "upgrade", "te", "trailer")
PASSED_BACK = ("content-type", "api-version", "docker-experimental", "ostype", "server")


class BadRequestError(Exception):
    """A request the filter will not read further. The connection closes after
    the refusal, because where the next request would begin is no longer known.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: tuple[tuple[str, str], ...]
    headers: dict[str, str]
    body: bytes
    keep_alive: bool

    def query_dict(self) -> dict[str, str]:
        return dict(self.query)


@dataclass
class Response:
    status: int
    reason: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


async def read_request(reader: asyncio.StreamReader) -> Request | None:
    """The next request on the connection, or None when the harness closed it
    between requests.
    """
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), IDLE_SECONDS)
    except asyncio.IncompleteReadError:
        return None
    except asyncio.LimitOverrunError:
        raise BadRequestError("the request head is too large") from None
    except TimeoutError:
        return None
    lines = head[:-4].split(b"\r\n")
    if any(b"\r" in line or b"\n" in line or b"\0" in line for line in lines):
        raise BadRequestError("a bare CR, LF or NUL in the request head")
    matched = REQUEST_LINE.fullmatch(lines[0])
    if matched is None:
        raise BadRequestError("the request line is not METHOD /path HTTP/1.x")
    method = matched.group(1).decode()
    target = matched.group(2).decode("ascii")
    minor = matched.group(3)
    headers = _headers(lines[1:])
    for name in REFUSED_HEADERS:
        if name in headers:
            raise BadRequestError(f"the {name} header is not accepted")
    connection = {t.strip().lower() for t in headers.get("connection", "").split(",")}
    if connection - {"", "keep-alive", "close"}:
        raise BadRequestError("Connection may only say keep-alive or close")
    body = await _body(reader, headers.get("content-length"))
    path, _, query = target.partition("?")
    if not PATH.fullmatch(path) or "//" in path:
        raise BadRequestError(
            "the path carries characters or encodings that are not accepted"
        )
    if not QUERY.fullmatch(query):
        raise BadRequestError(
            "the query carries characters or encodings that are not accepted"
        )
    pairs = tuple(
        (key, value)
        for key, _, value in (part.partition("=") for part in query.split("&") if part)
    )
    if len({key for key, _ in pairs}) != len(pairs):
        raise BadRequestError("a query parameter is repeated")
    keep_alive = minor == b"1" and "close" not in connection
    return Request(method, path, pairs, headers, body, keep_alive)


def _headers(lines: list[bytes]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in lines:
        if line[:1] in (b" ", b"\t"):
            raise BadRequestError("a folded header line")
        name, colon, value = line.partition(b":")
        if not colon or not TOKEN.fullmatch(name):
            raise BadRequestError("a header line that is not name: value")
        if any((byte < 0x20 and byte != 0x09) or byte == 0x7F for byte in value):
            raise BadRequestError("a control character in a header value")
        key = name.decode().lower()
        if key in headers:
            raise BadRequestError(f"the {key} header is repeated")
        headers[key] = value.strip(b" \t").decode("latin-1")
    return headers


async def _body(reader: asyncio.StreamReader, length: str | None) -> bytes:
    if length is None:
        return b""
    if not LENGTH.fullmatch(length):
        raise BadRequestError("Content-Length is not a plain number")
    size = int(length)
    if size > BODY_LIMIT:
        raise BadRequestError(f"the request body is over {BODY_LIMIT} bytes")
    try:
        return await asyncio.wait_for(reader.readexactly(size), IDLE_SECONDS)
    except asyncio.IncompleteReadError, TimeoutError:
        raise BadRequestError("the request body ended early") from None


def encode_request(request: Request, body: bytes | None = None) -> bytes:
    """The request the daemon is sent: rebuilt from what was judged, on a
    connection of its own that closes after the answer. `body` stands in for
    the harness's own bytes, which is how a create reaches the daemon as the
    document the filter judged.
    """
    content = request.body if body is None else body
    query = "&".join(f"{k}={v}" if v else k for k, v in request.query)
    target = f"{request.path}?{query}" if query else request.path
    lines = [f"{request.method} {target} HTTP/1.1", "Host: docker", "Connection: close"]
    if content or request.method in ("POST", "PUT"):
        lines.append(f"Content-Length: {len(content)}")
    if content:
        lines.append("Content-Type: application/json")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + content


async def read_response(reader: asyncio.StreamReader, method: str) -> Response:
    """One whole answer from the daemon, which the filter trusts to be HTTP."""
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head[:-4].decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    status = int(parts[1])
    reason = parts[2] if len(parts) > 2 else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    response = Response(status, reason, headers)
    if method == "HEAD" or status in (204, 304) or status < 200:
        return response
    if "chunked" in headers.get("transfer-encoding", "").lower():
        response.body = await _chunked(reader)
    elif "content-length" in headers:
        response.body = await reader.readexactly(int(headers["content-length"]))
    else:
        response.body = await _until_closed(reader)
    if len(response.body) > RESPONSE_LIMIT:
        raise ValueError(f"the daemon's answer is over {RESPONSE_LIMIT} bytes")
    return response


async def _until_closed(reader: asyncio.StreamReader) -> bytes:
    """An answer the daemon frames by closing the connection."""
    body = bytearray()
    while len(body) <= RESPONSE_LIMIT:
        chunk = await reader.read(64 * 1024)
        if not chunk:
            break
        body.extend(chunk)
    return bytes(body)


async def _chunked(reader: asyncio.StreamReader) -> bytes:
    body = bytearray()
    while True:
        line = await reader.readuntil(b"\r\n")
        size = int(line.split(b";")[0].strip() or b"0", 16)
        if size == 0:
            while await reader.readuntil(b"\r\n") != b"\r\n":
                pass
            return bytes(body)
        body.extend(await reader.readexactly(size + 2))
        del body[-2:]
        if len(body) > RESPONSE_LIMIT:
            raise ValueError(f"the daemon's answer is over {RESPONSE_LIMIT} bytes")


def encode_response(response: Response, method: str, keep_alive: bool) -> bytes:
    """The answer the harness is sent, framed by Content-Length whatever framing
    the daemon used.
    """
    lines = [f"HTTP/1.1 {response.status} {response.reason}".rstrip()]
    lines += [
        f"{name.title()}: {value}"
        for name, value in response.headers.items()
        if name in PASSED_BACK
    ]
    no_body = response.status in (204, 304) or response.status < 200
    if method == "HEAD" and "content-length" in response.headers:
        lines.append(f"Content-Length: {response.headers['content-length']}")
    elif not no_body:
        lines.append(f"Content-Length: {len(response.body)}")
    lines.append(f"Connection: {'keep-alive' if keep_alive else 'close'}")
    body = b"" if method == "HEAD" or no_body else response.body
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def refusal(reason: str) -> bytes:
    """A 403 with the reason in Docker's own error shape, so a Docker client shows
    it as the daemon's message.
    """
    body = json.dumps({"message": f"unicon socket filter: {reason}"}).encode()
    head = (
        "HTTP/1.1 403 Forbidden\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode("ascii") + body
