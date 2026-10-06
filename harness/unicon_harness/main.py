"""The harness image entrypoint. The CI starts the image once per grading run
with a URL to the envelope and the grading id, and the socket filter's socket
as DOCKER_HOST.

1. Fetch the envelope and refuse it unless its version, shape and grading id
   are right. A refusal exits 2 and reports nothing: an envelope the harness
   does not trust gives it no callback it can trust either.
2. Post started, then grade: find the run's workspace, give up root, read the
   plan and task files from the task checkout and the contestant's files from
   the submission checkout, and run the plan one sandboxed container per step.
3. Put the log by its presigned URL, and post the result, checked against
   result.schema.json, in the final callback. Anything unexpected on the way
   stops the run as a system_error, never a grade.

Every secret the envelope carries is hidden from both logs and masked in
every text the result reports.

Exit 0 once the forge accepted the result, 1 when it could not be delivered,
2 when the run never began.
"""

from __future__ import annotations

import os
import signal
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

import httpx

from unicon_harness import plan as plans
from unicon_harness import result as results
from unicon_harness import submission as submissions
from unicon_harness import workspace as workspaces
from unicon_harness.contracts import SCHEMA_VERSION, SchemasMissingError
from unicon_harness.docker import Docker, DockerClient
from unicon_harness.envelope import EnvelopeError, load
from unicon_harness.faults import GradingError
from unicon_harness.grading import Grader, Grading
from unicon_harness.report import RefusedError, Reporter, without_query
from unicon_harness.runlog import RunLog
from unicon_harness.sandbox import Sandbox

ENVELOPE_URL_VARIABLE = "UNICON_ENVELOPE_URL"
GRADING_ID_VARIABLE = "UNICON_GRADING_ID"

FETCH_IO_TIMEOUT_SECONDS = 30.0
# The platform records a run as started only after the CI has accepted it, and
# a fast machine can ask for the envelope in between, which the platform
# answers 410 until the record lands, a moment later. So a 410 is asked again
# a few times before the harness gives up; any other refusal is final at once.
ENVELOPE_TRIES = 5
ENVELOPE_PAUSE_SECONDS = 3.0
REPORT_MARGIN_SECONDS = 30.0
"""Kept back from the deadline for putting the log and posting the result."""

EXIT_OK = 0
EXIT_NOT_DELIVERED = 1
EXIT_CANNOT_START = 2


class Terminated(BaseException):
    """The CI stopped the step. A BaseException, so no handler meant for a
    failed step catches it on the way to the cleanup.
    """


DockerFactory = Callable[[], Docker]


def main(
    docker_factory: DockerFactory | None = None,
    client: httpx.Client | None = None,
    mountinfo: Callable[[], str] = workspaces.read_mountinfo,
) -> int:
    envelope_url = os.environ.get(ENVELOPE_URL_VARIABLE, "")
    if not envelope_url:
        return _refuse("missing_environment", f"{ENVELOPE_URL_VARIABLE} is not set")

    grading_id = os.environ.get(GRADING_ID_VARIABLE, "")
    if not grading_id:
        return _refuse("missing_environment", f"{GRADING_ID_VARIABLE} is not set")

    try:
        raw = _fetch(envelope_url, client)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        return _refuse(
            "envelope_unreachable",
            f"the envelope at {without_query(envelope_url)} answered {status}",
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return _refuse(
            "envelope_unreachable",
            f"could not fetch the envelope from {without_query(envelope_url)}: "
            f"{type(exc).__name__}",
        )

    try:
        envelope = load(raw)
    except EnvelopeError as exc:
        return _refuse(exc.code, exc.message)
    except SchemasMissingError as exc:
        return _refuse("schemas_missing", str(exc))

    if not _same_grading(grading_id, envelope["grading_id"]):
        return _refuse(
            "grading_id_mismatch",
            f"{GRADING_ID_VARIABLE} is {grading_id} but the envelope is for "
            f"{envelope['grading_id']}",
        )

    log = RunLog()
    log.hide(envelope["callback"]["token"])
    for url in (envelope_url, envelope["log_put"]):
        query = httpx.URL(url).query.decode()
        if query:
            log.hide(query)
    log.event(_summary(envelope))
    previous = signal.signal(signal.SIGTERM, _terminate)
    reporter = Reporter(envelope, log, client=client)
    try:
        return grade_and_report(
            envelope,
            log,
            reporter,
            docker_factory or DockerClient.from_environment,
            mountinfo,
        )
    except Terminated:
        log.event("the CI stopped the run; nothing is reported")
        return EXIT_NOT_DELIVERED
    finally:
        signal.signal(signal.SIGTERM, previous)
        if client is None:
            reporter.close()


def grade_and_report(
    envelope: dict[str, Any],
    log: RunLog,
    reporter: Reporter,
    docker_factory: DockerFactory,
    mountinfo: Callable[[], str],
) -> int:
    """Everything after the envelope is accepted: started, the grading, the log
    and the final callback. Every secret's value is hidden from both logs from
    the start.
    """
    for secret in envelope["secrets"].values():
        log.hide(secret)
    began = time.monotonic()
    log.tell(f"Grading {envelope['submission']['tag']}, attempt {envelope['attempt']}.")
    left = reporter.seconds_left()
    if left <= 0:
        log.event("the envelope's deadline has already passed; nothing is reported")
        return EXIT_NOT_DELIVERED
    budget = min(
        float(envelope["limits"]["wall_seconds"]), left - REPORT_MARGIN_SECONDS
    )
    try:
        reporter.started()
    except RefusedError as refused:
        log.event(f"the forge refused the run with {refused.status}; it will not grade")
        return EXIT_NOT_DELIVERED

    result = _grade(envelope, log, reporter, docker_factory, mountinfo, began, budget)
    result["run_log"] = reporter.log_url()
    found = results.check(result)
    if found is not None:
        message = (
            f"the result the harness built does not match result.schema.json {found}"
        )
        log.event(f"system error: {message}")
        result = results.system_error(message, envelope["secrets"].values())
        result["run_log"] = reporter.log_url()
    _tell_result(log, result)
    if not reporter.put_log(log.to_bytes()):
        result["run_log"] = None
    delivered = reporter.finished(result)
    print(
        f"unicon-harness: result {_said(result)} "
        f"{'delivered' if delivered else 'not delivered'}"
    )
    return EXIT_OK if delivered else EXIT_NOT_DELIVERED


def _grade(
    envelope: dict[str, Any],
    log: RunLog,
    reporter: Reporter,
    docker_factory: DockerFactory,
    mountinfo: Callable[[], str],
    began: float,
    budget: float,
) -> dict[str, Any]:
    secrets: dict[str, str] = envelope["secrets"]
    sandbox: Sandbox | None = None
    docker: Docker | None = None
    plan: plans.Plan | None = None
    grader: Grader | None = None
    try:
        if budget <= 0:
            raise GradingError("the run has no time left before its deadline")
        docker = docker_factory()
        task = Path(envelope["checkouts"]["task"])
        volume, mount = workspaces.locate(docker, task, mountinfo())
        workspace = workspaces.prepare(volume, mount)
        log.event(
            f"steps run as {workspace.user} in volume {volume} under {workspace.steps}"
        )
        plan = plans.load(task)
        given = submissions.load(Path(envelope["checkouts"]["submission"]), plan, log)
        log.event(
            f"plan: {len(plan.steps)} steps over {len(plan.tests)} tests, "
            f"harness {plan.harness_image}"
        )
        log.tell(f"{len(plan.steps)} steps to run over {len(plan.tests)} tests.")
        sandbox = Sandbox(docker, workspace, envelope["grading_id"], log)
        grader = Grader(
            plan,
            task,
            given,
            secrets,
            workspace,
            sandbox,
            log,
            began + budget,
            reporter.progress,
        )
        result = results.graded(plan, grader.run(), secrets.values())
        log.event(f"graded: {_said(result)}")
        return result
    except GradingError as fault:
        log.event(f"system error: {fault.message}")
        return _failed(fault.message, plan, grader, secrets)
    except Exception as exc:
        log.block("the harness failed", traceback.format_exc())
        message = f"the harness failed: {type(exc).__name__}: {exc}"
        return _failed(message, plan, grader, secrets)
    finally:
        if sandbox is not None:
            sandbox.clean_up()
        if isinstance(docker, DockerClient):
            docker.close()


def _failed(
    message: str,
    plan: plans.Plan | None,
    grader: Grader | None,
    secrets: dict[str, str],
) -> dict[str, Any]:
    """The result of a run a fault ended: the rows as far as it got, or none
    when the plan was never read.
    """
    if plan is None:
        return results.system_error(message, secrets.values())
    grading = grader.grading if grader is not None else Grading()
    return results.graded(plan, grading, secrets.values(), error=message)


def _tell_result(log: RunLog, result: dict[str, Any]) -> None:
    """The last line of the run log. A system error says only that there was
    one: its reason is for staff and is in the CI log.
    """
    if result["stopped"] == results.SYSTEM_ERROR:
        log.tell(
            "The run could not be graded because of a system error. The "
            "platform's staff can see what went wrong."
        )
        return
    said = _said(result)
    log.tell(f"{said[0].upper()}{said[1:]}.")


def _said(result: dict[str, Any]) -> str:
    """The result in a few words: what stopped it, or how many tests passed."""
    if result["stopped"] is not None:
        return f"stopped at {result['stopped']}"
    accepted = sum(1 for row in result["tests"] if row["outcome"] == "accepted")
    return f"{accepted} of {len(result['tests'])} tests accepted"


def _terminate(signum: int, frame: FrameType | None) -> None:
    raise Terminated


def _fetch(url: str, client: httpx.Client | None) -> bytes:
    timeout = httpx.Timeout(FETCH_IO_TIMEOUT_SECONDS)
    for tried in range(1, ENVELOPE_TRIES + 1):
        if client is None:
            response = httpx.get(url, timeout=timeout, follow_redirects=True)
        else:
            response = client.get(url, timeout=timeout, follow_redirects=True)
        if response.status_code != 410 or tried == ENVELOPE_TRIES:
            break
        _pause(ENVELOPE_PAUSE_SECONDS)
    response.raise_for_status()
    return response.content


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _same_grading(from_environment: str, from_envelope: str) -> bool:
    """Compare the two ids as uuids, so case and dash style agree. An unparseable
    value is a mismatch rather than a traceback: whoever started the image got
    the id wrong either way.
    """
    try:
        return uuid.UUID(from_environment) == uuid.UUID(from_envelope)
    except ValueError:
        return False


def _summary(envelope: dict[str, Any]) -> str:
    submission = envelope["submission"]
    return (
        f"envelope accepted: grading={envelope['grading_id']} "
        f"submission={submission['org']}/{submission['repo']}@{submission['tag']} "
        f"attempt={envelope['attempt']} "
        f"schema_version={SCHEMA_VERSION}"
    )


def _refuse(code: str, message: str) -> int:
    print(f"unicon-harness: {code}: {message}", file=sys.stderr)
    return EXIT_CANNOT_START


def run() -> None:
    """Console script entrypoint."""
    sys.exit(main())
