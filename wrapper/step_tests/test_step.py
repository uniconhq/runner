"""The wrapper against a stand-in for the forge's door (stand_in.py), run in
this process with /work, /cache and the cache's cap moved under a
temporary folder, and a small Python program as the primitive. What needs
a real container, a process reading /proc/1/environ, a compile including
it and exec on isolate, is in test_image.py.
"""

from __future__ import annotations

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
    )

    assert code == 0
    seen = json.loads(server.put["out/env.json"])
    assert not [name for name in seen if name.startswith("PLUGIN_")]
    assert seen["KEPT"] == "yes"
    assert seen["UNICON_BOX_USERS"] == "20480-20487"
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


def test_a_primitive_that_writes_no_outputs_is_an_error(
    step: Step, server: StandIn
) -> None:
    assert step("raise SystemExit(3)") == 1
    assert server.outputs() == {
        "schema_version": 6,
        "error": "the primitive ended and wrote no outputs.json",
    }


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
    json.dump({{"schema_version": 6, "outputs": {{}}}}, open("outputs.json", "w"))
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


def test_past_its_cap_the_cache_removes_the_least_recently_used_it_can(
    server: StandIn, tmp_path: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    store = unicon_step.Cache(cache)
    objects = cache / "objects"
    for age, name in enumerate(["oldest", "linked", "fetching", "older", "newest"]):
        (objects / name).write_bytes(b"x" * 100)
        os.utime(objects / name, (1000 + age, 1000 + age))
    os.link(objects / "linked", tmp_path / "held-by-an-item")
    fetching = unicon_step.take_lock(cache / "locks" / "fetching")
    monkeypatch.setattr(unicon_step, "CACHE_CAP", 550)
    try:
        store.make_room(200)
    finally:
        os.close(fetching)

    assert sorted(p.name for p in objects.iterdir()) == ["fetching", "linked", "newest"]


def test_a_machine_may_set_the_caches_cap(
    server: StandIn, tmp_path: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    store = unicon_step.Cache(cache)
    (cache / "objects" / "old").write_bytes(b"x" * 100)
    (tmp_path / "cap").write_text("150\n")
    monkeypatch.setattr(unicon_step, "CACHE_CAP_FILE", tmp_path / "cap")

    store.make_room(100)

    assert list((cache / "objects").iterdir()) == []
