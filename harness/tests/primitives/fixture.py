"""A stand-in for the three classic primitives, small enough to read at a
glance: `compile` checks a Python source and hands it on as the binary, `run`
runs the binary on each test's input in a batch, and `check` compares an
output with the expected answer, once per test or as a batch. One program
plays all three, telling which it is from the names of its inputs: a compile
takes a `source`, a run a `binary` and a check an `actual` output. The unit
tests call `main` directly on a step directory; the Docker integration test
builds it into an image, whose entrypoint it is, and runs it as a real step.

A compile's source is a folder, as the classic plan gives a file to the
folder port: the program is the file its `entry` input names, or the folder's
one file. A run writes each test's output under out/<its index>/, since a
test id holds a `/`.

A few sources ask for something other than their answer, to exercise the
harness: `# fixture: bad-outputs` writes an outputs.json that breaks the
contract, `# fixture: no-outputs` writes none, `# fixture: error` reports an
error, `# fixture: sleep` never finishes compiling.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

VERSION = 5
KINDS = (
    ("source", "compile"),
    ("binary", "run"),
    ("actual", "check"),
)


def main(work: Path) -> int:
    document = json.loads((work / "inputs.json").read_text(encoding="utf-8"))
    kind = kind_of(document)
    if kind == "compile":
        result = _compile(work, document["inputs"])
    elif kind == "run":
        result = {
            "schema_version": VERSION,
            "batch": [
                {
                    "test": item["test"],
                    "outputs": _run(work, index, item["inputs"]),
                }
                for index, item in enumerate(document["batch"], start=1)
            ],
        }
    elif kind == "check":
        if "batch" in document:
            result = {
                "schema_version": VERSION,
                "batch": [
                    {"test": item["test"], "outputs": _check(work, item["inputs"])}
                    for item in document["batch"]
                ],
            }
        else:
            result = {
                "schema_version": VERSION,
                "outputs": _check(work, document["inputs"]),
            }
    else:
        result = {"schema_version": VERSION, "error": f"no such fixture {kind}"}
    if result is not None:
        (work / "outputs.json").write_text(json.dumps(result), encoding="utf-8")
    return 0


def kind_of(document: dict[str, Any]) -> str:
    """Which primitive this run stands in for, from the names of its inputs."""
    batch = document.get("batch")
    inputs = batch[0]["inputs"] if batch else document.get("inputs", {})
    return next((kind for name, kind in KINDS if name in inputs), "unknown")


def _compile(work: Path, inputs: dict[str, Any]) -> dict[str, Any] | None:
    source = _program(work, inputs)
    text = source.read_text(encoding="utf-8")
    if "# fixture: sleep" in text:
        time.sleep(3600)
    if "# fixture: no-outputs" in text:
        return None
    if "# fixture: error" in text:
        return {"schema_version": VERSION, "error": "the fixture was asked to fail"}
    if "# fixture: bad-outputs" in text:
        return {"schema_version": VERSION, "outputs": {"binary": {"file": "../escape"}}}
    try:
        compile(text, source.name, "exec")
    except SyntaxError as exc:
        return {
            "schema_version": VERSION,
            "outputs": {
                "compile_log": f"{source.name}:{exc.lineno}: {exc.msg}",
                "outcome": "compile_error",
            },
        }
    out = work / "out"
    out.mkdir(exist_ok=True)
    (out / "binary").write_text(text, encoding="utf-8")
    return {
        "schema_version": VERSION,
        "outputs": {
            "binary": {"file": "out/binary"},
            "compile_log": f"{source.name}: compiled for {inputs['language']}",
            "outcome": "accepted",
        },
    }


def _program(work: Path, inputs: dict[str, Any]) -> Path:
    """The file to compile: a file source itself, or in a folder the file the
    entry input names, or its one file.
    """
    source: dict[str, str] = inputs["source"]
    if "file" in source:
        return work / source["file"]
    folder = work / source["folder"]
    if "entry" in inputs:
        return folder / str(inputs["entry"])
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    return files[0]


def _run(work: Path, index: int, inputs: dict[str, Any]) -> dict[str, Any]:
    binary = work / inputs["binary"]["file"]
    given = (work / inputs["input"]["file"]).read_bytes()
    out = work / "out" / str(index)
    out.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    try:
        finished = subprocess.run(
            [sys.executable, str(binary)],
            input=given,
            capture_output=True,
            timeout=float(inputs["time_limit"]),
            check=False,
        )
    except subprocess.TimeoutExpired:
        outcome, printed = "time_limit", b""
    else:
        printed = finished.stdout
        if finished.returncode in (-9, 137):
            outcome = "memory_limit"
        elif finished.returncode != 0:
            outcome = "runtime_error"
        else:
            outcome = "accepted"
    (out / "output").write_bytes(printed)
    return {
        "output": {"file": f"out/{index}/output"},
        "time_ms": round((time.monotonic() - began) * 1000),
        "memory_kb": 1024,
        "outcome": outcome,
    }


def _check(work: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    actual = (work / inputs["actual"]["file"]).read_text(encoding="utf-8")
    expected = (work / inputs["expected"]["file"]).read_text(encoding="utf-8")

    def lines(text: str) -> list[str]:
        return [line.rstrip() for line in text.rstrip().splitlines()]

    same = lines(actual) == lines(expected)
    return {"outcome": "accepted" if same else "wrong_answer"}


if __name__ == "__main__":
    sys.exit(main(Path.cwd()))
