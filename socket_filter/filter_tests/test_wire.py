"""Reading requests strictly, and what the daemon is sent instead of the
harness's own bytes.
"""

from __future__ import annotations

import asyncio

import pytest

from unicon_filter.wire import (
    BadRequestError,
    Request,
    Response,
    encode_request,
    encode_response,
    read_request,
    read_response,
)


def _read(data: bytes) -> Request | None:
    async def go() -> Request | None:
        reader = asyncio.StreamReader(limit=64 * 1024)
        reader.feed_data(data)
        reader.feed_eof()
        return await read_request(reader)

    return asyncio.run(go())


def _refused(data: bytes) -> str:
    with pytest.raises(BadRequestError) as refusal:
        _read(data)
    return refusal.value.reason


def test_a_plain_request_is_read_with_its_body() -> None:
    request = _read(
        b"POST /v1.45/containers/create?name=a HTTP/1.1\r\nHost: docker\r\n"
        b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
    )
    assert request is not None
    assert (request.method, request.path) == ("POST", "/v1.45/containers/create")
    assert request.query == (("name", "a"),)
    assert request.body == b"{}"
    assert request.keep_alive


def test_a_closed_connection_between_requests_is_no_request() -> None:
    assert _read(b"") is None


def test_close_and_http_1_0_end_the_connection_after_the_answer() -> None:
    closing = _read(b"GET /_ping HTTP/1.1\r\nConnection: close\r\n\r\n")
    old = _read(b"GET /_ping HTTP/1.0\r\n\r\n")
    assert closing is not None and not closing.keep_alive
    assert old is not None and not old.keep_alive


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (
            b"POST /containers/create HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
            "transfer-encoding",
        ),
        (
            b"GET /_ping HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx",
            "repeated",
        ),
        (b"GET /_ping HTTP/1.1\r\nX: a\nContent-Length: 5\r\n\r\n", "bare CR"),
        (b"GET /_ping HTTP/1.1\r\nX: a\r\n folded\r\n\r\n", "folded"),
        (b"GET /_ping HTTP/1.1\r\nContent-Length : 0\r\n\r\n", "not name: value"),
        (b"GET /_ping HTTP/1.1\r\nExpect: 100-continue\r\n\r\n", "expect"),
        (b"GET /_ping HTTP/1.1\r\nUpgrade: tcp\r\n\r\n", "upgrade"),
        (b"GET /_ping HTTP/1.1\r\nConnection: Upgrade\r\n\r\n", "keep-alive or close"),
        (b"GET /containers/%61%62/json HTTP/1.1\r\n\r\n", "encodings"),
        (b"GET //containers/create HTTP/1.1\r\n\r\n", "encodings"),
        (b"GET /_ping?a=%2F HTTP/1.1\r\n\r\n", "encodings"),
        (b"GET /_ping?a=1&a=2 HTTP/1.1\r\n\r\n", "repeated"),
        (b"GET http://docker/_ping HTTP/1.1\r\n\r\n", "request line"),
        (b"GET /_ping HTTP/2.0\r\n\r\n", "request line"),
        (b"GET  /_ping HTTP/1.1\r\n\r\n", "request line"),
        (b"GET /_ping HTTP/1.1\r\nContent-Length: -1\r\n\r\n", "plain number"),
        (b"GET /_ping HTTP/1.1\r\nContent-Length: +1\r\n\r\nx", "plain number"),
        (b"GET /_ping HTTP/1.1\r\nContent-Length: \xb2\r\n\r\nx", "plain number"),
        (b"GET /_ping HTTP/1.1\r\nContent-Length: 99999999\r\n\r\n", "over"),
        (b"GET /_ping HTTP/1.1\r\nContent-Length: 10\r\n\r\nshort", "ended early"),
        (b"GET /_ping HTTP/1.1\r\nX: \x01\r\n\r\n", "control character"),
        (b"GET /\xff HTTP/1.1\r\n\r\n", "request line"),
    ],
)
def test_what_a_lenient_parser_would_read_differently_is_refused(
    data: bytes, reason: str
) -> None:
    assert reason in _refused(data)


def test_a_huge_head_is_refused() -> None:
    assert "too large" in _refused(
        b"GET /_ping HTTP/1.1\r\nX: " + b"a" * 70000 + b"\r\n\r\n"
    )


def test_the_daemon_is_sent_a_request_written_afresh() -> None:
    request = _read(
        b"POST /v1.45/containers/create HTTP/1.1\r\nHost: evil\r\n"
        b"X-Registry-Auth: e30=\r\nCookie: a\r\nContent-Type: text/plain\r\n"
        b"Content-Length: 2\r\n\r\n{}"
    )
    assert request is not None
    sent = encode_request(request)
    assert sent == (
        b"POST /v1.45/containers/create HTTP/1.1\r\nHost: docker\r\n"
        b"Connection: close\r\nContent-Length: 2\r\n"
        b"Content-Type: application/json\r\n\r\n{}"
    )


def test_a_create_is_sent_the_body_the_filter_judged() -> None:
    request = _read(
        b"POST /v1.45/containers/create HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}"
    )
    assert request is not None
    sent = encode_request(request, b'{"Image":"x"}')
    assert sent.endswith(
        b'Content-Length: 13\r\nContent-Type: application/json\r\n\r\n{"Image":"x"}'
    )


def test_an_answer_framed_by_closing_is_read_to_the_end() -> None:
    async def go() -> Response:
        reader = asyncio.StreamReader()
        reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n")
        for _ in range(3):
            reader.feed_data(b"x" * 70000)
        reader.feed_eof()
        return await read_response(reader, "GET")

    assert len(asyncio.run(go()).body) == 210000


def test_a_chunked_answer_is_sent_back_by_its_length() -> None:
    async def go() -> Response:
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\nX-Other: 1\r\n\r\n"
            b'3\r\n{"a\r\n4\r\n":1}\r\n0\r\n\r\n'
        )
        reader.feed_eof()
        return await read_response(reader, "GET")

    response = asyncio.run(go())
    assert response.body == b'{"a":1}'
    sent = encode_response(response, "GET", keep_alive=True)
    assert sent.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Length: 7\r\n" in sent
    assert b"Transfer-Encoding" not in sent and b"X-Other" not in sent
    assert sent.endswith(b'\r\n\r\n{"a":1}')
