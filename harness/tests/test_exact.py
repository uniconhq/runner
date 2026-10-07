"""Numbers read and written exactly as they are written, and a contestant's
number spelled the one way a template writes it.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from unicon_harness import exact
from unicon_harness.grading import spelled
from unicon_harness.submission import Scalar


def test_a_number_with_a_fraction_is_read_as_its_decimal() -> None:
    document = exact.loads(b'{"a": 0.1, "b": 2, "c": 1.0000000000000000001}')

    assert document == {
        "a": Decimal("0.1"),
        "b": 2,
        "c": Decimal("1.0000000000000000001"),
    }
    assert isinstance(document["b"], int)


def test_nan_and_infinity_are_read_as_decimals_to_be_refused_by_name() -> None:
    document = exact.loads(b"[NaN, Infinity, -Infinity]")

    assert [str(number) for number in document] == ["NaN", "Infinity", "-Infinity"]
    assert not exact.finite(document)
    assert exact.finite({"a": [Decimal("1E+400"), 3, "NaN"]})


def test_a_decimal_is_written_as_its_own_text() -> None:
    text = exact.dumps(
        {"a": Decimal("0.1"), "b": Decimal("1.50"), "c": Decimal("1E+3"), "d": 7}
    )

    assert text == '{"a": 0.1, "b": 1.50, "c": 1E+3, "d": 7}'
    assert json.loads(text)["c"] == 1000


def test_the_writing_matches_the_standard_writer_on_everything_else() -> None:
    document = {
        "schema_version": 5,
        "batch": [{"test": "main/1", "inputs": {"x": 'é\n"', "y": True, "z": None}}],
        "empty": {},
        "none": [],
    }

    assert exact.dumps(document) == json.dumps(document)
    assert exact.dumps(document, indent=2) == json.dumps(document, indent=2)


@pytest.mark.parametrize("number", [Decimal("NaN"), Decimal("Infinity")])
def test_a_number_that_is_not_finite_cannot_be_written(number: Decimal) -> None:
    with pytest.raises(ValueError, match="not a number JSON can carry"):
        exact.dumps({"a": number})


def test_a_float_has_no_place_in_what_the_harness_writes() -> None:
    with pytest.raises(TypeError):
        exact.dumps({"a": 0.1})


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (7, "7"),
        (-3, "-3"),
        (Decimal("2.50"), "2.5"),
        (Decimal("2.0"), "2"),
        (Decimal("0.5"), "0.5"),
        (Decimal("1E+3"), "1000"),
        (Decimal("1.5E-7"), "0.00000015"),
        (Decimal("-0.0"), "0"),
        (Decimal("100"), "100"),
        (
            Decimal("0.10000000000000000000000000000000001"),
            "0.10000000000000000000000000000000001",
        ),
        (True, "true"),
        (False, "false"),
        ("as it is {0}", "as it is {0}"),
    ],
)
def test_a_scalar_is_spelled_one_way(value: Scalar, text: str) -> None:
    assert spelled(value) == text
