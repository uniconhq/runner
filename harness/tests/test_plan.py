"""The checks on a plan that its schema cannot express."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from tests.support import EXAMPLES, classic_plan
from unicon_harness import exact
from unicon_harness.faults import GradingError
from unicon_harness.plan import Output, parse, pieces

TESTS = ["main/1", "main/2"]


def _refusal(document: dict[str, Any]) -> str:
    with pytest.raises(GradingError) as refused:
        parse(document)
    return refused.value.message


def test_the_example_plan_parses_into_its_steps() -> None:
    plan = parse(exact.loads((EXAMPLES / "plan.json").read_bytes()))

    assert plan.tests == ("main/1", "main/2", "samples/1")
    kinds = [s.kind for s in plan.steps]
    assert kinds == ["once", "batch", "batch", "test", "test", "test"]
    assert plan.per_test("run") and plan.per_test("judge")
    assert not plan.per_test("compile")
    assert plan.tests_of("judge") == ["main/1", "main/2", "samples/1"]
    assert plan.steps[3].label == "judge for test main/1"
    assert plan.steps[3].network is True
    assert plan.steps[0].folders == frozenset({"source"})
    assert plan.steps[3].outputs["fraction"] == Output("number", optional=True)
    assert plan.steps[3].outputs["outcome"] == Output("outcome", optional=False)
    assert plan.contestant["answers"].per_test
    assert plan.contestant["language"].options == ("c", "cpp", "java", "python")
    assert plan.report["fraction"].at_most == 1
    assert plan.report["log"].at_least is None


def test_bounds_are_read_exactly_as_written() -> None:
    document = classic_plan(TESTS)
    text = json.dumps(document).replace('"at_least": 0', '"at_least": 0.1', 1)

    plan = parse(exact.loads(text))

    assert plan.report["time_ms"].at_least == Decimal("0.1")


def test_a_plan_with_a_number_that_is_not_finite_is_refused() -> None:
    text = json.dumps(classic_plan(TESTS)).replace('"at_least": 0', '"at_least": NaN')
    assert "not finite" in _refusal(exact.loads(text))


def test_a_plan_of_another_version_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["schema_version"] = 4
    assert "schema_version 4" in _refusal(plan)


def test_a_plan_with_a_stage_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["stage"] = "default"
    assert "plan.schema.json" in _refusal(plan)


def test_two_steps_with_one_id_are_refused_unless_they_are_one_per_test_step() -> None:
    plan = classic_plan(TESTS)
    plan["steps"].append(dict(plan["steps"][1]))
    assert "more than one step with id 'run'" in _refusal(plan)


def test_a_step_that_runs_once_after_a_per_test_step_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["steps"].append(dict(plan["steps"][0], id="late"))
    assert "step late runs once after a step that runs per test" in _refusal(plan)


def test_a_test_outside_the_plan_list_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["tests"] = ["main/1"]
    assert "not in the plan's tests" in _refusal(plan)


def test_one_test_run_twice_by_a_step_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["steps"][2]["test"] = "main/2"
    assert "runs test 'main/2' twice" in _refusal(plan)


def test_a_reference_to_a_later_step_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["steps"][0]["inputs"]["source"] = {"step": "run", "output": "output"}
    assert "does not run before it" in _refusal(plan)


def test_a_reference_to_a_step_the_plan_does_not_have_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["steps"][1]["batch"][0]["inputs"]["binary"]["step"] = "build"
    assert "reads step 'build', which does not run before it" in _refusal(plan)


def test_a_step_that_runs_once_reading_a_per_test_step_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["steps"].append(
        dict(
            plan["steps"][0],
            id="score",
            inputs={"results": {"step": "run", "output": "outcome"}},
        )
    )
    assert "reads step run, which runs per test, and step score runs once" in (
        _refusal(plan)
    )


def test_a_reference_to_an_output_the_step_does_not_declare_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["steps"][1]["batch"][0]["inputs"]["binary"]["output"] = "program"
    assert "reads output program of step compile, which it does not" in (_refusal(plan))


def test_a_contestant_input_the_plan_does_not_declare_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["steps"][0]["inputs"]["seed"] = {"submission": "seed"}
    assert "contestant inputs do not declare" in _refusal(plan)


def test_a_per_test_input_read_by_a_step_that_runs_once_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["contestant"]["answers"] = {"type": "file", "per_test": True}
    plan["steps"][0]["inputs"]["answers"] = {"submission": "answers"}
    assert "gives one file per test, and step compile runs once" in _refusal(plan)


@pytest.mark.parametrize(
    ("template", "parts", "found"),
    [
        ("--seed {0", 1, "a lone {"),
        ("--seed 0}", 1, "a lone }"),
        ("--seed {a}", 1, "a lone {"),
        ("--seed {1}", 1, "names a part it does not have"),
    ],
)
def test_a_malformed_template_is_refused(template: str, parts: int, found: str) -> None:
    plan = classic_plan(TESTS)
    plan["contestant"]["seed"] = {"type": "number"}
    plan["steps"][0]["inputs"]["args"] = {
        "template": template,
        "parts": [{"submission": "seed"}] * parts,
    }
    assert found in _refusal(plan)


def test_a_template_part_that_is_a_file_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["steps"][0]["inputs"]["args"] = {
        "template": "{0}",
        "parts": [{"submission": "submission"}],
    }
    assert "not text, a number, a boolean or an enum" in _refusal(plan)


def test_a_template_is_its_text_and_its_parts() -> None:
    assert pieces("--seed {0} {{off}} {1}}}") == [
        "--seed ",
        0,
        " ",
        "{",
        "off",
        "}",
        " ",
        1,
        "}",
    ]
    assert pieces("") == []
    assert pieces("{{}}") == ["{", "}"]


def test_a_report_naming_an_unknown_step_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["report"]["score"] = {"step": "grade", "output": "points"}
    assert "reads step 'grade', which the plan does not have" in _refusal(plan)


def test_a_report_naming_an_undeclared_output_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["report"]["score"] = {"step": "check", "output": "points"}
    assert "reads output points of step check, which it does not" in _refusal(plan)


def test_a_report_of_a_file_is_refused() -> None:
    plan = classic_plan(TESTS)
    plan["report"]["out"] = {"step": "run", "output": "output"}
    assert "a file, which a result cannot carry" in _refusal(plan)


def test_bounds_on_a_text_are_refused() -> None:
    plan = classic_plan(TESTS)
    plan["report"]["log"]["at_most"] = 3
    assert "gives bounds to a text, not a number" in _refusal(plan)


def test_a_plan_with_one_step_and_no_report_is_fine() -> None:
    plan = classic_plan(TESTS)
    plan["steps"] = plan["steps"][:1]
    plan["report"] = {}
    parsed = parse(plan)
    assert parsed.report == {}
    assert parsed.steps[0].kind == "once"
