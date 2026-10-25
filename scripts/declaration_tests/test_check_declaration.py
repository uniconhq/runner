"""check_declaration.py against a primitive repository laid out as each
primitive-<name> repo is: primitive.yaml at the root, with no image line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import check_declaration

REPO_ROOT = Path(__file__).parents[2]

DECLARATION = """\
batch: false
limits:
  {time_ms: 60000, cpu_ms: 60000, memory_mb: 1024, pids: 128, output_mb: 64, gpus: 0}
inputs:
  source: {type: folder, runs: true}
  entry: {type: text, optional: true}
  language: {type: enum, options: [python, c, cpp, java]}
outputs:
  binary: {type: file}
  compile_log: {type: text}
  outcome: {type: outcome}
"""


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (REPO_ROOT / "schemas" / "primitive.schema.json").read_text(encoding="utf-8")
    )
    return document


def _repo(tmp_path: Path, declaration: str) -> Path:
    (tmp_path / "primitive.yaml").write_text(declaration, encoding="utf-8")
    return tmp_path


def test_a_declaration_without_an_image_conforms(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    """The placeholder digest stands in for the image bootstrap writes."""
    assert check_declaration.problems(schema, _repo(tmp_path, DECLARATION)) == []


def test_an_option_yaml_1_1_would_read_as_false_is_the_text_no(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    """Read as the forge reads it, YAML 1.2, `no` and `on` stay text, so the
    options are text as the contract asks.
    """
    text = DECLARATION.replace("[python, c, cpp, java]", "[yes, no, on, NO]")
    assert check_declaration.problems(schema, _repo(tmp_path, text)) == []
    assert check_declaration.load_declaration(tmp_path)["inputs"]["language"][
        "options"
    ] == ["yes", "no", "on", "NO"]


def test_a_key_given_twice_is_a_problem(schema: dict[str, Any], tmp_path: Path) -> None:
    text = DECLARATION + "batch: true\n"
    assert check_declaration.problems(schema, _repo(tmp_path, text)) == [
        "(top): does not parse as YAML: the key 'batch' is given twice"
    ]


def test_an_image_line_is_refused(schema: dict[str, Any], tmp_path: Path) -> None:
    """The image comes from the release manifest, never from the repo file."""
    text = DECLARATION + "image: ghcr.io/x/y@sha256:" + "1" * 64 + "\n"
    found = check_declaration.problems(schema, _repo(tmp_path, text))
    assert found == [
        "primitive.yaml has an image line; the release manifest supplies it"
    ]


@pytest.mark.parametrize("line", ["name: unicon/compile\n", "version: v2\n"])
def test_the_declaration_names_neither_the_primitive_nor_its_version(
    schema: dict[str, Any], tmp_path: Path, line: str
) -> None:
    """The repo at the forge is the name and its tag the version."""
    found = check_declaration.problems(schema, _repo(tmp_path, line + DECLARATION))
    assert len(found) == 1
    assert line.split(":")[0] in found[0]


def test_a_primitive_and_its_ports_may_say_what_they_are_for(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    """A title and descriptions are for the workflow editor; each is one line."""
    text = (
        "title: Compile\n"
        "description: Compiles the contestant's source into a program.\n"
        + DECLARATION.replace(
            "{type: folder, runs: true}",
            "{type: folder, runs: true, description: The source files.}",
        )
        .replace("{type: file}", "{type: file, description: The program built.}")
        .replace(
            "{type: outcome}", "{type: outcome, description: Whether it compiled.}"
        )
    )
    assert check_declaration.problems(schema, _repo(tmp_path, text)) == []


@pytest.mark.parametrize(
    "line",
    [
        'title: ""\n',
        "title: '   '\n",
        'description: "Two\\nlines."\n',
        "description: 3\n",
    ],
)
def test_a_title_or_description_that_is_not_one_line_of_text_is_refused(
    schema: dict[str, Any], tmp_path: Path, line: str
) -> None:
    found = check_declaration.problems(schema, _repo(tmp_path, line + DECLARATION))
    assert len(found) == 1
    assert line.split(":")[0] in found[0]


def test_the_outcome_port_may_say_what_each_outcome_it_reports_means(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    """Each outcome the step can report, with when, for the workflow editor."""
    text = DECLARATION.replace(
        "outcome: {type: outcome}",
        "outcome:\n"
        "    type: outcome\n"
        "    outcomes:\n"
        "      accepted: the source compiles\n"
        "      compile_error: it does not compile\n",
    )
    assert check_declaration.problems(schema, _repo(tmp_path, text)) == []


@pytest.mark.parametrize(
    "outcomes",
    [
        "{skipped: it did not run}",
        "{system_error: it broke}",
        "{finished: it finished}",
        "{accepted: ''}",
        '{accepted: "two\\nlines"}',
        "{}",
    ],
)
def test_outcomes_name_only_what_a_step_may_report_each_in_one_line(
    schema: dict[str, Any], tmp_path: Path, outcomes: str
) -> None:
    text = DECLARATION.replace(
        "outcome: {type: outcome}", f"outcome: {{type: outcome, outcomes: {outcomes}}}"
    )
    found = check_declaration.problems(schema, _repo(tmp_path, text))
    assert len(found) == 1
    assert "outcome" in found[0]


def test_outcomes_are_only_on_the_outcome_port(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    text = DECLARATION.replace(
        "compile_log: {type: text}",
        "compile_log: {type: text, outcomes: {accepted: it compiled}}",
    )
    found = check_declaration.problems(schema, _repo(tmp_path, text))
    assert len(found) == 1
    assert "compile_log" in found[0]


def test_a_file_input_without_its_runs_mark_is_refused(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    text = DECLARATION.replace("{type: folder, runs: true}", "{type: folder}")
    assert check_declaration.problems(schema, _repo(tmp_path, text)) != []


def test_a_field_the_contract_does_not_know_is_refused(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    text = DECLARATION + "entrypoint: [/usr/local/bin/compile]\n"
    found = check_declaration.problems(schema, _repo(tmp_path, text))
    assert len(found) == 1
    assert "entrypoint" in found[0]


DECLARATION_V6 = """\
schema_version: 6
batch: true
limits:
  {time_ms: 5000, cpu_ms: 5000, memory_mb: 256, pids: 128, output_mb: 64, gpus: 0}
outputs_from: manifest
inputs:
  program: {type: file, runs: true}
  files: {type: named_files, runs: false, optional: true, names: [output, answer]}
  manifest: {type: file, runs: false}
outputs:
  time_ms: {type: number, better: lower, at_least: 0}
  score: {type: number, better: higher, at_least: 0, at_most: 1}
  outcome: {type: outcome}
"""


def test_a_version_6_declaration_with_each_new_key_conforms(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    assert check_declaration.problems(schema, _repo(tmp_path, DECLARATION_V6)) == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("better: lower", "better: faster"),
        ("at_least: 0, at_most: 1", "at_least: 2, at_most: 1"),
        ("names: [output, answer]", "names: [../x]"),
        ("schema_version: 6", "schema_version: 5"),
    ],
)
def test_a_version_6_declaration_that_breaks_a_rule_is_refused(
    schema: dict[str, Any], tmp_path: Path, old: str, new: str
) -> None:
    text = DECLARATION_V6.replace(old, new)
    assert text != DECLARATION_V6
    assert check_declaration.problems(schema, _repo(tmp_path, text)) != []


def test_bounds_that_cross_are_named(schema: dict[str, Any], tmp_path: Path) -> None:
    text = DECLARATION_V6.replace(
        "at_least: 0, at_most: 1", "at_least: 2.5, at_most: 1"
    )
    assert check_declaration.problems(schema, _repo(tmp_path, text)) == [
        "outputs/score: at_least 2.5 is above at_most 1"
    ]


@pytest.mark.parametrize("bound", [".inf", "-.inf", ".nan"])
def test_a_bound_that_is_not_finite_is_named(
    schema: dict[str, Any], tmp_path: Path, bound: str
) -> None:
    text = DECLARATION_V6.replace("at_most: 1", f"at_most: {bound}")
    [found] = check_declaration.problems(schema, _repo(tmp_path, text))
    assert found.startswith("outputs/score/at_most: ")
    assert found.endswith(" is not a finite number")


def test_equal_bounds_conform(schema: dict[str, Any], tmp_path: Path) -> None:
    text = DECLARATION_V6.replace("at_least: 0, at_most: 1", "at_least: 1, at_most: 1")
    assert check_declaration.problems(schema, _repo(tmp_path, text)) == []


DECLARATION_PROGRAMS = """\
schema_version: 6
batch: true
limits:
  {time_ms: 4000, cpu_ms: 4000, memory_mb: 512, pids: 1056, output_mb: 64, gpus: 0}
limits_from:
  time_ms: {max: programs.time_limit, scale: 4000, add: 6000}
  memory_mb: {sum: programs.memory_limit, add: 512}
outputs_from: programs
inputs:
  programs:
    type: programs
    inputs:
      program: {type: file, runs: true}
      files: {type: named_files, runs: false, optional: true}
      time_limit: {type: number}
      memory_limit: {type: number}
      manifest: {type: file, runs: false, optional: true}
      stops_others: {type: boolean, optional: true}
    outputs:
      time_ms: {type: number, better: lower, at_least: 0, at_most: 100}
      ended: {type: enum, options: [finished, time_limit]}
      stopped_by: {type: text, optional: true}
  links: {type: links, optional: true}
outputs:
  facts: {type: file}
  outcome: {type: outcome}
"""


def test_a_declaration_with_programs_conforms(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    found = check_declaration.problems(schema, _repo(tmp_path, DECLARATION_PROGRAMS))
    assert found == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("schema_version: 6", "schema_version: 5"),
        ("    type: programs\n", "    type: programs\n    runs: true\n"),
        ("    outputs:\n      time_ms", "    output:\n      time_ms"),
        ("max: programs.time_limit", "input: programs.time_limit"),
        ("max: programs.time_limit,", "max: programs.time_limit, input: x,"),
        ("max: programs.time_limit", "max: time_limit"),
        ("      program: {type: file, runs: true}", "      inner: {type: links}"),
        (
            "  links: {type: links, optional: true}",
            "  links: {type: links, runs: true}",
        ),
        ("  facts: {type: file}", "  facts: {type: file, inputs: {}}"),
        ("      manifest: {type: file, runs: false, optional: true}\n", ""),
    ],
)
def test_a_declaration_with_programs_that_breaks_a_rule_is_refused(
    schema: dict[str, Any], tmp_path: Path, old: str, new: str
) -> None:
    text = DECLARATION_PROGRAMS.replace(old, new)
    assert text != DECLARATION_PROGRAMS
    assert check_declaration.problems(schema, _repo(tmp_path, text)) != []


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        (
            "sum: programs.memory_limit",
            "sum: links.memory_limit",
            "limits_from/memory_mb/sum: links is not a programs port",
        ),
        (
            "max: programs.time_limit",
            "max: programs.files",
            "limits_from/time_ms/max: programs has no number input files",
        ),
        (
            "max: programs.time_limit",
            "max: programs.wall",
            "limits_from/time_ms/max: programs has no number input wall",
        ),
        (
            "at_least: 0, at_most: 100",
            "at_least: 200, at_most: 100",
            "inputs/programs/outputs/time_ms: at_least 200 is above at_most 100",
        ),
    ],
)
def test_what_a_programs_port_says_of_another_is_checked(
    schema: dict[str, Any], tmp_path: Path, old: str, new: str, problem: str
) -> None:
    text = DECLARATION_PROGRAMS.replace(old, new)
    assert text != DECLARATION_PROGRAMS
    assert check_declaration.problems(schema, _repo(tmp_path, text)) == [problem]


def test_two_programs_ports_are_named(schema: dict[str, Any], tmp_path: Path) -> None:
    text = DECLARATION_PROGRAMS.replace(
        "  links: {type: links, optional: true}",
        "  links: {type: links, optional: true}\n  more: {type: programs, "
        "inputs: {}, outputs: {}}",
    )
    found = check_declaration.problems(schema, _repo(tmp_path, text))
    assert "inputs: programs, more are each a programs port" in found


def test_a_programs_port_must_be_named_programs(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    text = DECLARATION_PROGRAMS.replace("  programs:\n", "  players:\n").replace(
        "outputs_from: programs\n", ""
    )
    found = check_declaration.problems(schema, _repo(tmp_path, text))
    assert "inputs/players: a programs port must be named programs" in found


def test_a_programs_port_needs_outputs_from_programs(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    text = DECLARATION_PROGRAMS.replace("outputs_from: programs\n", "")
    assert check_declaration.problems(schema, _repo(tmp_path, text)) == [
        "outputs_from: a declaration with a programs port needs outputs_from: programs"
    ]


def test_links_without_a_programs_port_are_named(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    text = DECLARATION_V6.replace(
        "  manifest: {type: file, runs: false}\n",
        "  manifest: {type: file, runs: false}\n  links: {type: links}\n",
    )
    assert check_declaration.problems(schema, _repo(tmp_path, text)) == [
        "inputs/links: links with no programs port to link"
    ]


def test_the_command_line_names_the_schema_and_the_repo(
    schema: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schema_file = tmp_path / "primitive.schema.json"
    schema_file.write_text(json.dumps(schema), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo, DECLARATION)
    assert check_declaration.main(["", str(schema_file), str(repo)]) == 0
    (repo / "primitive.yaml").write_text(
        DECLARATION.replace("batch: false", "batch: maybe"), encoding="utf-8"
    )
    assert check_declaration.main(["", str(schema_file), str(repo)]) == 1
    assert "primitive.yaml: batch:" in capsys.readouterr().err
    assert check_declaration.main(["", str(schema_file)]) == 2
