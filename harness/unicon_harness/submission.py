"""Reading `submission.json` from the submission checkout: what the contestant
gave for each input, and where its files are.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unicon_harness.contracts import SCHEMA_VERSION, violation
from unicon_harness.faults import GradingError
from unicon_harness.files import inside, read_bounded

SUBMISSION_FILE = "submission.json"
SUBMISSION_LIMIT_BYTES = 1024 * 1024

Scalar = str | int | float | bool


@dataclass(frozen=True)
class Given:
    """One contestant input: its files and chosen language, or its value."""

    files: tuple[Path, ...] = ()
    language: str | None = None
    value: Scalar | None = None


def load(checkout: Path) -> dict[str, Given]:
    path = inside(checkout, SUBMISSION_FILE, SUBMISSION_FILE)
    raw = read_bounded(path, SUBMISSION_LIMIT_BYTES, SUBMISSION_FILE)
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GradingError(f"{SUBMISSION_FILE} is not JSON: {exc}") from None
    if not isinstance(document, dict):
        raise GradingError(f"{SUBMISSION_FILE} is not a JSON object")
    declared = document.get("schema_version")
    if declared != SCHEMA_VERSION:
        raise GradingError(
            f"{SUBMISSION_FILE} declares schema_version {declared!r}, and this harness "
            f"only speaks {SCHEMA_VERSION}"
        )
    found = violation(document, "submission")
    if found is not None:
        raise GradingError(
            f"{SUBMISSION_FILE} does not match submission.schema.json {found}"
        )
    return {
        input_id: _given(checkout, input_id, entry)
        for input_id, entry in document["inputs"].items()
    }


def _given(checkout: Path, input_id: str, entry: dict[str, Any]) -> Given:
    """One entry, already checked against the schema."""
    if "value" in entry:
        return Given(value=entry["value"])
    paths = []
    for relative in entry["files"]:
        if not relative.startswith(f"files/{input_id}/"):
            raise GradingError(
                f"{SUBMISSION_FILE} lists {relative} for input {input_id}, outside "
                f"files/{input_id}/"
            )
        paths.append(inside(checkout, relative, f"submission file {relative}"))
    return Given(files=tuple(paths), language=entry.get("language"))
