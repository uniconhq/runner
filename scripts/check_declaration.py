"""Check a primitive repository's primitive.yaml against the primitive contract.

    python scripts/check_declaration.py PRIMITIVE_SCHEMA_JSON PRIMITIVE_REPO

Every primitive repository runs this, from the runner release it is built
against, in the shared workflows `primitive-ci.yaml` and
`primitive-release.yaml`. The repository's `primitive.yaml` has no image line:
bootstrap writes the image by digest from the release manifest into the
version it creates at the forge. So this check refuses an image line, fills in
a placeholder image and validates the result against the schema's
declaration. The declaration names neither the primitive nor its version: the
repository at the forge is the name and its tag the version. The file is read
as the forge reads it, YAML 1.2's core schema with exact numbers
(core_yaml.py), so an option `no` is the text `no` here as there. Exits 1 and
lists every problem when there is one.

A number output's bounds are compared here too, at_least no more than at_most,
since a JSON schema can't compare two values.
"""

import json
import math
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

import core_yaml

PLACEHOLDER_IMAGE = "ghcr.io/uniconhq/primitive@sha256:" + "0" * 64
DECLARATION = "primitive.yaml"


def load_declaration(root: Path) -> dict[str, Any]:
    """Read primitive.yaml as it is in the repository at `root`."""
    document = core_yaml.load((root / DECLARATION).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{DECLARATION} is not a mapping")
    return document


def with_placeholder_image(declaration: dict[str, Any]) -> dict[str, Any]:
    """The declaration with the image line bootstrap would write, by a fake digest."""
    return {**declaration, "image": PLACEHOLDER_IMAGE}


def problems(schema: dict[str, Any], root: Path) -> list[str]:
    """Everything wrong with the declaration of the repository at `root`, one
    line each.
    """
    try:
        declaration = load_declaration(root)
    except core_yaml.YAMLError as refused:
        problem = getattr(refused, "problem", None) or "it is not valid YAML"
        return [f"(top): does not parse as YAML: {problem}"]
    found = []
    if "image" in declaration:
        found.append(
            f"{DECLARATION} has an image line; the release manifest supplies it"
        )
    registry: Registry[Any] = Registry().with_resource(
        schema["$id"], Resource.from_contents(schema)
    )
    validator = Draft202012Validator(
        {"$ref": f"{schema['$id']}#/$defs/declaration"}, registry=registry
    )
    for error in validator.iter_errors(with_placeholder_image(declaration)):
        where = "/".join(str(part) for part in error.absolute_path) or "(top)"
        found.append(f"{where}: {error.message}")
    return found + crossed_bounds(declaration)


def crossed_bounds(declaration: dict[str, Any]) -> list[str]:
    """Each number output whose bound is not a finite number, or whose at_least
    is above its at_most, so no value fits.
    """
    outputs = declaration.get("outputs")
    if not isinstance(outputs, dict):
        return []
    found = []
    for name, port in outputs.items():
        if not isinstance(port, dict):
            continue
        bounds: dict[str, int | Decimal] = {}
        for key in ("at_least", "at_most"):
            value = port.get(key)
            if not isinstance(value, (int, Decimal)) or isinstance(value, bool):
                continue
            if math.isfinite(value):
                bounds[key] = value
            else:
                found.append(f"outputs/{name}/{key}: {value} is not a finite number")
        if len(bounds) == 2 and bounds["at_least"] > bounds["at_most"]:
            found.append(
                f"outputs/{name}: at_least {bounds['at_least']} "
                f"is above at_most {bounds['at_most']}"
            )
    return found


def main(argv: list[str]) -> int:
    """Check the repository named on the command line against the schema file."""
    if len(argv) != 3:
        print(
            "usage: check_declaration.py PRIMITIVE_SCHEMA_JSON PRIMITIVE_REPO",
            file=sys.stderr,
        )
        return 2
    schema = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    found = problems(schema, Path(argv[2]))
    for problem in found:
        print(f"{DECLARATION}: {problem}", file=sys.stderr)
    if found:
        return 1
    print(f"{DECLARATION} conforms to the declaration in primitive.schema.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
