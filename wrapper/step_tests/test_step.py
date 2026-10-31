"""The wrapper against a stand-in for the forge's door (stand_in.py), run in
this process with /work, /cache and the cache's cap moved under a
temporary folder, and a small Python program as the primitive. What needs
a real container, a process reading /proc/1/environ, a compile including
it and exec on isolate, is in test_image.py.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import unicon_step
from step_tests.stand_in import TOKEN, StandIn, stand_in

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-not-a-real-key-81c4"

Step = Callable[..., int]


@pytest.fixture
def server() -> Iterator[StandIn]:
    with stand_in() as state:
        yield state


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.setattr(unicon_step, "WORK", root)
    monkeypatch.setattr(unicon_step, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(unicon_step, "CACHE_CAP_FILE", tmp_path / "no-cap")
    monkeypatch.setattr(unicon_step, "undumpable", lambda: None)
    monkeypatch.setattr(unicon_step, "BACKOFF", 0.0)
    monkeypatch.setattr(unicon_step, "mounted", lambda path: True)
    monkeypatch.delenv("CI", raising=False)
    (tmp_path / "cache").mkdir()
    return root


@pytest.fixture
def step(
    server: StandIn, work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Step:
    """Run the wrapper once over the stand-in's job, with `source` as the
    primitive's program, and return its exit code."""
    monkeypatch.setenv("PLUGIN_DATA_URL", server.door)
    monkeypatch.setenv("PLUGIN_DATA_TOKEN", TOKEN)

    def run(source: str = WRITES_ACCEPTED, **job: Any) -> int:
        for path in work.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        server.ended = False
        server.job = {
            "schema_version": 1,
            "inputs": {"schema_version": 6, "inputs": {}},
            "files": [],
            "uploads": f"{server.door}/uploads",
            "done": f"{server.door}/done",
            "time_limit": 30,
            "fail_on_non_accepted": True,
        } | job
        program = tmp_path / "primitive.py"
        program.write_text(textwrap.dedent(source))
        return unicon_step.main(["unicon-step", sys.executable, str(program)])

    return run


ACCEPTED = '{"schema_version": 6, "outputs": {"outcome": "accepted"}}'
WRITES_ACCEPTED = f"open('outputs.json', 'w').write({ACCEPTED!r})"


def a_file(server: StandIn, name: str, content: bytes, **entry: Any) -> dict[str, Any]:
    """A download entry for `content`, served by the stand-in as `name`."""
    server.files[name] = content
    return {
        "path": f"in/{name}",
        "url": server.link(name),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    } | entry


def test_a_step_places_its_files_runs_the_primitive_and_reports(
    step: Step, server: StandIn, work: Path
) -> None:
    files = [a_file(server, "Xa9", b"3 4\n"), a_file(server, "Qm2", b"ok")]
    files[1]["url"] = server.link("Qm2", by_door=True)
    source = """
    import json, os
    inputs = json.load(open("inputs.json"))
    total = sum(map(int, open(inputs["inputs"]["numbers"]["file"]).read().split()))
    os.makedirs("out/1")
    open("out/1/sum", "w").write(str(total))
    json.dump({"schema_version": 6, "outputs": {"sum": {"file": "out/1/sum"},
               "outcome": "accepted"}}, open("outputs.json", "w"))
    """
    inputs = {"schema_version": 6, "inputs": {"numbers": {"file": "in/Xa9"}}}

    assert step(source, files=files, inputs=inputs) == 0

    assert server.put["out/1/sum"] == b"7"
    assert server.outputs()["outputs"]["outcome"] == "accepted"
    listed = {f["path"]: f for f in server.asked[0]["files"]}
    assert listed["out/1/sum"] == {
        "path": "out/1/sum",
        "sha256": hashlib.sha256(b"7").hexdigest(),
        "size": 1,
    }
    assert server.calls("POST", "/door/done") == 1
    assert (work / "in" / "Qm2").read_bytes() == b"ok"


def test_the_token_goes_only_to_the_door_and_never_in_a_link(
    step: Step, server: StandIn
) -> None:
    files = [a_file(server, "Xa9", b"data"), a_file(server, "Qm2", b"door")]
    files[1]["url"] = server.link("Qm2", by_door=True)
    source = WRITES_ACCEPTED + "\nimport os; os.makedirs('out'); open('out/x', 'w')"

    assert step(source, files=files) == 0

    for seen in server.seen:
        assert TOKEN not in seen.path
        door = seen.path.startswith("/door")
        assert seen.authorization == (f"Bearer {TOKEN}" if door else None), seen
    assert {s.path.split("?")[0] for s in server.seen if s.method == "PUT"} == {
        "/put/outputs.json",
        "/put/out/x",
    }


def test_a_file_whose_hash_is_not_the_jobs_refuses_the_step(
    step: Step, server: StandIn, work: Path
) -> None:
    entry = a_file(server, "Xa9", b"the right bytes")
    server.files["Xa9"] = b"the wrong bytes"
    ran = work.parent / "ran"

    code = step(f"open({str(ran)!r}, 'w')", files=[entry])

    assert code == 1
    assert not ran.exists()
    error = server.outputs()["error"]
    assert "is not the length and SHA-256 the job names" in error
    assert server.calls("POST", "/door/done") == 1
    assert not (work / "in" / "Xa9").exists()


def test_a_file_longer_than_the_job_says_is_refused(
    step: Step, server: StandIn
) -> None:
    entry = a_file(server, "Xa9", b"short")
    server.files["Xa9"] = b"short and then a great deal more"

    assert step(files=[entry]) == 1
    assert "length and SHA-256" in server.outputs()["error"]


def test_the_time_limit_kills_the_primitive_and_what_it_started(
    step: Step, server: StandIn, work: Path
) -> None:
    pid_file = work.parent / "child"
    source = f"""
    import subprocess, time
    child = subprocess.Popen(["sleep", "60"])
    open({str(pid_file)!r}, "w").write(str(child.pid))
    time.sleep(60)
    """
    started = time.monotonic()

    code = step(source, time_limit=1)

    assert code == 1
    assert time.monotonic() - started < 20
    assert "time limit of 1 s" in server.outputs()["error"]
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the primitive's own child outlived the time limit")


def test_the_link_the_token_and_secrets_are_gone_from_the_primitives_environment(
    step: Step, server: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUGIN_MODEL_KEY", SECRET)
    monkeypatch.setenv("PLUGIN_ANOTHER", "not named by the job")
    monkeypatch.setenv("KEPT", "yes")
    source = (
        """
    import json, os
    os.makedirs("out")
    json.dump(dict(os.environ), open("out/env.json", "w"))
    inputs = json.load(open("inputs.json"))
    open("out/key", "w").write(inputs["inputs"]["api_key"])
    """
        + WRITES_ACCEPTED
    )
    inputs = {"schema_version": 6, "inputs": {"api_key": {"secret": "model_key"}}}

    code = step(
        source,
        inputs=inputs,
        secrets=["model_key"],
        box_users={"first": 20480, "last": 20487},
        step_memory_mb=400,
    )

    assert code == 0
    seen = json.loads(server.put["out/env.json"])
    assert not [name for name in seen if name.startswith("PLUGIN_")]
    assert seen["KEPT"] == "yes"
    assert seen["UNICON_BOX_USERS"] == "20480-20487"
    assert seen["UNICON_STEP_MEMORY_MB"] == "400"
    assert TOKEN not in server.put["out/env.json"].decode()
    assert server.put["out/key"] == SECRET.encode()


def test_a_secret_the_step_was_not_given_is_an_error(
    step: Step, server: StandIn
) -> None:
    inputs = {"schema_version": 6, "inputs": {"api_key": {"secret": "model_key"}}}

    assert step(inputs=inputs, secrets=["model_key"]) == 1
    assert "model_key was not given" in server.outputs()["error"]


def test_a_secret_the_link_and_the_token_are_masked_in_the_log_and_the_outputs(
    step: Step,
    server: StandIn,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PLUGIN_MODEL_KEY", SECRET)
    said = f"key {SECRET}, link {server.door}, token {TOKEN}"
    source = f"""
    import json, sys
    print({said!r})
    print({said!r}, file=sys.stderr)
    numbers = '"score": 1.10, "big": 1e400, "n": 12345678901234567890'
    text = '{{"schema_version": 6, "outputs": {{"log": ' + json.dumps({said!r}) + (
        ', ' + numbers + ', "outcome": "accepted"}}}}')
    open("outputs.json", "w").write(text)
    """

    assert step(source, secrets=["model_key"]) == 0

    printed = capsys.readouterr().out
    assert printed.count("key ***, link ***, token ***") == 2
    for value in (SECRET, server.door, TOKEN):
        assert value not in printed
    uploaded = server.put["outputs.json"].decode()
    assert '"log":"key ***, link ***, token ***"' in uploaded
    assert '"score":1.10' in uploaded
    assert '"big":1e400' in uploaded
    assert '"n":12345678901234567890' in uploaded


def test_outputs_with_nothing_to_mask_are_uploaded_as_written(
    step: Step, server: StandIn
) -> None:
    text = '{ "schema_version": 6,\n  "outputs": {"outcome": "accepted", "x": 1.0} }'

    assert step(f"open('outputs.json', 'w').write({text!r})") == 0
    assert server.put["outputs.json"] == text.encode()


def test_a_failed_once_step_uploads_its_outputs_and_exits_non_zero(
    step: Step, server: StandIn
) -> None:
    source = """
    import json, os
    os.makedirs("out")
    open("out/program", "w")
    json.dump({"schema_version": 6, "outputs": {"log": "main.c:1: error",
               "outcome": "compile_error"}}, open("outputs.json", "w"))
    """

    assert step(source) == 1

    assert server.outputs()["outputs"]["outcome"] == "compile_error"
    assert "out/program" in server.put
    assert server.calls("POST", "/door/done") == 1


def test_a_step_done_per_run_exits_zero_whatever_its_outcomes(
    step: Step, server: StandIn
) -> None:
    source = """
    import json
    batch = [{"run": "r1", "outputs": {"outcome": "accepted"}},
             {"run": "r2", "outputs": {"outcome": "time_limit"}}]
    json.dump({"schema_version": 6, "batch": batch}, open("outputs.json", "w"))
    """

    assert step(source, fail_on_non_accepted=False) == 0
    assert step(source, fail_on_non_accepted=True) == 1


def test_a_primitive_that_writes_no_outputs_is_an_error_naming_how_it_ended(
    step: Step, server: StandIn
) -> None:
    assert step("raise SystemExit(3)") == 1
    assert server.outputs() == {
        "schema_version": 6,
        "error": "the primitive exited with code 3 and wrote no outputs.json",
    }

    assert step("import os, signal; os.kill(os.getpid(), signal.SIGKILL)") == 1
    assert server.outputs()["error"] == (
        "the primitive was killed by signal 9 and wrote no outputs.json"
    )


def test_the_inputs_reach_inputs_json_with_each_number_as_written(
    step: Step, server: StandIn
) -> None:
    whole = "9" * 5000
    server.raw = {
        "@fraction": "1.0000000000000000001",
        "@exponent": "1E+2",
        "@whole": whole,
    }
    inputs = {
        "schema_version": 6,
        "inputs": {"a": "@fraction", "b": "@exponent", "c": ["@whole", "\u00e9"]},
    }
    source = "import os, shutil\nos.makedirs('out')\n"
    source += "shutil.copy('inputs.json', 'out/inputs.json')\n" + WRITES_ACCEPTED

    assert step(source, inputs=inputs) == 0
    assert server.put["out/inputs.json"].decode() == (
        '{"schema_version":6,"inputs":{"a":1.0000000000000000001,"b":1E+2,'
        f'"c":[{whole},"\u00e9"]}}}}'
    )


def test_a_failed_step_writes_the_inputs_version_as_written(
    step: Step, server: StandIn
) -> None:
    server.raw = {"@version": "6.0"}

    assert step("raise SystemExit(1)", inputs={"schema_version": "@version"}) == 1
    assert server.put["outputs.json"].startswith(b'{"schema_version":6.0,')


def test_the_primitives_error_fails_the_step(step: Step, server: StandIn) -> None:
    source = """
    import json
    json.dump({"schema_version": 6, "error": "inputs.json has no batch list"},
              open("outputs.json", "w"))
    """

    assert step(source, fail_on_non_accepted=False) == 1
    assert server.outputs()["error"] == "inputs.json has no batch list"


def test_a_door_that_does_not_answer_at_first_is_asked_again(
    step: Step, server: StandIn
) -> None:
    server.failures = 2

    assert step() == 0
    assert server.calls("GET", "/door") == 3


def test_a_job_the_door_refuses_ends_the_step_without_a_report(
    step: Step, server: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUGIN_DATA_TOKEN", "another step's token")

    assert step() == 2
    assert server.put == {}


def test_a_step_with_nothing_left_to_do_exits_zero_having_done_nothing(
    step: Step, server: StandIn, work: Path
) -> None:
    server.job_code = 204

    assert step() == 0
    assert not (work / "outputs.json").exists()
    assert [(s.method, s.path) for s in server.seen] == [("GET", "/door")]


@pytest.mark.parametrize("code", [401, 410])
def test_a_job_refused_for_a_finished_grading_or_an_ended_token_fails_the_step(
    step: Step, server: StandIn, code: int
) -> None:
    server.job_code = code

    assert step() == 2
    assert [(s.method, s.path) for s in server.seen] == [("GET", "/door")]


def test_an_empty_folder_the_job_names_is_made(
    step: Step, server: StandIn, work: Path
) -> None:
    source = (
        "import json, os\n"
        "inputs = json.load(open('inputs.json'))\n"
        "assert os.listdir(inputs['inputs']['source']['folder']) == []\n"
    ) + WRITES_ACCEPTED
    inputs = {"schema_version": 6, "inputs": {"source": {"folder": "in/Ef1"}}}
    files = [{"path": "in/Ef1", "folder": True}]

    assert step(source, files=files, inputs=inputs) == 0
    assert (work / "in" / "Ef1").is_dir()
    for wrong in (
        {"path": "cache/a/b", "folder": True, "cached": True},
        {"path": "in/Ef1", "folder": True, "workspace": "files"},
        {"path": "in/..", "folder": True},
    ):
        assert step(files=[wrong]) == 1, wrong
        assert "places a file at" in server.outputs()["error"]


def test_each_upload_sends_the_checksum_its_link_is_signed_for(
    step: Step, server: StandIn
) -> None:
    writes = "\nimport os; os.makedirs('out'); open('out/x', 'w').write('x')"
    assert step(WRITES_ACCEPTED + writes) == 0

    puts = {s.path.split("?")[0]: s.checksum for s in server.seen if s.method == "PUT"}
    assert puts == {
        f"/put/{path}": base64.b64encode(hashlib.sha256(body).digest()).decode()
        for path, body in server.put.items()
    }
    assert set(puts) == {"/put/outputs.json", "/put/out/x"}


def test_a_job_of_another_version_is_refused(step: Step, server: StandIn) -> None:
    assert step(schema_version=2) == 2
    assert server.put == {}


def test_a_file_placed_outside_in_is_refused(step: Step, server: StandIn) -> None:
    for path in ("../etc/x", "in/../x", "/tmp/x", "out/x", "in", "cache/a/b"):
        entry = a_file(server, "Xa9", b"x") | {"path": path}
        assert step(files=[entry]) == 1, path
        assert "places a file at" in server.outputs()["error"]


def test_the_submission_is_copied_from_the_workspace_without_links(
    step: Step,
    server: StandIn,
    work: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "woodpecker" / "src"
    (workspace / "files" / "solution" / "lib").mkdir(parents=True)
    (workspace / "files" / "solution" / "main.c").write_text("int main;")
    (workspace / "files" / "solution" / "lib" / "a.h").write_text("#pragma once")
    monkeypatch.setenv("CI_WORKSPACE", str(workspace))
    entry = {"path": "in/S1", "workspace": "files/solution"}

    assert step(files=[entry]) == 0
    assert (work / "in" / "S1" / "lib" / "a.h").read_text() == "#pragma once"

    (workspace / "files" / "solution" / "lib" / "secret").symlink_to("/etc/hosts")
    assert step(files=[{"path": "in/S2", "workspace": "files/solution"}]) == 1
    assert "holds 'secret'" in server.outputs()["error"]
    assert step(files=[{"path": "in/S3", "workspace": "../../etc"}]) == 1


def test_report_mode_tells_the_door_the_pipeline_ended(
    server: StandIn, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUGIN_DATA_URL", f"{server.door}/ended")
    monkeypatch.setenv("PLUGIN_DATA_TOKEN", TOKEN)
    monkeypatch.setenv("PLUGIN_MODE", "report")

    assert unicon_step.main(["unicon-step"]) == 0
    assert [(s.method, s.path) for s in server.seen] == [("POST", "/door/ended")]


def test_report_mode_without_an_answer_exits_non_zero(
    server: StandIn, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUGIN_DATA_URL", f"{server.door}/nowhere")
    monkeypatch.setenv("PLUGIN_DATA_TOKEN", TOKEN)
    monkeypatch.setenv("PLUGIN_MODE", "report")

    assert unicon_step.main(["unicon-step"]) == 2


def test_without_a_data_link_it_runs_the_command_in_its_place(tmp_path: Path) -> None:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PLUGIN_")}
    done = subprocess.run(
        [sys.executable, unicon_step.__file__, "/bin/echo", "as", "before"],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )
    assert done.stdout == "as before\n"


def test_the_jobs_version_is_the_schemas() -> None:
    schema = json.loads((REPO_ROOT / "schemas" / "job.schema.json").read_text())
    assert schema["properties"]["schema_version"]["const"] == unicon_step.JOB_VERSION


# The cache.

CACHED_READ = (
    """
import json, os
inputs = json.load(open("inputs.json"))
path = inputs["inputs"]["data"]["file"]
os.makedirs("out")
info = {
    "content": open(path).read(),
    "real": os.path.realpath(path),
    "links": os.stat(path).st_nlink,
    "mode": oct(os.stat(os.path.dirname(os.path.realpath(path))).st_mode & 0o777),
}
json.dump(info, open("out/seen.json", "w"))
"""
    + WRITES_ACCEPTED
)


def cached(server: StandIn, name: str, content: bytes, path: str) -> dict[str, Any]:
    return a_file(server, name, content, cached=True) | {"path": path}


def test_a_cached_file_is_fetched_once_into_the_cache_and_linked_into_an_item(
    step: Step, server: StandIn, tmp_path: Path, work: Path
) -> None:
    cache = tmp_path / "cache"
    entry = cached(server, "train", b"1,2,3\n", "cache/Wq81/train.csv")
    inputs = {"schema_version": 6, "inputs": {"data": {"file": "cache/Wq81/train.csv"}}}

    assert step(CACHED_READ, files=[entry], inputs=inputs) == 0
    seen = json.loads(server.put["out/seen.json"])
    assert seen["content"] == "1,2,3\n"
    step_folder = Path(seen["real"]).parent.parent
    assert step_folder.parent == (cache / "items").resolve()
    assert step_folder.name.startswith("step-")
    assert seen["links"] == 2
    assert seen["mode"] == "0o555"
    digest = entry["sha256"]
    assert (cache / "objects" / digest).read_bytes() == b"1,2,3\n"
    assert list((cache / "items").iterdir()) == []

    assert step(CACHED_READ, files=[entry], inputs=inputs) == 0
    assert server.calls("GET", "/files/train") == 1


def test_the_step_folder_is_held_by_its_lock_for_the_steps_life(
    step: Step, server: StandIn, tmp_path: Path
) -> None:
    entry = cached(server, "train", b"x", "cache/Wq81/n")
    inputs = {"schema_version": 6, "inputs": {"data": {"file": "cache/Wq81/n"}}}
    source = f"""
    import fcntl, json, os, glob
    locks = glob.glob({str(tmp_path / "cache" / "items" / "*.lock")!r})
    held = []
    for lock in locks:
        with open(lock) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
    os.makedirs("out")
    json.dump(held, open("out/held.json", "w"))
    open("outputs.json", "w").write({ACCEPTED!r})
    """

    assert step(source, files=[entry], inputs=inputs) == 0
    assert json.loads(server.put["out/held.json"]) == [True]


def test_a_step_folder_whose_lock_no_one_holds_is_swept(
    step: Step, server: StandIn, tmp_path: Path
) -> None:
    items = tmp_path / "cache" / "items"
    items.mkdir()
    for name in ("step-gone", "step-live"):
        (items / name / "r1").mkdir(parents=True)
        (items / name / "r1" / "f").write_text("held")
        (items / name / "r1").chmod(0o555)
        (items / f"{name}.lock").touch()
    live = os.open(items / "step-live.lock", os.O_RDONLY)
    fcntl.flock(live, fcntl.LOCK_EX)
    entry = cached(server, "train", b"x", "cache/Wq81/n")
    try:
        assert step(files=[entry]) == 0
        assert sorted(p.name for p in items.iterdir()) == [
            "step-live",
            "step-live.lock",
        ]
    finally:
        os.close(live)


def test_a_name_is_fetched_once_while_another_step_holds_its_lock(
    server: StandIn, tmp_path: Path, work: Path
) -> None:
    cache = tmp_path / "cache"
    entry = cached(server, "train", b"fetched elsewhere", "cache/Wq81/n")
    store = unicon_step.Cache(cache)
    door = unicon_step.Door(server.door, TOKEN)
    held = unicon_step.take_lock(cache / "locks" / entry["sha256"])
    target = store.folder / "Wq81" / "n"
    placing = threading.Thread(target=store.place, args=(door, entry, target))
    placing.start()
    time.sleep(0.3)
    assert placing.is_alive()
    (cache / "objects" / entry["sha256"]).write_bytes(b"fetched elsewhere")
    os.close(held)
    placing.join(timeout=10)

    assert target.read_bytes() == b"fetched elsewhere"
    assert server.calls("GET", "/files/train") == 0


@pytest.mark.parametrize("found", [b"tampered", b"", None])
def test_a_cached_object_not_its_names_is_fetched_again_before_it_is_linked(
    server: StandIn, tmp_path: Path, work: Path, found: bytes | None
) -> None:
    cache = tmp_path / "cache"
    entry = cached(server, "train", b"the real answer", "cache/Wq81/n")
    store = unicon_step.Cache(cache)
    door = unicon_step.Door(server.door, TOKEN)
    object_ = cache / "objects" / entry["sha256"]
    if found is None:
        object_.symlink_to(tmp_path / "elsewhere")
        (tmp_path / "elsewhere").write_bytes(b"the real answer")
    else:
        object_.write_bytes(found)
        object_.chmod(0o444)
    target = store.folder / "Wq81" / "n"

    store.place(door, entry, target)

    assert target.read_bytes() == b"the real answer"
    assert not object_.is_symlink()
    assert object_.read_bytes() == b"the real answer"
    assert object_.samefile(target)
    assert server.calls("GET", "/files/train") == 1


def test_past_its_cap_the_cache_removes_the_least_recently_used_it_can(
    server: StandIn, tmp_path: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    unicon_step.tidy(cache)
    objects = cache / "objects"
    for age, name in enumerate(["oldest", "linked", "fetching", "older", "newest"]):
        (objects / name).write_bytes(b"x" * 100)
        os.utime(objects / name, (1000 + age, 1000 + age))
    os.link(objects / "linked", tmp_path / "held-by-an-item")
    fetching = unicon_step.take_lock(cache / "locks" / "fetching")
    monkeypatch.setattr(unicon_step, "CACHE_CAP", 550)
    try:
        unicon_step.make_room(cache, 200)
    finally:
        os.close(fetching)

    assert sorted(p.name for p in objects.iterdir()) == ["fetching", "linked", "newest"]


def test_a_machine_may_set_the_caches_cap(
    server: StandIn, tmp_path: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    unicon_step.tidy(cache)
    (cache / "objects" / "old").write_bytes(b"x" * 100)
    (tmp_path / "cap").write_text("150\n")
    monkeypatch.setattr(unicon_step, "CACHE_CAP_FILE", tmp_path / "cap")

    unicon_step.make_room(cache, 100)

    assert list((cache / "objects").iterdir()) == []


def test_stale_leftovers_of_killed_steps_are_swept_and_fresh_ones_kept(
    server: StandIn, tmp_path: Path, work: Path
) -> None:
    cache = tmp_path / "cache"
    items, locks, objects = cache / "items", cache / "locks", cache / "objects"
    for folder in (items, locks, objects):
        folder.mkdir()
    old = time.time() - unicon_step.STALE_SECONDS - 60
    (items / "step-old.lock-new").touch()
    (items / "step-new.lock-new").touch()
    for name in ("step-lockless", "step-young"):
        (items / name / ".fetching").mkdir(parents=True)
        (items / name / ".fetching" / "part").write_bytes(b"half a download")
    (objects / "kept").write_bytes(b"x")
    for name in ("kept", "orphan", "held"):
        (locks / name).touch()
    for path in (items / "step-old.lock-new", items / "step-lockless"):
        os.utime(path, (old, old))
    held = unicon_step.take_lock(locks / "held")
    try:
        unicon_step.Cache(cache)
    finally:
        os.close(held)

    left = sorted(p.name for p in items.iterdir())
    assert "step-old.lock-new" not in left
    assert "step-lockless" not in left
    assert {"step-new.lock-new", "step-young"} <= set(left)
    assert sorted(p.name for p in locks.iterdir()) == ["evict", "held", "kept"]


KEYS = {name: hashlib.sha256(name.encode()).hexdigest() for name in "abcdef"}


def a_program(cache: Path, name: str, age: int, size: int = 100) -> Path:
    """A compiled program's folder in the cache, as exec stores one, last
    used `age` seconds after the epoch's thousandth."""
    folder = cache / "programs" / KEYS[name]
    folder.mkdir(parents=True)
    (folder / "program").write_bytes(b"x" * (size - 2))
    (folder / "program").chmod(0o444)
    (folder / "result.json").write_bytes(b"{}")
    os.utime(folder, (1000 + age, 1000 + age))
    return folder


def test_past_its_cap_compiled_programs_go_by_last_use_with_the_objects(
    server: StandIn, tmp_path: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One cap and one order for both: the oldest goes first, whichever it
    is, and a program a step is compiling or reading stays."""
    cache = tmp_path / "cache"
    unicon_step.tidy(cache)
    for age, name in [(0, "oldest"), (3, "newer")]:
        (cache / "objects" / name).write_bytes(b"x" * 100)
        os.utime(cache / "objects" / name, (1000 + age, 1000 + age))
    a_program(cache, "a", age=1)
    a_program(cache, "b", age=2)
    a_program(cache, "c", age=4)
    held = unicon_step.take_lock(cache / "locks" / f"programs-{KEYS['b']}.lock")
    monkeypatch.setattr(unicon_step, "CACHE_CAP", 200)
    try:
        unicon_step.make_room(cache, 0)
    finally:
        os.close(held)

    assert list((cache / "objects").iterdir()) == []
    assert sorted(p.name for p in (cache / "programs").iterdir()) == sorted(
        [KEYS["b"], KEYS["c"]]
    )
    assert not (cache / "locks" / f"programs-{KEYS['a']}.lock").exists()


def test_a_folder_a_killed_step_was_compiling_into_is_swept(
    server: StandIn, tmp_path: Path, work: Path
) -> None:
    """`<key>.<anything>` is a compile in progress: swept once stale and its
    lock free, kept while young or held. A stored program stays, however
    old."""
    cache = tmp_path / "cache"
    programs = cache / "programs"
    programs.mkdir()
    for name in ("a", "b", "c"):
        (programs / f"{KEYS[name]}.tmp1").mkdir()
        (programs / f"{KEYS[name]}.tmp1" / "program").write_bytes(b"half")
    (programs / "stray").write_bytes(b"?")
    stored = a_program(cache, "d", age=0)
    old = time.time() - unicon_step.STALE_SECONDS - 60
    for path in (
        programs / f"{KEYS['a']}.tmp1",
        programs / f"{KEYS['b']}.tmp1",
        programs / "stray",
    ):
        os.utime(path, (old, old))
    for name in ("items", "locks"):
        (cache / name).mkdir()
    held = unicon_step.take_lock(cache / "locks" / f"programs-{KEYS['b']}.lock")
    try:
        unicon_step.sweep(cache)
    finally:
        os.close(held)

    assert sorted(p.name for p in programs.iterdir()) == sorted(
        [f"{KEYS['b']}.tmp1", f"{KEYS['c']}.tmp1", stored.name]
    )


@pytest.mark.parametrize(("uid", "tidied"), [(0, True), (1000, False)])
def test_a_step_given_the_cache_and_no_cached_files_tidies_it_as_root(
    step: Step,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    uid: int,
    tidied: bool,
) -> None:
    """exec stores what it compiles without the wrapper, so a step that
    fetches nothing into the cache still holds it to the cap; one not run
    as root cannot write it and leaves it as it is."""
    cache = tmp_path / "cache"
    a_program(cache, "a", age=0)
    a_program(cache, "b", age=1)
    monkeypatch.setattr(unicon_step, "CACHE_CAP", 150)
    monkeypatch.setattr(os, "geteuid", lambda: uid)

    assert step() == 0

    left = sorted(p.name for p in (cache / "programs").iterdir())
    assert left == ([KEYS["b"]] if tidied else sorted([KEYS["a"], KEYS["b"]]))


def test_cached_files_without_a_cache_volume_are_refused(
    step: Step, server: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(unicon_step, "mounted", lambda path: False)
    entry = cached(server, "train", b"x", "cache/Wq81/n")

    assert step(files=[entry]) == 1
    assert "no /cache volume" in server.outputs()["error"]


# Fetching, the door and the links.


@pytest.mark.parametrize("ways", [["chunked"], ["reset"], ["short"], ["reset"] * 3])
def test_a_download_cut_off_midway_is_fetched_again_from_its_start(
    step: Step, server: StandIn, work: Path, ways: list[str]
) -> None:
    content = b"0123456789" * 1000
    server.cuts["/files/Xa9"] = list(ways)

    assert step(files=[a_file(server, "Xa9", content)]) == 0
    assert (work / "in" / "Xa9").read_bytes() == content
    assert server.calls("GET", "/files/Xa9") == len(ways) + 1


def test_a_download_cut_off_every_time_names_the_file_never_its_link(
    step: Step, server: StandIn, work: Path
) -> None:
    server.cuts["/files/Xa9"] = ["chunked", "reset", "short", "chunked"]

    assert step(files=[a_file(server, "Xa9", b"0123456789" * 1000)]) == 1
    error = server.outputs()["error"]
    assert error.startswith("the file for in/Xa9: ")
    assert server.base not in error and "/files/" not in error
    assert not (work / "in" / "Xa9").exists()


def test_a_missing_file_names_the_file_never_its_link(
    step: Step, server: StandIn
) -> None:
    entry = a_file(server, "Xa9", b"x") | {"url": server.link("gone") + "?sig=s3cr3t"}

    assert step(files=[entry]) == 1
    error = server.outputs()["error"]
    assert error == "the file for in/Xa9: GET answered 404"


def test_a_job_cut_off_midway_is_asked_for_again(step: Step, server: StandIn) -> None:
    server.cuts["/door"] = ["chunked", "reset"]

    assert step() == 0
    assert server.calls("GET", "/door") == 3


def test_the_door_is_sent_json_and_the_store_bytes(step: Step, server: StandIn) -> None:
    assert (
        step(WRITES_ACCEPTED + "\nimport os; os.makedirs('out'); open('out/x', 'w')")
        == 0
    )

    kinds = {(s.method, s.path.split("?")[0], s.kind) for s in server.seen}
    assert kinds == {
        ("GET", "/door", None),
        ("POST", "/door/uploads", "application/json"),
        ("POST", "/door/done", "application/json"),
        ("PUT", "/put/outputs.json", "application/octet-stream"),
        ("PUT", "/put/out/x", "application/octet-stream"),
    }


def test_a_redirect_is_refused_and_never_followed(step: Step, server: StandIn) -> None:
    with stand_in() as elsewhere:
        server.moved["/files/Xa9"] = elsewhere.link("Xa9")
        elsewhere.files["Xa9"] = b"x"

        assert step(files=[a_file(server, "Xa9", b"x")]) == 1
        assert server.outputs()["error"] == "the file for in/Xa9: GET answered 302"

        server.moved = {"/door/done": f"{elsewhere.door}/done"}
        assert step() == 2

        assert elsewhere.seen == []


def test_no_proxy_is_used(
    step: Step, server: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    with stand_in() as proxy:
        for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
            monkeypatch.setenv(name, proxy.base)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)

        assert step() == 0
        assert proxy.seen == []


@pytest.mark.parametrize(
    ("url", "under"),
    [
        ("http://forge:8080/step/7", True),
        ("http://forge:8080/step/7/uploads", True),
        ("http://FORGE:8080/step/7/done", True),
        ("http://forge:8080/step/70", False),
        ("http://forge:8080/step/7/../8/done", False),
        ("http://forge:8080/step/7/./done", False),
        ("http://forge:8080/step/7/%2e%2e/8", False),
        ("http://forge:8080/step/7/%2E%2E%2F8", False),
        ("http://forge:8080/step/7/..%2f8", False),
        ("http://forge:8080/step/7/..%5c8", False),
        ("http://forge:8080/step/7\\..\\8", False),
        ("https://forge:8080/step/7/done", False),
        ("http://forge:8081/step/7/done", False),
        ("http://forge/step/7/done", False),
        ("http://forge.evil:8080/step/7/done", False),
        ("http://forge:8080@evil:8080/step/7/done", False),
        ("http://user@forge:8080/step/7/done", False),
        ("http://forge:99999/step/7", False),
    ],
)
def test_the_token_goes_only_under_the_door(url: str, under: bool) -> None:
    assert unicon_step.under("http://forge:8080/step/7", url) is under


def test_a_door_on_the_schemes_own_port_is_matched_with_or_without_it() -> None:
    assert unicon_step.under("https://forge/step/7", "https://forge:443/step/7/done")
    assert unicon_step.under("http://forge:80/step/7", "http://forge/step/7/done")


def test_in_a_woodpecker_step_without_a_data_link_it_refuses_to_run(
    tmp_path: Path,
) -> None:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PLUGIN_")}
    done = subprocess.run(
        [sys.executable, unicon_step.__file__, "/bin/echo", "ran"],
        capture_output=True,
        text=True,
        env=environment | {"CI": "woodpecker"},
    )
    assert done.returncode == 2
    assert "ran" not in done.stdout


# Secrets and masking.


@pytest.mark.parametrize(
    "name", ["data_token", "data_url", "mode", "Model_key", "model-key", "1key", ""]
)
def test_a_secret_name_that_is_not_a_secret_settings_is_refused(
    step: Step, server: StandIn, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("PLUGIN_MODEL-KEY", SECRET)
    inputs = {"schema_version": 6, "inputs": {"k": {"secret": name}}}

    assert step(inputs=inputs, secrets=[name]) == 1
    error = server.outputs()["error"]
    assert error == f"the job names the secret setting {name!r}"
    assert TOKEN not in json.dumps(server.put["outputs.json"].decode())


@pytest.mark.parametrize("value", ["", "short12"])
def test_a_secret_too_short_to_mask_is_refused(
    step: Step, server: StandIn, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PLUGIN_MODEL_KEY", value)

    assert step(secrets=["model_key"]) == 1
    assert "shorter than 8 characters" in server.outputs()["error"]


def test_a_secret_split_across_a_long_lines_reads_is_masked(
    step: Step,
    server: StandIn,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PLUGIN_MODEL_KEY", SECRET)
    source = (
        f"""
    import sys
    for before in (65536 - 5, 65536 * 2 - 9, 3):
        sys.stdout.write("é" * 2 + "a" * (before - 4) + {SECRET!r} + "b" * 70000)
        sys.stdout.write("\\n")
    sys.stdout.write("end " + {SECRET!r})
    sys.stdout.flush()
    """
        + WRITES_ACCEPTED
    )

    assert step(source, secrets=["model_key"]) == 0

    printed = capsys.readouterr().out
    assert SECRET not in printed
    assert printed.count("***") == 4
    lines = printed.split("\n")
    assert lines[0].startswith("éé") and lines[0].endswith("b" * 10)
    assert "end ***" in printed


def test_an_outcome_is_never_masked_and_a_number_holding_a_secret_is(
    step: Step, server: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUGIN_WORD", "accepted")
    monkeypatch.setenv("PLUGIN_PIN", "31415926535")
    text = (
        '{"schema_version": 6, "outputs": {"outcome": "accepted", '
        '"note": "accepted", "n": 31415926535, "m": 7}}'
    )

    assert (
        step(f"open('outputs.json', 'w').write({text!r})", secrets=["word", "pin"]) == 0
    )
    uploaded = server.put["outputs.json"].decode()
    assert '"outcome":"accepted"' in uploaded
    assert '"note":"***"' in uploaded
    assert '"n":"***"' in uploaded
    assert '"m":7' in uploaded


# The outcome and the outputs.


def test_a_missing_outcome_is_not_accepted(step: Step, server: StandIn) -> None:
    source = (
        "open('outputs.json', 'w').write('{\"schema_version\": 6, \"outputs\": {}}')"
    )

    assert step(source) == 1
    assert step(source, fail_on_non_accepted=False) == 0
    batch = (
        "import json; json.dump({'schema_version': 6, 'batch': [{'run': 'r1', "
        "'outputs': {'outcome': 'accepted'}}, {'run': 'r2'}]}, "
        "open('outputs.json', 'w'))"
    )
    assert step(batch) == 1


def test_outputs_written_as_a_link_are_refused_and_its_target_left_alone(
    step: Step, server: StandIn, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text(ACCEPTED)
    source = f"import os; os.symlink({str(target)!r}, 'outputs.json')"

    assert step(source) == 1
    assert (
        server.outputs()["error"] == "the primitive's outputs.json is not a plain file"
    )
    assert target.read_text() == ACCEPTED


def test_only_plain_files_under_out_are_uploaded(
    step: Step, server: StandIn, tmp_path: Path
) -> None:
    (tmp_path / "secret").write_text("not the primitive's")
    source = (
        WRITES_ACCEPTED
        + f"""
import os
os.makedirs("out/d")
open("out/d/plain", "w").write("p")
os.symlink({str(tmp_path / "secret")!r}, "out/link")
os.mkfifo("out/pipe")
"""
    )

    assert step(source) == 0
    assert set(server.put) == {"outputs.json", "out/d/plain"}


# Done, once, and what the wrapper refuses to start without.


@pytest.mark.parametrize("way", ["reset", "short", "chunked"])
def test_done_taken_though_its_answer_was_cut_is_done(
    step: Step, server: StandIn, way: str
) -> None:
    """The door took done, which ended the token, and the answer was cut:
    asked again, the door answers 401, which is done taken."""
    server.cuts["/door/done"] = [way]

    assert step() == 0
    assert server.calls("POST", "/door/done") == 2


def test_done_refused_at_the_first_ask_is_not_done(step: Step, server: StandIn) -> None:
    server.done_code = 401

    assert step() == 2
    assert server.calls("POST", "/door/done") == 1


def test_the_end_of_the_pipeline_taken_though_its_answer_was_cut_is_done(
    server: StandIn, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUGIN_DATA_URL", f"{server.door}/ended")
    monkeypatch.setenv("PLUGIN_DATA_TOKEN", TOKEN)
    monkeypatch.setenv("PLUGIN_MODE", "report")
    server.cuts["/door/ended"] = ["reset"]

    assert unicon_step.main(["unicon-step"]) == 0
    assert server.calls("POST", "/door/ended") == 2


def test_a_file_that_cannot_be_written_is_not_fetched_again(
    server: StandIn, tmp_path: Path
) -> None:
    folder = tmp_path / "in"
    folder.mkdir()
    folder.chmod(0o555)
    door = unicon_step.Door(server.door, TOKEN)
    entry = a_file(server, "Xa9", b"x")
    try:
        with pytest.raises(unicon_step.StepError) as raised:
            unicon_step.download(door, entry, folder / "Xa9")
    finally:
        folder.chmod(0o755)
    assert str(raised.value) == "could not write the file for in/Xa9: Permission denied"
    assert server.calls("GET", "/files/Xa9") == 0


def test_a_write_that_fails_midway_is_not_fetched_again(
    server: StandIn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    door = unicon_step.Door(server.door, TOKEN)
    entry = a_file(server, "Xa9", b"x" * 100)
    target = tmp_path / "Xa9"
    real = Path.open

    class Full:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __getattr__(self, name: str) -> Any:
            return getattr(self.stream, name)

        def __enter__(self) -> Full:
            return self

        def __exit__(self, *exc: object) -> None:
            self.stream.close()

        def write(self, data: bytes) -> int:
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "open", lambda self, *a, **k: Full(real(self, *a, **k)))
    with pytest.raises(unicon_step.StepError) as raised:
        unicon_step.download(door, entry, target)
    assert str(raised.value) == (
        "could not write the file for in/Xa9: No space left on device"
    )
    assert server.calls("GET", "/files/Xa9") == 1
    assert not target.exists()


def test_an_object_linked_while_its_lock_was_awaited_is_kept(
    server: StandIn, tmp_path: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """make_room listed the object unlinked; by the time it held the lock,
    another step had linked it into an item, so it stays."""
    cache = tmp_path / "cache"
    unicon_step.tidy(cache)
    (cache / "objects" / "taken").write_bytes(b"x" * 100)
    real = unicon_step.take_lock

    def linked_meanwhile(path: Path, wait: bool = True, create: bool = True) -> int:
        if path.name == "taken":
            os.link(cache / "objects" / "taken", tmp_path / "an-item-holds-it")
        return real(path, wait, create)

    monkeypatch.setattr(unicon_step, "take_lock", linked_meanwhile)
    monkeypatch.setattr(unicon_step, "CACHE_CAP", 50)
    unicon_step.make_room(cache, 10)

    assert (cache / "objects" / "taken").exists()


def test_in_a_woodpecker_step_it_must_be_the_first_process(
    step: Step, server: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under CI=woodpecker the wrapper must be pid 1, or what a primitive
    leaves running would outlive it unseen: it refuses before the job."""
    monkeypatch.setenv("CI", "woodpecker")

    assert step() == 2
    assert server.seen == []
