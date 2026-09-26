"""The harness image entrypoint. Woodpecker starts the image once per submission
with a URL to the envelope and the judging id. Today it fetches the envelope,
checks it against the contract and stops; running the plan is Task 6.
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Any

import httpx

from unicon_harness.contracts import SCHEMA_VERSION, SchemasMissingError
from unicon_harness.envelope import EnvelopeError, load

ENVELOPE_URL_VARIABLE = "UNICON_ENVELOPE_URL"
JUDGING_ID_VARIABLE = "UNICON_JUDGING_ID"

FETCH_IO_TIMEOUT_SECONDS = 30.0

EXIT_OK = 0
EXIT_CANNOT_START = 2


def main() -> int:
    envelope_url = os.environ.get(ENVELOPE_URL_VARIABLE, "")
    if not envelope_url:
        return _refuse("missing_environment", f"{ENVELOPE_URL_VARIABLE} is not set")

    judging_id = os.environ.get(JUDGING_ID_VARIABLE, "")
    if not judging_id:
        return _refuse("missing_environment", f"{JUDGING_ID_VARIABLE} is not set")

    try:
        raw = _fetch(envelope_url)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        return _refuse(
            "envelope_unreachable",
            f"the envelope at {_redacted(envelope_url)} answered {status}",
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return _refuse(
            "envelope_unreachable",
            f"could not fetch the envelope from {_redacted(envelope_url)}: "
            f"{type(exc).__name__}",
        )

    try:
        envelope = load(raw)
    except EnvelopeError as exc:
        return _refuse(exc.code, exc.message)
    except SchemasMissingError as exc:
        return _refuse("schemas_missing", str(exc))

    if not _same_judging(judging_id, envelope["judging_id"]):
        return _refuse(
            "judging_id_mismatch",
            f"{JUDGING_ID_VARIABLE} is {judging_id} but the envelope is for "
            f"{envelope['judging_id']}",
        )

    print(_summary(envelope))
    return EXIT_OK


def _fetch(url: str) -> bytes:
    timeout = httpx.Timeout(FETCH_IO_TIMEOUT_SECONDS)
    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _same_judging(from_environment: str, from_envelope: str) -> bool:
    """Compare the two ids as uuids, so case and dash style agree. An unparseable
    value is a mismatch rather than a traceback: whoever started the image got
    the id wrong either way.
    """
    try:
        return uuid.UUID(from_environment) == uuid.UUID(from_envelope)
    except ValueError:
        return False


def _redacted(url: str) -> str:
    """The envelope URL is presigned: its query is a credential and must not land
    in the job log.
    """
    try:
        return str(httpx.URL(url).copy_with(query=None))
    except httpx.InvalidURL:
        return "<invalid url>"


def _summary(envelope: dict[str, Any]) -> str:
    submission = envelope["submission"]
    return (
        f"envelope accepted: judging={envelope['judging_id']} "
        f"submission={submission['org']}/{submission['repo']}@{submission['tag']} "
        f"stage={envelope['stage']} attempt={envelope['attempt']} "
        f"schema_version={SCHEMA_VERSION}"
    )


def _refuse(code: str, message: str) -> int:
    print(f"unicon-harness: {code}: {message}", file=sys.stderr)
    return EXIT_CANNOT_START


def run() -> None:
    """Console script entrypoint."""
    sys.exit(main())
