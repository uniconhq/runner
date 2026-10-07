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
