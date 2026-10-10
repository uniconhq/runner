"""core_yaml.py reads as the forge's reader does: YAML 1.2's core schema,
whatever `%YAML` line a file carries, every number that is not whole an
exact Decimal, a key given twice refused, and the tags only YAML 1.1 has
refused. The cases are the forge's own (`tests/test_yaml_models.py`), so
the two copies cannot drift apart unnoticed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import core_yaml

THIRTY_DIGITS = "123456789012345.678901234567891"


@pytest.mark.parametrize(
    ("written", "read"),
    [
        ("no", "no"),
        ("NO", "NO"),
        ("on", "on"),
        ("off", "off"),
        ("yes", "yes"),
        ("y", "y"),
        ("1_000", "1_000"),
        ("1:30", "1:30"),
        ("2026-06-01", "2026-06-01"),
        ("2026-06-01T09:00:00Z", "2026-06-01T09:00:00Z"),
        ("<<", "<<"),
        ("017", 17),
        ("0o17", 15),
        ("0x1F", 31),
        ("-3", -3),
        ("true", True),
        ("False", False),
        ("~", None),
        ("", None),
        ("2.50", Decimal("2.50")),
        ("1e3", Decimal("1e3")),
        ("-.5", Decimal("-0.5")),
        (THIRTY_DIGITS, Decimal(THIRTY_DIGITS)),
    ],
)
def test_a_scalar_reads_as_yaml_1_2_says(written: str, read: object) -> None:
    found = core_yaml.load(f"value: {written}\n")

    assert found == {"value": read}
    assert type(found["value"]) is type(read)


def test_a_file_naming_yaml_1_1_is_still_read_as_1_2() -> None:
    assert core_yaml.load("%YAML 1.1\n---\ncountry: NO\nlate: no\n") == {
        "country": "NO",
        "late": "no",
    }


@pytest.mark.parametrize(
    "written",
    [
        "!!timestamp 2026-01-01",
        "!!binary aGk=",
        "!!set {a: null}",
        "!!omap [{a: 1}]",
        "!!pairs [{a: 1}]",
    ],
)
def test_a_tag_of_yaml_1_1_alone_is_refused(written: str) -> None:
    with pytest.raises(core_yaml.YAMLError, match=r"not one of YAML 1.2.s core schema"):
        core_yaml.load(f"value: {written}\n")


def test_a_key_given_twice_is_refused() -> None:
    with pytest.raises(core_yaml.YAMLError, match="the key 'a' is given twice"):
        core_yaml.load("a: 1\na: 2\n")
