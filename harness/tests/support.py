"""What the harness tests stand in for the machine and the platform: a fake
Docker that runs the fixture primitives in-process on real step directories,
an HTTP server playing the forge's envelope, callback and log routes, and a
builder for the two checkouts.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from tests.primitives import fixture
from unicon_harness.docker import DockerError

REPO_ROOT = Path(__file__).parents[2]
EXAMPLES = REPO_ROOT / "examples"

SELF_ID = "c" * 64
VOLUME = "wp_01j9example_default"
DIGEST = "0" * 64
COMPILE = f"ghcr.io/uniconhq/primitive-compile@sha256:{'1' * 64}"
RUN = f"ghcr.io/uniconhq/primitive-sandbox-run@sha256:{'2' * 64}"
CHECK = f"ghcr.io/uniconhq/primitive-diff-check@sha256:{'3' * 64}"
JUDGE = f"ghcr.io/acme/rubric@sha256:{'4' * 64}"
HARNESS = f"ghcr.io/uniconhq/harness@sha256:{'5' * 64}"


def mountinfo() -> str:
    """Two lines of /proc/self/mountinfo as Docker leaves them in a container."""
    return (
        "812 790 0:52 / / rw,relatime - overlay overlay rw\n"
        f"820 790 8:48 /var/lib/docker/containers/{SELF_ID}/hostname /etc/hostname "
        "rw,relatime - ext4 /dev/sde rw\n"
    )


def example(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    return document


@dataclass
class Does:
    """What a fake container does instead of running the fixture: `hangs` never
    exits until killed, `out_of_memory` is Docker's flag afterwards.
    """

    exit_code: int = 0
    hangs: bool = False
    out_of_memory: bool = False
    printed: str = ""
    before: Callable[[Path], None] | None = None
    runs_fixture: bool = True


@dataclass
class FakeContainer:
    body: dict[str, Any]
    work: Path
    does: Does
    exit_code: int | None = None
    killed: bool = False
    removed: bool = False


class FakeDocker:
    """The seven calls, over a real directory standing in for the run's volume.
    A container "runs" at start: the fixture is called on its working
    directory, and tells from the inputs which primitive it stands in for.
    """

    def __init__(self, volume_root: Path, volume: str = VOLUME) -> None:
        self.volume_root = volume_root
        self.volume = volume
        self.containers: dict[str, FakeContainer] = {}
        self.order: list[str] = []
        self.calls: list[str] = []
        self.behaviour: dict[str, Does] = {}
        self.refuse_create: str | None = None
        self.own_mounts: list[dict[str, Any]] | None = None
        self.on_create: Callable[[str, FakeContainer], None] | None = None

    def does(self, step: str, does: Does) -> None:
        """What the container for step `step` does, instead of the fixture."""
        self.behaviour[step] = does

    def bodies(self) -> list[dict[str, Any]]:
        return [self.containers[c].body for c in self.order]

    def inspect(self, container_id: str) -> dict[str, Any]:
        self.calls.append(f"inspect {container_id[:12]}")
        if container_id == SELF_ID:
            return {
                "Id": SELF_ID,
                "Mounts": self.own_mounts
                if self.own_mounts is not None
                else [
                    {
                        "Type": "volume",
                        "Name": self.volume,
                        "Destination": str(self.volume_root),
                    },
                    {
                        "Type": "volume",
                        "Name": "unicon-filter",
                        "Destination": "/run/unicon",
                    },
                ],
            }
        container = self._get(container_id)
        return {
            "Id": container_id,
            "State": {
                "Running": container.exit_code is None,
                "ExitCode": container.exit_code or 0,
                "OOMKilled": container.does.out_of_memory,
            },
        }

    def create(self, body: Mapping[str, Any]) -> str:
        self.calls.append("create")
        if self.refuse_create is not None:
            raise DockerError(403, self.refuse_create)
        copied: dict[str, Any] = copy.deepcopy(dict(body))
        mount = copied["HostConfig"]["Mounts"][0]
        assert mount["Source"] == self.volume
        work = self.volume_root / mount["VolumeOptions"]["Subpath"]
        container_id = f"{len(self.order) + 1:064x}"
        step = copied["Labels"]["unicon.step"]
        does = self.behaviour.get(step, Does())
        container = FakeContainer(copied, work, does)
        self.containers[container_id] = container
        self.order.append(container_id)
        if self.on_create is not None:
            self.on_create(step, container)
        return container_id

    def start(self, container_id: str) -> None:
        self.calls.append(f"start {container_id[-3:]}")
        container = self._get(container_id)
        if container.does.before is not None:
            container.does.before(container.work)
        if container.does.hangs:
            return
        if container.does.runs_fixture:
            fixture.main(container.work)
        container.exit_code = container.does.exit_code

    def wait(self, container_id: str, timeout: float) -> int | None:
        self.calls.append(f"wait {container_id[-3:]}")
        return self._get(container_id).exit_code

    def kill(self, container_id: str) -> None:
        self.calls.append(f"kill {container_id[-3:]}")
        container = self._get(container_id)
        container.killed = True
        container.exit_code = 137

    def logs(self, container_id: str, limit: int) -> str:
        return self._get(container_id).does.printed[:limit]

    def remove(self, container_id: str) -> None:
        self.calls.append(f"remove {container_id[-3:]}")
        self._get(container_id).removed = True

    def _get(self, container_id: str) -> FakeContainer:
        if container_id not in self.containers:
            raise DockerError(404, f"No such container: {container_id}")
        return self.containers[container_id]


def limits(time_ms: int = 10_000, memory_mb: int = 256) -> dict[str, int]:
    return {
        "time_ms": time_ms,
        "cpu_ms": time_ms,
        "memory_mb": memory_mb,
        "pids": 64,
        "output_mb": 8,
        "gpus": 0,
    }


def classic_plan(
    tests: list[str],
    check_batch: bool = False,
    image: str | None = None,
    run_memory_mb: int = 256,
) -> dict[str, Any]:
    """The classic workflow compiled over `tests`: compile once, its source a
    file given to a folder port, run as a batch, check once per test (or as a
    batch), reporting each test's time and memory and the compile log. `image`
    puts every step on one image, the fixture's, for a real Docker.
    """
    compile_step = {
        "id": "compile",
        "primitive": "unicon/compile@v2",
        "image": image or COMPILE,
        "network": False,
        "limits": limits(),
        "outputs": {"binary": "file", "compile_log": "text", "outcome": "outcome"},
        "folders": ["source"],
        "inputs": {
            "source": {"submission": "submission"},
            "language": {"submission": "language"},
        },
    }
    run_step = {
        "id": "run",
        "primitive": "unicon/sandbox-run@v2",
        "image": image or RUN,
        "network": False,
        "limits": limits(time_ms=30_000, memory_mb=run_memory_mb),
        "outputs": {
            "output": "file",
            "time_ms": "number",
            "memory_kb": "number",
            "outcome": "outcome",
        },
        "batch": [
            {
                "test": test,
                "inputs": {
                    "binary": {"step": "compile", "output": "binary"},
                    "input": {"task": f"tests/{test}/input"},
                    "time_limit": {"value": 2},
                    "memory_limit": {"value": 64},
                },
            }
            for test in tests
        ],
    }
    check_base: dict[str, Any] = {
        "id": "check",
        "primitive": "unicon/diff-check@v2",
        "image": image or CHECK,
        "network": False,
        "limits": limits(),
        "outputs": {"outcome": "outcome"},
    }

    def check_inputs(test: str) -> dict[str, Any]:
        return {
            "actual": {"step": "run", "output": "output"},
            "expected": {"task": f"tests/{test}/answer"},
        }

    if check_batch:
        checks = [
            check_base
            | {"batch": [{"test": t, "inputs": check_inputs(t)} for t in tests]}
        ]
    else:
        checks = [check_base | {"test": t, "inputs": check_inputs(t)} for t in tests]
    return {
        "schema_version": 5,
        "harness_image": HARNESS,
        "tests": tests,
        "contestant": {
            "submission": {"type": "file"},
            "language": {"type": "enum", "options": ["c", "python"]},
        },
        "steps": [compile_step, run_step, *checks],
        "report": {
            "time_ms": {"step": "run", "output": "time_ms", "at_least": 0},
            "memory_kb": {"step": "run", "output": "memory_kb", "at_least": 0},
            "log": {"step": "compile", "output": "compile_log"},
        },
    }


SUM = "import sys\nprint(sum(int(x) for x in sys.stdin.read().split()))\n"


@dataclass
class Checkouts:
    """The run's volume as the CI leaves it: the task and submission checkouts."""

    root: Path
    tests: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def task(self) -> Path:
        return self.root / "task"

    @property
    def submission(self) -> Path:
        return self.root / "submission"

    def write_plan(self, plan: dict[str, Any]) -> None:
        path = self.task / "plans" / "plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    def write_source(self, source: str, language: str = "python") -> None:
        self.write_submission(
            {
                "submission": {"files": ["files/submission/main.py"]},
                "language": {"value": language},
            },
            {"files/submission/main.py": source},
        )

    def write_submission(
        self, inputs: dict[str, Any], files: Mapping[str, str] | None = None
    ) -> None:
        """submission.json with these entries, and these files under the
        checkout by their paths.
        """
        for relative, text in (files or {}).items():
            path = self.submission / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        document = {"schema_version": 5, "inputs": inputs}
        self.submission.mkdir(parents=True, exist_ok=True)
        (self.submission / "submission.json").write_text(
            json.dumps(document), encoding="utf-8"
        )


def checkouts(root: Path, tests: Mapping[str, tuple[str, str]]) -> Checkouts:
    """A task with the given tests (input, answer) by id, at
    tests/<group>/<test>/, and the classic plan, and a submission of the sum
    program.
    """
    made = Checkouts(root, dict(tests))
    for test, (given, answer) in tests.items():
        folder = made.task / "tests" / test
        folder.mkdir(parents=True)
        (folder / "input").write_text(given, encoding="utf-8")
        (folder / "answer").write_text(answer, encoding="utf-8")
    made.write_plan(classic_plan(list(tests)))
    made.write_source(SUM)
    return made


class Platform:
    """The forge's machine routes: GET /envelope, POST /callback, PUT /log.
    `callback_statuses` answers the started and progress callbacks in turn and
    `finished_statuses` the final one, the last of each repeating.
    """

    def __init__(self, url: str, state: dict[str, Any]) -> None:
        self.url = url
        self._state = state

    @property
    def envelope_url(self) -> str:
        return f"{self.url}/envelope?key=envelope-secret"

    def serve(self, body: bytes, status: int = 200, delay: float = 0.0) -> None:
        self._state.update(body=body, status=status, delay=delay)

    def serve_envelope(self, envelope: dict[str, Any]) -> None:
        self.serve(json.dumps(envelope).encode("utf-8"))

    def answer_callbacks(self, *statuses: int) -> None:
        self._state["callback_statuses"] = list(statuses)

    def answer_finished(self, *statuses: int) -> None:
        """How the final callback is answered, in turn, the last one repeating."""
        self._state["finished_statuses"] = list(statuses)

    def answer_log(self, status: int) -> None:
        self._state["log_status"] = status

    @property
    def callbacks(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = self._state["callbacks"]
        return found

    @property
    def authorizations(self) -> list[str]:
        found: list[str] = self._state["authorizations"]
        return found

    @property
    def logs(self) -> list[bytes]:
        found: list[bytes] = self._state["logs"]
        return found

    def events(self) -> list[str]:
        return [c["event"] for c in self.callbacks]

    def result(self) -> dict[str, Any]:
        """The result of the last final callback, its numbers as Decimals and
        ints as the forge reads them.
        """
        finished = [c for c in self.callbacks if c["event"] == "finished"]
        assert finished, self.events()
        found: dict[str, Any] = finished[-1]["result"]
        return found

    @property
    def bodies(self) -> list[bytes]:
        """Every callback body as it was sent."""
        found: list[bytes] = self._state["bodies"]
        return found

    def envelope_for(self, made: Checkouts, wall_seconds: int = 900) -> dict[str, Any]:
        envelope = example("envelope.json")
        envelope["checkouts"] = {
            "task": str(made.task),
            "submission": str(made.submission),
        }
        envelope["callback"]["url"] = f"{self.url}/callback"
        envelope["log_put"] = f"{self.url}/log?X-Amz-Signature=log-secret"
        envelope["deadline"] = "2999-01-01T00:00:00Z"
        envelope["limits"] = {"wall_seconds": wall_seconds}
        return envelope


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client that gave up mid-response is what some tests are for. The
        default prints a traceback from a thread pytest is capturing, landing
        in the middle of an unrelated assertion.
        """


@contextmanager
def platform_server() -> Iterator[Platform]:
    state: dict[str, Any] = {
        "body": b"{}",
        "status": 200,
        "delay": 0.0,
        "callbacks": [],
        "bodies": [],
        "authorizations": [],
        "logs": [],
        "callback_statuses": [200],
        "finished_statuses": [200],
        "log_status": 200,
    }
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            time.sleep(state["delay"])
            self._answer(state["status"], state["body"])

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            document = json.loads(body, parse_float=Decimal)
            with lock:
                state["callbacks"].append(document)
                state["bodies"].append(body)
                state["authorizations"].append(self.headers.get("Authorization", ""))
                final = document.get("event") == "finished"
                statuses = state["finished_statuses" if final else "callback_statuses"]
                status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            self._answer(status, b"{}")

        def do_PUT(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            with lock:
                state["logs"].append(body)
            self._answer(state["log_status"], b"")

        def _answer(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            """Silence the default stderr access log; it only noises up pytest."""

    server = _QuietServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = int(server.server_address[1])
    try:
        yield Platform(f"http://127.0.0.1:{port}", state)
    finally:
        server.shutdown()
        server.server_close()
