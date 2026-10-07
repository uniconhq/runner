"""Reading and writing JSON with every number exactly as it is written.

A number a primitive writes is the decimal it is written as, and the platform
folds and compares it as that decimal, so it never passes through a binary
double on the way: `loads` reads every number with a fraction or an exponent
as a Decimal (whole numbers stay ints), and `dumps` writes a Decimal as its
own text. NaN and Infinity, which JSON does not have but Python's reader
takes, are read as the Decimal they name, so a check can refuse them by name
rather than the reader turning them into floats.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


def loads(raw: bytes | str) -> Any:
    """Raises ValueError, json.JSONDecodeError among them, for what is not JSON."""
    return json.loads(raw, parse_float=Decimal, parse_constant=Decimal)


def dumps(document: Any, indent: int | None = None) -> str:
    """`document` as JSON text, with each Decimal written as its own text.
    Raises ValueError for a number that is not finite, which JSON cannot carry.
    """
    return _encoded(document, indent, 0)


def finite(document: Any) -> bool:
    """Whether every number anywhere in `document` is finite."""
    if isinstance(document, Decimal):
        return document.is_finite()
    if isinstance(document, dict):
        return all(finite(value) for value in document.values())
    if isinstance(document, list):
        return all(finite(value) for value in document)
    return True


def _encoded(value: Any, indent: int | None, depth: int) -> str:
    if value is None or isinstance(value, bool | str):
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{value} is not a number JSON can carry")
        return str(value)
    if isinstance(value, dict):
        entries = [
            f"{json.dumps(str(key))}: {_encoded(item, indent, depth + 1)}"
            for key, item in value.items()
        ]
        return _joined(entries, "{", "}", indent, depth)
    if isinstance(value, list | tuple):
        entries = [_encoded(item, indent, depth + 1) for item in value]
        return _joined(entries, "[", "]", indent, depth)
    raise TypeError(f"a {type(value).__name__} has no JSON form")


def _joined(
    entries: list[str], opening: str, closing: str, indent: int | None, depth: int
) -> str:
    if not entries:
        return opening + closing
    if indent is None:
        return opening + ", ".join(entries) + closing
    inner = "\n" + " " * (indent * (depth + 1))
    outer = "\n" + " " * (indent * depth)
    return opening + inner + ("," + inner).join(entries) + outer + closing
