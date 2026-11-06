"""plan.schema.json on a run's environment value, which no example plan has."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).parents[2]


def run_value() -> Draft202012Validator:
    """A validator of one run value, as the plan's `run_value` defines it."""
    schema = json.loads(
        (REPO_ROOT / "schemas" / "plan.schema.json").read_text(encoding="utf-8")
    )
    return Draft202012Validator({"$ref": "#/$defs/run_value", "$defs": schema["$defs"]})


@pytest.mark.parametrize(("version", "taken"), [(3, True), (2, False)])
def test_an_environment_lock_is_taken_only_at_version_3(
    version: int, taken: bool
) -> None:
    value: dict[str, Any] = {
        "environment": {"version": version, "prefix": "/env", "platforms": {}}
    }
    assert run_value().is_valid(value) is taken
