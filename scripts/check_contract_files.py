"""Check that the published examples still match the published schemas. An example
that drifts from its schema is the first warning that the backend and the
harness have parted company.

`CONTRACTS` maps each contract to the examples that exercise it. The primitive
contract describes three documents, inputs.json, outputs.json and the
primitive.yaml declaration, so each of its examples is checked against the
branch it is an instance of, and a branch's fields are exercised by its
examples together. An example is JSON, or YAML when its name ends in .yaml,
read as the forge reads a definition file (core_yaml.py).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

import core_yaml
from unicon_harness.contracts import SCHEMA_VERSION

REPO_ROOT = Path(__file__).parents[1]
SCHEMAS = REPO_ROOT / "schemas"
EXAMPLES = REPO_ROOT / "examples"

CONTRACTS: dict[str, list[tuple[str, str | None]]] = {
    "plan": [("plan.json", None)],
    "envelope": [("envelope.json", None)],
    "result": [("result.json", None)],
    "submission": [("submission.json", None)],
    "primitive": [
        ("primitive-inputs.json", "inputs_file"),
        ("primitive-inputs-batch.json", "inputs_file"),
        ("primitive-outputs.json", "outputs_file"),
        ("primitive-outputs-batch.json", "outputs_file"),
        ("primitive-outputs-error.json", "outputs_file"),
        ("primitive-v6-inputs-batch.json", "inputs_file"),
        ("primitive-v6-outputs-batch.json", "outputs_file"),
        ("primitive.yaml", "declaration"),
        ("primitive-v6.yaml", "declaration"),
    ],
}


def main() -> int:
    faults = [
        fault
        for name, examples in CONTRACTS.items()
        for fault in _check(name, examples)
    ]

    for fault in faults:
        print(fault, file=sys.stderr)
    if faults:
        return 1

    print(f"contract files ok: {', '.join(CONTRACTS)}")
    return 0


def _check(name: str, examples: list[tuple[str, str | None]]) -> list[str]:
    schema = _read(SCHEMAS / f"{name}.schema.json")
    Draft202012Validator.check_schema(schema)
    faults = _version_pinned(name, schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    exercised: dict[str | None, set[str]] = {}
    for example_name, branch in examples:
        example = _read(EXAMPLES / example_name)
        faults += [
            f"examples/{example_name} does not match "
            f"schemas/{name}.schema.json at "
            f"{'/'.join(str(part) for part in error.absolute_path) or 'the document'}: "
            f"{error.message}"
            for error in validator.iter_errors(example)
        ]
        if branch is not None:
            faults += _branch_mismatch(name, example_name, branch, schema, example)
        exercised.setdefault(branch, set()).update(_document_fields(example))

    for branch, present in exercised.items():
        node = schema if branch is None else schema["$defs"][branch]
        faults += _unexercised(name, branch, node, schema, present)
    return faults


def _branch_mismatch(
    name: str, example_name: str, branch: str, schema: Any, example: Any
) -> list[str]:
    """An example listed for one branch must be an instance of that branch, not
    only of the root: a declaration that happened to validate as an outputs file
    would exercise the wrong fields.
    """
    node = {"$defs": schema["$defs"], "$ref": f"#/$defs/{branch}"}
    if Draft202012Validator(node).is_valid(example):
        return []
    return [
        f"examples/{example_name} is listed as {branch} of "
        f"schemas/{name}.schema.json but is not one"
    ]


def _version_pinned(name: str, schema: Any) -> list[str]:
    """Every contract file accepts the schema_version the harness speaks. The
    primitive contract accepts the versions primitives speak, the harness's
    among them, since a primitive keeps its version until it moves to a new one.
    """
    declared = _schema_versions(schema, schema)
    if declared is None or SCHEMA_VERSION not in declared:
        return [
            f"schemas/{name}.schema.json accepts schema_version "
            f"{sorted(declared) if declared else None}, "
            f"but the harness speaks {SCHEMA_VERSION}"
        ]
    return []


def _schema_versions(node: Any, root: Any) -> frozenset[int] | None:
    """The versions schema_version may be, by its `const` or `enum`, at the root
    or the same in every root branch that carries one. A primitive declaration
    without a version speaks version 5.
    """
    properties = node.get("properties", {})
    if "schema_version" in properties:
        version = properties["schema_version"]
        if "const" in version:
            return frozenset([version["const"]])
        return frozenset(version.get("enum", []))
    found = {
        _schema_versions(_resolved(branch, root), root)
        for branch in node.get("oneOf", [])
        if "schema_version" in _resolved(branch, root).get("properties", {})
    }
    return found.pop() if len(found) == 1 else None


def _unexercised(
    name: str, branch: str | None, node: Any, root: Any, present: set[str]
) -> list[str]:
    """Name every schema field the examples never fill in. A field nobody writes
    is a field nobody reads, and it stays plausible for years because the
    schema still describes it.
    """
    where = f"schemas/{name}.schema.json" + (f" ({branch})" if branch else "")
    return [
        f"{where} declares {field}, which no example under examples/ sets"
        for field in _schema_fields(node, root)
        if field not in present
    ]


def _schema_fields(node: Any, root: Any, prefix: str = "") -> Iterator[str]:
    """Every field path a document may carry, as dotted names. Array items extend
    the path of the array itself, so `tests.test` is one row's test.
    """
    if not isinstance(node, dict):
        return
    node = _resolved(node, root)
    for field, child in node.get("properties", {}).items():
        yield f"{prefix}{field}"
        yield from _schema_fields(child, root, f"{prefix}{field}.")
    yield from _schema_fields(node.get("items"), root, prefix)


def _resolved(node: Any, root: Any) -> Any:
    reference = node.get("$ref")
    if isinstance(reference, str):
        return root["$defs"][reference.rsplit("/", 1)[-1]]
    return node


def _document_fields(value: Any, prefix: str = "") -> Iterator[str]:
    if isinstance(value, dict):
        for field, child in value.items():
            yield f"{prefix}{field}"
            yield from _document_fields(child, f"{prefix}{field}.")
    elif isinstance(value, list):
        for item in value:
            yield from _document_fields(item, prefix)


def _read(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    document: dict[str, Any] = (
        core_yaml.load(text) if path.suffix == ".yaml" else json.loads(text)
    )
    return document


if __name__ == "__main__":
    sys.exit(main())
