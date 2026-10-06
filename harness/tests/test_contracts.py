"""What the plan, primitive and submission schemas accept and refuse. The
result has its own file; the envelope is tested through the harness that
reads it.
"""

from __future__ import annotations

import copy
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


@pytest.fixture
def declaration() -> dict[str, Any]:
    return _yaml("primitive.yaml")


def test_the_example_plan_is_accepted(plan: dict[str, Any]) -> None:
    assert violation(plan, "plan") is None


def test_a_step_without_a_digest_is_refused(plan: dict[str, Any]) -> None:
    plan["steps"][0]["image"] = "ghcr.io/uniconhq/primitive-compile:v2"
    assert violation(plan, "plan") is not None


def test_a_harness_image_by_tag_is_refused(plan: dict[str, Any]) -> None:
    plan["harness_image"] = "ghcr.io/uniconhq/harness:v0.6.0"
    assert violation(plan, "plan") is not None


def test_a_local_registry_with_a_port_is_a_digest_reference(
    plan: dict[str, Any],
) -> None:
    plan["harness_image"] = f"localhost:5000/uniconhq/harness@sha256:{'a' * 64}"
    assert violation(plan, "plan") is None


@pytest.mark.parametrize(
    "limit", ["time_ms", "cpu_ms", "memory_mb", "pids", "output_mb", "gpus"]
)
def test_a_step_missing_any_limit_is_refused(plan: dict[str, Any], limit: str) -> None:
    del plan["steps"][1]["limits"][limit]
    assert violation(plan, "plan") is not None


@pytest.mark.parametrize("field", ["network", "outputs"])
def test_a_step_says_whether_it_has_the_network_and_what_it_writes(
    plan: dict[str, Any], field: str
) -> None:
    del plan["steps"][0][field]
    assert violation(plan, "plan") is not None


def test_every_step_declares_its_outcome(plan: dict[str, Any]) -> None:
    del plan["steps"][0]["outputs"]["outcome"]
    assert violation(plan, "plan") is not None
    plan["steps"][0]["outputs"]["outcome"] = "text"
    assert violation(plan, "plan") is not None


def test_an_optional_output_is_marked_after_its_name(plan: dict[str, Any]) -> None:
    plan["steps"][0]["outputs"]["binary?"] = plan["steps"][0]["outputs"].pop("binary")
    assert violation(plan, "plan") is None
    plan["steps"][0]["outputs"]["?binary"] = "file"
    assert violation(plan, "plan") is not None


def test_a_step_with_both_inputs_and_a_batch_is_refused(plan: dict[str, Any]) -> None:
    plan["steps"][1]["inputs"] = {}
    assert violation(plan, "plan") is not None


def test_a_batch_step_naming_one_test_is_refused(plan: dict[str, Any]) -> None:
    plan["steps"][1]["test"] = "main/1"
    assert violation(plan, "plan") is not None


@pytest.mark.parametrize("test", ["1", "main", "main/1/2", "main/a.b", "/1"])
def test_a_test_id_is_a_group_and_a_test(plan: dict[str, Any], test: str) -> None:
    plan["tests"][0] = test
    assert violation(plan, "plan") is not None


@pytest.mark.parametrize(
    "value",
    [
        {"value": 1, "task": "x"},
        {"task": "../secret"},
        {"task": "/etc/passwd"},
        {"task": "data//1.in"},
        {"task": "data/./1.in"},
        {"task": []},
        {"submission": "submission", "field": "language"},
        {"step": "compile"},
        {"step": "compile", "output": "binary", "test": "main/1"},
        {"value": None},
        {"value": [1]},
        {"secret": ""},
        {"template": "{0}"},
        {"template": "{0}", "parts": []},
        {"template": "{0}", "parts": [{"value": 1}]},
    ],
)
def test_an_input_value_is_exactly_one_known_form(
    plan: dict[str, Any], value: dict[str, Any]
) -> None:
    plan["steps"][0]["inputs"]["source"] = value
    assert violation(plan, "plan") is not None


@pytest.mark.parametrize(
    "value",
    [
        {"task": "rubric/"},
        {"task": "tests/main/1/input"},
        {"secret": "model-key"},
        {"template": "{{0}}", "parts": [{"submission": "seed"}]},
    ],
)
def test_the_value_forms_a_plan_carries(
    plan: dict[str, Any], value: dict[str, Any]
) -> None:
    plan["steps"][0]["inputs"]["source"] = value
    assert violation(plan, "plan") is None


def test_a_per_test_contestant_input_is_a_file(plan: dict[str, Any]) -> None:
    plan["contestant"]["seed"]["per_test"] = True
    assert violation(plan, "plan") is not None


def test_an_enum_contestant_input_carries_its_options(plan: dict[str, Any]) -> None:
    del plan["contestant"]["language"]["options"]
    assert violation(plan, "plan") is not None


def test_a_report_entry_names_a_step_and_an_output(plan: dict[str, Any]) -> None:
    plan["report"]["time_ms"] = {"step": "run"}
    assert violation(plan, "plan") is not None
    plan["report"]["time_ms"] = {"step": "run", "output": "time_ms", "fold": "sum"}
    assert violation(plan, "plan") is not None


def test_a_plan_of_the_previous_version_is_refused(plan: dict[str, Any]) -> None:
    plan["schema_version"] = 4
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
    assert violation({"schema_version": 5}, "primitive", "outputs_file") is not None


@pytest.mark.parametrize(
    "path",
    ["in/../../etc/passwd", "out/../../../host", "in//abs", "out/./x", "work/x", "out"],
)
def test_a_file_or_folder_value_cannot_leave_its_directory(path: str) -> None:
    for kind in ("file", "folder"):
        outputs = {"schema_version": 5, "outputs": {"binary": {kind: path}}}
        assert violation(outputs, "primitive", "outputs_file") is not None


def test_outputs_carry_either_one_run_or_a_batch() -> None:
    both = {"schema_version": 5, "outputs": {}, "batch": []}
    assert violation(both, "primitive", "outputs_file") is not None


def test_a_value_is_never_a_list() -> None:
    files = {"schema_version": 5, "outputs": {"x": [{"file": "out/a"}]}}
    assert violation(files, "primitive", "outputs_file") is not None
    texts = {"schema_version": 5, "outputs": {"x": ["a", "b"]}}
    assert violation(texts, "primitive", "outputs_file") is not None


def test_a_batch_item_is_keyed_by_its_test() -> None:
    item = {"id": "main/1", "inputs": {}}
    batch = {"schema_version": 5, "batch": [item]}
    assert violation(batch, "primitive", "inputs_file") is not None


def test_a_declaration_without_an_image_is_refused(
    declaration: dict[str, Any],
) -> None:
    del declaration["image"]
    assert violation(declaration, "primitive", "declaration") is not None


@pytest.mark.parametrize("field", ["name", "version", "schema_version"])
def test_a_declaration_names_neither_itself_nor_its_version(
    declaration: dict[str, Any], field: str
) -> None:
    declaration[field] = "v2"
    assert violation(declaration, "primitive", "declaration") is not None


def test_an_enum_carries_its_options_and_nothing_else_does(
    declaration: dict[str, Any],
) -> None:
    enum = copy.deepcopy(declaration)
    del enum["inputs"]["mode"]["options"]
    assert violation(enum, "primitive", "declaration") is not None
    text = copy.deepcopy(declaration)
    text["inputs"]["api_key"]["options"] = ["a"]
    assert violation(text, "primitive", "declaration") is not None


def test_every_file_or_folder_input_says_whether_it_runs(
    declaration: dict[str, Any],
) -> None:
    unmarked = copy.deepcopy(declaration)
    del unmarked["inputs"]["binary"]["runs"]
    assert violation(unmarked, "primitive", "declaration") is not None
    folder = copy.deepcopy(declaration)
    del folder["inputs"]["data"]["runs"]
    assert violation(folder, "primitive", "declaration") is not None
    scalar = copy.deepcopy(declaration)
    scalar["inputs"]["time_limit"]["runs"] = False
    assert violation(scalar, "primitive", "declaration") is not None


def test_only_an_input_is_a_secret(declaration: dict[str, Any]) -> None:
    declaration["outputs"]["output"]["secret"] = True
    assert violation(declaration, "primitive", "declaration") is not None


def test_a_declared_type_is_one_of_the_six_or_outcome(
    declaration: dict[str, Any],
) -> None:
    declaration["outputs"]["output"]["type"] = "file[]"
    assert violation(declaration, "primitive", "declaration") is not None
    declaration["outputs"]["output"]["type"] = "outcome"
    assert violation(declaration, "primitive", "declaration") is not None


def test_a_declaration_carries_all_six_limits(declaration: dict[str, Any]) -> None:
    del declaration["limits"]["gpus"]
    assert violation(declaration, "primitive", "declaration") is not None


def test_the_compile_declaration_of_the_spec_is_accepted() -> None:
    text = (
        f"image: ghcr.io/uniconhq/primitive-compile@sha256:{'0' * 64}\n"
        "limits: {time_ms: 60000, cpu_ms: 60000, memory_mb: 1024, pids: 128, "
        "output_mb: 64, gpus: 0}\n"
        "inputs:\n"
        "  source: {type: folder, runs: true}\n"
        "  entry: {type: text, optional: true}\n"
        "  language: {type: enum, options: [python, cpp, c, java]}\n"
        "outputs:\n"
        "  binary: {type: file}\n"
        "  compile_log: {type: text}\n"
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
        {"files": ["files/submission/./x"]},
        {"files": ["files/submission"]},
        {"value": [1]},
        {"value": None},
        {"files": ["files/a/b"], "value": 1},
        {"files": ["files/a/b"], "language": "python"},
    ],
)
def test_a_submission_entry_is_files_or_a_value(entry: dict[str, Any]) -> None:
    document = {"schema_version": 5, "inputs": {"submission": entry}}
    assert violation(document, "submission") is not None


def test_a_folder_input_may_hold_nested_paths() -> None:
    entry = {"files": ["files/submission/main.py", "files/submission/lib/a.py"]}
    document = {"schema_version": 5, "inputs": {"submission": entry}}
    assert violation(document, "submission") is None
