"""The checks on a plan that its schema cannot express."""

from __future__ import annotations

from typing import Any

import pytest

from tests.support import classic_plan, example
from unicon_harness.faults import GradingError
from unicon_harness.plan import parse


def _refusal(document: dict[str, Any], stage: str = "default") -> str:
    with pytest.raises(GradingError) as refused:
        parse(document, stage)
    return refused.value.message


def test_the_example_plan_parses_into_its_steps() -> None:
    plan = parse(example("plan.json"), "default")

    assert plan.tests == ("1", "2", "10")
    assert [s.kind for s in plan.steps] == ["once", "batch", "test", "test", "test"]
    assert plan.per_test("run") and plan.per_test("check")
    assert not plan.per_test("compile")
    assert plan.tests_of("check") == ["1", "2", "10"]
    assert plan.steps[2].label == "check for test 1"


def test_a_plan_for_another_stage_is_refused() -> None:
    assert "for stage 'default'" in _refusal(classic_plan(["1"]), "final")


def test_two_steps_with_one_id_are_refused_unless_they_are_one_foreach() -> None:
    plan = classic_plan(["1"])
    plan["steps"].append(dict(plan["steps"][0]))
    assert "more than one step with id 'compile'" in _refusal(plan)


def test_a_test_outside_the_plan_list_is_refused() -> None:
    plan = classic_plan(["1", "2"])
    plan["tests"] = ["1"]
    assert "not in the plan's tests" in _refusal(plan)


def test_one_test_run_twice_by_a_step_is_refused() -> None:
    plan = classic_plan(["1", "2"])
    plan["steps"][2]["test"] = "2"
    assert "runs test '2' twice" in _refusal(plan)


def test_a_reference_to_a_later_step_is_refused() -> None:
    plan = classic_plan(["1"])
    plan["steps"][0]["inputs"]["source"] = {"step": "run", "output": "output"}
    assert "does not run before it" in _refusal(plan)


def test_a_reference_for_one_test_to_a_run_once_step_is_refused() -> None:
    plan = classic_plan(["1"])
    plan["steps"][1]["batch"][0]["inputs"]["binary"]["test"] = "1"
    assert "but it runs once" in _refusal(plan)


def test_a_reference_for_a_test_the_step_does_not_run_is_refused() -> None:
    plan = classic_plan(["1", "2"])
    plan["steps"][1]["batch"].pop()
    assert "which it does not run" in _refusal(plan)


def test_a_verdict_naming_an_unknown_step_is_refused() -> None:
    plan = classic_plan(["1"])
    plan["verdict"]["summary"] = {"step": "report", "output": "text"}
    assert "does not have" in _refusal(plan)


def test_a_plan_with_no_tests_and_one_step_is_fine() -> None:
    plan = classic_plan([])
    plan["steps"] = plan["steps"][:1]
    plan["verdict"] = {"outcome": {"step": "compile", "output": "outcome"}}
    parsed = parse(plan, "default")
    assert parsed.tests == ()
    assert parsed.verdict.summary is None
