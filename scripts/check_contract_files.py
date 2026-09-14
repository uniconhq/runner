"""Check that the published examples still match the published schemas. An example
that drifts from its schema is the first warning that the backend and the
harness have parted company.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from unicon_harness.contracts import SCHEMA_VERSION

REPO_ROOT = Path(__file__).parents[1]
SCHEMAS = REPO_ROOT / "schemas"
EXAMPLES = REPO_ROOT / "examples"

CONTRACTS = ["plan", "envelope", "verdict"]


def main() -> int:
    problems = [problem for name in CONTRACTS for problem in _check(name)]
    problems += _check_registry()

    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1

    print(f"contract files ok: {', '.join(CONTRACTS)}, registry")
    return 0


def _check(name: str) -> list[str]:
    schema = _read(SCHEMAS / f"{name}.schema.json")
    example = _read(EXAMPLES / f"{name}.json")
    Draft202012Validator.check_schema(schema)
    declared = schema["properties"]["schema_version"].get("const")
    if declared != SCHEMA_VERSION:
        return [
            f"schemas/{name}.schema.json pins schema_version {declared}, "
            f"but the harness speaks {SCHEMA_VERSION}"
        ]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    problems = [
        f"examples/{name}.json does not match schemas/{name}.schema.json at "
        f"{'/'.join(str(part) for part in error.absolute_path) or 'the document'}: "
        f"{error.message}"
        for error in validator.iter_errors(example)
    ]
    return problems + _unexercised(name, schema, example)


def _unexercised(name: str, schema: Any, example: Any) -> list[str]:
    """Name every schema field the example never fills in. A field nobody writes
    is a field nobody reads, and it stays plausible for years because the
    schema still describes it.
    """
    present = set(_document_fields(example))
    return [
        f"schemas/{name}.schema.json declares {field}, "
        f"which examples/{name}.json never sets"
        for field in _schema_fields(schema, schema)
        if field not in present
    ]


def _schema_fields(node: Any, root: Any, prefix: str = "") -> Iterator[str]:
    """Every field path a document may carry, as dotted names. Array items extend
    the path of the array itself, so `summary.id` is one row's id.
    """
    if not isinstance(node, dict):
        return
    reference = node.get("$ref")
    if isinstance(reference, str):
        node = root["$defs"][reference.rsplit("/", 1)[-1]]
    for field, child in node.get("properties", {}).items():
        yield f"{prefix}{field}"
        yield from _schema_fields(child, root, f"{prefix}{field}.")
    yield from _schema_fields(node.get("items"), root, prefix)


def _document_fields(value: Any, prefix: str = "") -> Iterator[str]:
    if isinstance(value, dict):
        for field, child in value.items():
            yield f"{prefix}{field}"
            yield from _document_fields(child, f"{prefix}{field}.")
    elif isinstance(value, list):
        for item in value:
            yield from _document_fields(item, prefix)


def _check_registry() -> list[str]:
    """The registry has no schema of its own; Task 10 fills it and may add one."""
    registry = _read(REPO_ROOT / "registry.json")
    if registry.get("schema_version") != SCHEMA_VERSION:
        return [f"registry.json must declare schema_version {SCHEMA_VERSION}"]
    if not isinstance(registry.get("primitives"), list):
        return ["registry.json must have a primitives list"]
    return []


def _read(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


if __name__ == "__main__":
    sys.exit(main())
