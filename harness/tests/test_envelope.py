"""What the harness accepts as an envelope, and what it says when it will not."""

from __future__ import annotations

import json
from typing import Any

import pytest

from unicon_harness.envelope import EnvelopeError, load


def _serialise(document: dict[str, Any]) -> bytes:
    return json.dumps(document).encode("utf-8")


def test_the_published_example_is_accepted(example_envelope: dict[str, Any]) -> None:
    assert load(_serialise(example_envelope)) == example_envelope


def test_a_future_schema_version_is_refused(example_envelope: dict[str, Any]) -> None:
    example_envelope["schema_version"] = 2

    with pytest.raises(EnvelopeError) as refusal:
        load(_serialise(example_envelope))

    assert refusal.value.code == "schema_version_mismatch"
    assert "2" in refusal.value.message
    assert "1" in refusal.value.message


def test_a_missing_schema_version_is_refused_as_a_version_problem(
    example_envelope: dict[str, Any],
) -> None:
    del example_envelope["schema_version"]

    with pytest.raises(EnvelopeError) as refusal:
        load(_serialise(example_envelope))

    assert refusal.value.code == "schema_version_mismatch"


def test_a_missing_field_is_refused_and_named(
    example_envelope: dict[str, Any],
) -> None:
    del example_envelope["checkouts"]["task"]

    with pytest.raises(EnvelopeError) as refusal:
        load(_serialise(example_envelope))

    assert refusal.value.code == "schema_violation"
    assert "task" in refusal.value.message


def test_an_envelope_carrying_a_bundle_url_is_refused(
    example_envelope: dict[str, Any],
) -> None:
    """The harness downloads nothing but the envelope: a field naming a bundle
    to fetch is the dropped design, not a newer one.
    """
    example_envelope["urls"] = {"task_bundle": "http://garage.invalid/x"}

    with pytest.raises(EnvelopeError) as refusal:
        load(_serialise(example_envelope))

    assert refusal.value.code == "schema_violation"
    assert "urls" in refusal.value.message


def test_an_unknown_field_is_refused(example_envelope: dict[str, Any]) -> None:
    example_envelope["retries"] = 3

    with pytest.raises(EnvelopeError) as refusal:
        load(_serialise(example_envelope))

    assert refusal.value.code == "schema_violation"
    assert "retries" in refusal.value.message


def test_a_malformed_url_is_refused(example_envelope: dict[str, Any]) -> None:
    example_envelope["log_put"] = "not a url"

    with pytest.raises(EnvelopeError) as refusal:
        load(_serialise(example_envelope))

    assert refusal.value.code == "schema_violation"
    assert "log_put" in refusal.value.message


def test_a_relative_checkout_path_is_refused(example_envelope: dict[str, Any]) -> None:
    example_envelope["checkouts"]["submission"] = "submission"

    with pytest.raises(EnvelopeError) as refusal:
        load(_serialise(example_envelope))

    assert refusal.value.code == "schema_violation"
    assert "submission" in refusal.value.message


def test_bytes_that_are_not_json_are_refused() -> None:
    with pytest.raises(EnvelopeError) as refusal:
        load(b"<html>403 Forbidden</html>")

    assert refusal.value.code == "envelope_not_json"


def test_json_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(EnvelopeError) as refusal:
        load(b"[]")

    assert refusal.value.code == "envelope_not_json"
