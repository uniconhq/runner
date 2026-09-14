"""Reading the envelope a grading job is handed, and refusing a bad one. The only
module that knows where the envelope schema lives.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from unicon_harness.contracts import SCHEMA_VERSION, schema_dir


@dataclass(frozen=True)
class EnvelopeError(Exception):
    """An envelope the harness will not act on. `code` is stable and safe to
    branch on; `message` is for whoever reads the job log.
    """

    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def load(raw: bytes) -> dict[str, Any]:
    """Parse one envelope and check it against the contract. Raises EnvelopeError
    with code `envelope_not_json`, `schema_version_mismatch` or
    `schema_violation`.
    """
    document = _parse(raw)
    _require_known_schema_version(document)
    _require_valid_shape(document)
    return document


def _parse(raw: bytes) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeError(
            "envelope_not_json", f"the envelope is not JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise EnvelopeError(
            "envelope_not_json",
            f"the envelope must be a JSON object, found {type(document).__name__}",
        )
    return document


def _require_known_schema_version(document: dict[str, Any]) -> None:
    """Check the version before the shape. Running the whole schema first would
    report a dozen field errors for what is really one problem: the backend and
    the image are different releases.
    """
    declared = document.get("schema_version")
    if declared != SCHEMA_VERSION:
        raise EnvelopeError(
            "schema_version_mismatch",
            f"the envelope declares schema_version {declared!r}, "
            f"and this harness only speaks {SCHEMA_VERSION}",
        )


def _require_valid_shape(document: dict[str, Any]) -> None:
    errors = sorted(_validator().iter_errors(document), key=lambda e: list(e.path))
    if not errors:
        return
    first = errors[0]
    location = "/".join(str(part) for part in first.absolute_path) or "the envelope"
    raise EnvelopeError(
        "schema_violation",
        f"the envelope does not match envelope.schema.json at {location}: "
        f"{first.message}",
    )


@cache
def _validator() -> Draft202012Validator:
    path = schema_dir() / "envelope.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())
