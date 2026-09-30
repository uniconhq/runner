"""What the plan, primitive and submission schemas accept and refuse. The
verdict has its own file; the envelope is tested through the harness that
reads it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.support import EXAMPLES, example
from unicon_harness.contracts import violation


def _yaml(name: str) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load((EXAMPLES / name).read_text("utf-8"))
    return document


@pytest.fixture
def plan() -> dict[str, Any]:
    return example("plan.json")


def test_the_example_plan_for_the_classic_workflow_is_accepted(
    plan: dict[str, Any],
) -> None:
    assert violation(plan, "plan") is None


def test_a_step_without_a_digest_is_refused(plan: dict[str, Any]) -> None:
    plan["steps"][0]["image"] = "ghcr.io/uniconhq/primitive-compile:v1"
    assert violation(plan, "plan") is not None


def test_a_harness_image_by_tag_is_refused(plan: dict[str, Any]) -> None:
    plan["harness_image"] = "ghcr.io/uniconhq/harness:v0.3.0"
    assert violation(plan, "plan") is not None


def test_a_local_registry_with_a_port_is_a_digest_reference(
    plan: dict[str, Any],
) -> None:
    plan["harness_image"] = f"localhost:5000/uniconhq/harness@sha256:{'a' * 64}"
    assert violation(plan, "plan") is None


@pytest.mark.parametrize(
    "limit", ["time_ms", "cpu_ms", "memory_mb", "pids", "output_mb"]
)
def test_a_step_missing_any_limit_is_refused(plan: dict[str, Any], limit: str) -> None:
    del plan["steps"][1]["limits"][limit]
    assert violation(plan, "plan") is not None


def test_a_step_with_both_inputs_and_a_batch_is_refused(plan: dict[str, Any]) -> None:
    plan["steps"][1]["inputs"] = {}
    assert violation(plan, "plan") is not None


def test_a_batch_step_naming_one_test_is_refused(plan: dict[str, Any]) -> None:
    plan["steps"][1]["test"] = "1"
    assert violation(plan, "plan") is not None


@pytest.mark.parametrize(
    "value",
    [
        {"value": 1, "task": "x"},
        {"task": "../secret"},
        {"task": "/etc/passwd"},
        {"task": "data//1.in"},
        {"task": []},
        {"submission": "submission", "field": "files"},
        {"step": "compile"},
        {"value": None},
    ],
)
def test_an_input_value_is_exactly_one_known_form(
    plan: dict[str, Any], value: dict[str, Any]
) -> None:
    plan["steps"][0]["inputs"]["source"] = value
    assert violation(plan, "plan") is not None


def test_the_verdict_needs_an_outcome(plan: dict[str, Any]) -> None:
    del plan["verdict"]["outcome"]
    assert violation(plan, "plan") is not None


def test_a_plan_of_the_previous_version_is_refused(plan: dict[str, Any]) -> None:
    plan["schema_version"] = 2
    assert violation(plan, "plan") is not None


def test_every_primitive_example_is_accepted() -> None:
    for name in (
        "primitive-inputs.json",
        "primitive-inputs-batch.json",
        "primitive-outputs.json",
        "primitive-outputs-batch.json",
        "primitive-outputs-error.json",
    ):
        assert violation(example(name), "primitive") is None, name
    assert violation(_yaml("primitive.yaml"), "primitive") is None


def test_a_primitive_that_writes_no_outputs_fails_the_check() -> None:
    assert violation({"schema_version": 3}, "primitive", "outputs_file") is not None


@pytest.mark.parametrize(
    "path",
    ["in/../../etc/passwd", "out/../../../host", "in//abs", "out/./x", "work/x", "out"],
)
def test_a_file_value_cannot_leave_its_directory(path: str) -> None:
    outputs = {"schema_version": 3, "outputs": {"binary": {"file": path}}}
    assert violation(outputs, "primitive", "outputs_file") is not None


def test_outputs_carry_either_one_run_or_a_batch() -> None:
    both = {"schema_version": 3, "outputs": {}, "batch": []}
    assert violation(both, "primitive", "outputs_file") is not None


def test_a_list_value_is_all_files_or_all_plain_values() -> None:
    mixed = {"schema_version": 3, "outputs": {"x": [{"file": "out/a"}, "b"]}}
    assert violation(mixed, "primitive", "outputs_file") is not None


def test_a_declaration_without_an_image_is_refused() -> None:
    declaration = _yaml("primitive.yaml")
    del declaration["image"]
    assert violation(declaration, "primitive", "declaration") is not None


def test_an_enum_carries_its_values_and_nothing_else_does() -> None:
    declaration = _yaml("primitive.yaml")
    enum = copy.deepcopy(declaration)
    del enum["inputs"]["mode"]["values"]
    assert violation(enum, "primitive", "declaration") is not None
    text = copy.deepcopy(declaration)
    text["inputs"]["note"]["values"] = ["a"]
    assert violation(text, "primitive", "declaration") is not None


def test_a_declared_type_is_one_of_the_seven() -> None:
    declaration = _yaml("primitive.yaml")
    declaration["outputs"]["output"]["type"] = "directory"
    assert violation(declaration, "primitive", "declaration") is not None


def test_the_contract_example_compile_declaration_is_accepted(tmp_path: Path) -> None:
    """The declaration exactly as the contract note writes it for compile."""
    text = (
        "name: unicon/compile\n"
        "version: v1\n"
        f"image: ghcr.io/uniconhq/primitive-compile@sha256:{'0' * 64}\n"
        "entrypoint: [/usr/local/bin/compile]\n"
        "batch: false\n"
        "limits: {time_ms: 60000, cpu_ms: 60000, memory_mb: 1024, pids: 128, "
        "output_mb: 64}\n"
        "limits_from: {}\n"
        "inputs:\n"
        "  source: {type: file}\n"
        "  language: {type: enum, values: [python, cpp, c, java]}\n"
        "outputs:\n"
        "  binary: {type: file, optional: true}\n"
        "  compile_log: {type: text}\n"
        "  outcome: {type: outcome}\n"
    )
    assert violation(yaml.safe_load(text), "primitive", "declaration") is None


def test_sandbox_runs_own_declaration_is_accepted() -> None:
    """limits_from without a scale, as the sandbox-run repo writes it."""
    text = (
        "name: unicon/sandbox-run\n"
        "version: v1\n"
        f"image: ghcr.io/uniconhq/primitive-sandbox-run@sha256:{'0' * 64}\n"
        "entrypoint: [/usr/local/bin/sandbox-run]\n"
        "batch: true\n"
        "limits: {time_ms: 5000, cpu_ms: 5000, memory_mb: 256, pids: 128, "
        "output_mb: 64}\n"
        "limits_from:\n"
        "  time_ms: {input: time_limit, scale: 2000, add: 3000}\n"
        "  cpu_ms: {input: time_limit, scale: 2000, add: 3000}\n"
        "  memory_mb: {input: memory_limit, add: 256}\n"
        "inputs:\n"
        "  binary: {type: file}\n"
        "  input: {type: file}\n"
        "  time_limit: {type: number}\n"
        "  memory_limit: {type: number}\n"
        "outputs:\n"
        "  output: {type: file}\n"
        "  time_ms: {type: number}\n"
        "  memory_kb: {type: number}\n"
        "  outcome: {type: outcome}\n"
    )
    assert violation(yaml.safe_load(text), "primitive", "declaration") is None


def test_the_example_submission_is_accepted() -> None:
    assert violation(example("submission.json"), "submission") is None


@pytest.mark.parametrize(
    "entry",
    [
        {"files": []},
        {"files": ["main.py"]},
        {"files": ["files/submission/../x"]},
        {"files": ["files/submission/dir/main.py"]},
        {"value": [1]},
        {"files": ["files/a/b"], "value": 1},
    ],
)
def test_a_submission_entry_is_files_or_a_value(entry: dict[str, Any]) -> None:
    document = {"schema_version": 3, "inputs": {"submission": entry}}
    assert violation(document, "submission") is not None
