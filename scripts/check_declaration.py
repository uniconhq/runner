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
since a JSON schema can't compare two values, and so are a programs port's
outputs. So is what one port says of another: at most one programs port, a
links port only beside one, and a limit from sum or max naming a programs
port and one of its number inputs. A declaration with forms is checked
per form, as the shared keys plus that form's keys, and a form with no
required input of its own is refused.
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
    return found + _form_problems(declaration)


_SHARED = (
    "title",
    "description",
    "schema_version",
    "batch",
    "network",
    "cache",
    "image",
)


def _form_problems(declaration: dict[str, Any]) -> list[str]:
    """`crossed_bounds` and `programs_problems` for a formless declaration,
    or for each form as the shared keys plus that form's keys. A form must
    have a required input no other form declares.
    """
    forms = declaration.get("forms")
    if not isinstance(forms, dict) or not forms:
        return crossed_bounds(declaration) + programs_problems(declaration)
    found = []
    views: list[tuple[str, dict[str, Any]]] = []
    shared = {key: declaration[key] for key in _SHARED if key in declaration}
    for name, form in forms.items():
        if not isinstance(form, dict):
            continue
        view = {**shared, **form}
        views.append((str(name), form))
        for problem in crossed_bounds(view) + programs_problems(view):
            found.append(f"forms/{name}/{problem}")
    found += _own_required(views)
    return found


def _own_required(forms: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Each form that has no required input no other form declares."""
    declared: dict[str, set[str]] = {}
    required: dict[str, set[str]] = {}
    for name, form in forms:
        inputs = form.get("inputs")
        if not isinstance(inputs, dict):
            declared[name] = set()
            required[name] = set()
            continue
        declared[name] = set(inputs)
        required[name] = {
            key
            for key, port in inputs.items()
            if not (isinstance(port, dict) and port.get("optional") is True)
        }
    found = []
    for name, needed in required.items():
        others: set[str] = set()
        for other, keys in declared.items():
            if other != name:
                others |= keys
        if not needed - others:
            found.append(
                f"forms/{name}: a form needs a required input no other form declares"
            )
    return found


def crossed_bounds(declaration: dict[str, Any]) -> list[str]:
    """Each number output, the declaration's own and each programs port's,
    whose bound is not a finite number, or whose at_least is above its
    at_most, so no value fits.
    """
    found = crossed_output_bounds(declaration.get("outputs"), "outputs")
    for name, port in programs_ports(declaration).items():
        found += crossed_output_bounds(port.get("outputs"), f"inputs/{name}/outputs")
    return found


def crossed_output_bounds(outputs: object, where: str) -> list[str]:
    """`crossed_bounds` for one map of outputs, at `where`."""
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
                found.append(f"{where}/{name}/{key}: {value} is not a finite number")
        if len(bounds) == 2 and bounds["at_least"] > bounds["at_most"]:
            found.append(
                f"{where}/{name}: at_least {bounds['at_least']} "
                f"is above at_most {bounds['at_most']}"
            )
    return found


def programs_ports(declaration: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The declaration's inputs of type programs, by name."""
    inputs = declaration.get("inputs")
    if not isinstance(inputs, dict):
        return {}
    return {
        name: port
        for name, port in inputs.items()
        if isinstance(port, dict) and port.get("type") == "programs"
    }


def programs_problems(declaration: dict[str, Any]) -> list[str]:
    """What a schema can't say of programs ports: there is at most one, named
    `programs`, since a step's programs share its outputs' names and a step
    takes its outputs from that one port; such a port needs `outputs_from:
    programs`; a links port links its programs, so needs one; and a limit
    from `sum` or `max` names a programs port and a number input it declares.
    """
    programs = programs_ports(declaration)
    found = []
    if len(programs) > 1:
        found.append(f"inputs: {', '.join(programs)} are each a programs port")
    for name in programs:
        if name != "programs":
            found.append(f"inputs/{name}: a programs port must be named programs")
    if programs and declaration.get("outputs_from") != "programs":
        found.append(
            "outputs_from: a declaration with a programs port needs "
            "outputs_from: programs"
        )
    inputs = declaration.get("inputs")
    links = [
        name
        for name, port in (inputs if isinstance(inputs, dict) else {}).items()
        if isinstance(port, dict) and port.get("type") == "links"
    ]
    if links and not programs:
        found.append(f"inputs/{links[0]}: links with no programs port to link")
    limits_from = declaration.get("limits_from")
    for limit, given in (limits_from if isinstance(limits_from, dict) else {}).items():
        for over in ("sum", "max"):
            named = given.get(over) if isinstance(given, dict) else None
            if not isinstance(named, str):
                continue
            port, _, number = named.partition(".")
            entry = programs.get(port, {}).get("inputs")
            taken = entry.get(number) if isinstance(entry, dict) else None
            if port not in programs:
                found.append(
                    f"limits_from/{limit}/{over}: {port} is not a programs port"
                )
            elif not isinstance(taken, dict) or taken.get("type") != "number":
                found.append(
                    f"limits_from/{limit}/{over}: {port} has no number input {number}"
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
