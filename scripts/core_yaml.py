"""YAML as the forge reads a definition file: YAML 1.2's core schema and
nothing else, whatever `%YAML` line a file carries. Only `true` and `false`
are booleans, so `no`, `NO` and `on` are text, and `1_000`, `1:30` and a date
are text too. Every number that is not whole is the exact `Decimal` it is
written as, never a float, and a mapping that gives one key twice is
refused. The declaration check and the contract examples read with it, so
a primitive's declaration means here what it means at the forge.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.constructor import ConstructorError, SafeConstructor
from ruamel.yaml.error import YAMLError as YAMLError
from ruamel.yaml.nodes import ScalarNode
from ruamel.yaml.resolver import BaseResolver

_TAG = "tag:yaml.org,2002:"
_DIGITS = list("0123456789")
_CORE: tuple[tuple[str, str, list[str]], ...] = (
    ("null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    ("int", r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$", ["-", "+", *_DIGITS]),
    (
        "float",
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
        ["-", "+", ".", *_DIGITS],
    ),
)


class _CoreResolver(BaseResolver):
    """YAML 1.2's core schema and nothing else. Every resolver is filed
    under the characters it can start with, since the library adds the ones
    filed under none to a list it keeps.
    """

    yaml_implicit_resolvers: dict[Any, list[tuple[str, re.Pattern[str]]]] = {}  # noqa: RUF012

    def __init__(self, version: Any = None, loader: Any = None) -> None:
        super().__init__(loader)

    @property
    def processing_version(self) -> tuple[int, int]:
        return (1, 2)


for _name, _pattern, _first in _CORE:
    for _char in _first:
        _CoreResolver.yaml_implicit_resolvers.setdefault(_char, []).append(
            (_TAG + _name, re.compile(_pattern))
        )


class _ExactConstructor(SafeConstructor):
    """The safe constructor with every number that is not whole an exact
    `Decimal`, every whole one read as 1.2 spells it, and a mapping that
    gives one key twice refused.
    """

    def check_mapping_key(
        self, node: Any, key_node: Any, mapping: Any, key: Any, value: Any
    ) -> bool:
        if key in mapping:
            raise ConstructorError(
                None, None, f"the key {key!r} is given twice", key_node.start_mark
            )
        return True

    def construct_exact_float(self, node: ScalarNode) -> Decimal:
        text = str(self.construct_scalar(node))
        bare = text.lstrip("+-").lower()
        if bare == ".inf":
            return Decimal("-Infinity" if text.startswith("-") else "Infinity")
        if bare == ".nan":
            return Decimal("NaN")
        return Decimal(text)

    def construct_core_int(self, node: ScalarNode) -> int:
        text = str(self.construct_scalar(node))
        if text.startswith(("0o", "0x")):
            return int(text[2:], 8 if text[1] == "o" else 16)
        return int(text, 10)


_ExactConstructor.add_constructor(
    _TAG + "float", _ExactConstructor.construct_exact_float
)
_ExactConstructor.add_constructor(_TAG + "int", _ExactConstructor.construct_core_int)


def load(text: str | bytes) -> Any:
    """The document in `text`. Raises `YAMLError` when it does not parse."""
    reader = YAML(typ="safe", pure=True)
    reader.Resolver = _CoreResolver
    reader.Constructor = _ExactConstructor
    return reader.load(text)
