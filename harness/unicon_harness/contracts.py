"""Where the contract files live, which version of them this harness speaks, and
the one way the harness checks a document against one of them. The plan,
envelope, result, primitive and submission schemas ship as release assets
beside the images, and the rest of the platform pins one runner release.
"""

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_VERSION = 5

MESSAGE_LIMIT = 300

SCHEMA_CANDIDATES = (
    Path(__file__).parent / "schemas",
    Path(__file__).parents[2] / "schemas",
)


class SchemasMissingError(Exception):
    """The image was built without the contract files. A packaging mistake rather
    than a job failure, so it names the directories it looked in.
    """


@cache
def schema_dir() -> Path:
    """The directory holding the contract schemas. Resolved once: it cannot
    change while the process runs.
    """
    for candidate in SCHEMA_CANDIDATES:
        if candidate.is_dir():
            return candidate
    looked_in = " or ".join(str(candidate) for candidate in SCHEMA_CANDIDATES)
    raise SchemasMissingError(
        f"no contract schemas in this image: looked in {looked_in}"
    )


@cache
def validator(contract: str, branch: str | None = None) -> Draft202012Validator:
    """The validator for one contract, `plan` for plan.schema.json and so on, or
    for one named branch under its `$defs`, such as the primitive contract's
    `outputs_file`.
    """
    path = schema_dir() / f"{contract}.schema.json"
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if branch is not None:
        schema = {"$defs": schema["$defs"], "$ref": f"#/$defs/{branch}"}
    return Draft202012Validator(schema, format_checker=FormatChecker())


def violation(document: Any, contract: str, branch: str | None = None) -> str | None:
    """The first way `document` breaks the contract, as `at <path>: <message>`,
    or None when it keeps it. Errors are sorted by where they are so the same
    document always reports the same one, and the message is cut short: a
    oneOf failure repeats the whole value it failed on.
    """
    errors = sorted(
        validator(contract, branch).iter_errors(document),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if not errors:
        return None
    first = errors[0]
    location = "/".join(str(part) for part in first.absolute_path) or "the top"
    message = first.message
    if len(message) > MESSAGE_LIMIT:
        message = message[: MESSAGE_LIMIT - 3] + "..."
    return f"at {location}: {message}"
