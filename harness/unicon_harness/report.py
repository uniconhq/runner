"""Talking back to the platform: the callbacks, all under the run's one bearer
token, and the log's presigned PUT. These, and the envelope, are the only
network calls the harness makes.

`started` and `progress` are for a person watching the grading; losing one is
not worth failing a run over, so they are tried briefly and let go. The final
callback carries the verdict and is retried until the envelope's deadline,
after which the token is dead anyway. A refusal the forge means (401, 403, 404,
409, 410: not this token, not this grading, or no longer running) is final.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from unicon_harness.runlog import RunLog

REQUEST_TIMEOUT_SECONDS = 15.0
LOG_TIMEOUT_SECONDS = 60.0
BRIEF_ATTEMPTS = 2
LOG_ATTEMPTS = 3
FINAL_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 15.0)
REFUSED = frozenset({401, 403, 404, 409, 410})


class RefusedError(Exception):
    """The forge answered that it will not take this run's reports."""

    def __init__(self, status: int) -> None:
        super().__init__(f"the callback answered {status}")
        self.status = status


def without_query(url: str) -> str:
    """A presigned URL's query is its credential."""
    try:
        return str(httpx.URL(url).copy_with(query=None, fragment=None))
    except httpx.InvalidURL:
        return "<invalid url>"


class Reporter:
    def __init__(
        self,
        envelope: dict[str, Any],
        log: RunLog,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._url = envelope["callback"]["url"]
        self._token = envelope["callback"]["token"]
        self._log_put = envelope["log_put"]
        self._deadline = datetime.fromisoformat(envelope["deadline"])
        self._log = log
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        self._sleep = sleep
        self._now = now

    def close(self) -> None:
        self._client.close()

    def seconds_left(self) -> float:
        return (self._deadline - self._now()).total_seconds()

    def started(self) -> None:
        """Raises RefusedError when the forge will not take this run."""
        self._brief({"event": "started"})

    def progress(self, step: str, done: int, total: int) -> None:
        try:
            self._brief(
                {"event": "progress", "step": step, "done": done, "total": total}
            )
        except RefusedError as refused:
            self._log.event(f"the progress callback was refused with {refused.status}")

    def finished(self, verdict: dict[str, Any]) -> bool:
        """Whether the forge accepted the verdict."""
        body = {"event": "finished", "verdict": verdict}
        waits = iter(FINAL_BACKOFF_SECONDS)
        while True:
            try:
                status = self._post(body)
            except httpx.HTTPError as exc:
                status = None
                self._log.event(
                    f"the final callback did not answer: {type(exc).__name__}"
                )
            if status is not None and status < 300:
                return True
            if status is not None and (
                status in REFUSED or (400 <= status < 500 and status != 429)
            ):
                self._log.event(f"the final callback was refused with {status}")
                return False
            wait = next(waits, FINAL_BACKOFF_SECONDS[-1])
            if self.seconds_left() <= wait:
                self._log.event("the deadline came before the verdict was delivered")
                return False
            self._sleep(wait)

    def log_url(self) -> str:
        """Where the log is put, without the presigned query."""
        return without_query(self._log_put)

    def put_log(self, data: bytes) -> bool:
        """Upload the log; whether it was stored."""
        for attempt in range(1, LOG_ATTEMPTS + 1):
            try:
                response = self._client.put(
                    self._log_put, content=data, timeout=LOG_TIMEOUT_SECONDS
                )
            except httpx.HTTPError as exc:
                reason = type(exc).__name__
            else:
                if response.status_code < 300:
                    return True
                reason = f"HTTP {response.status_code}"
            self._log.event(f"the log upload failed ({reason}), attempt {attempt}")
            if attempt < LOG_ATTEMPTS:
                self._sleep(float(attempt))
        return False

    def _brief(self, body: dict[str, Any]) -> None:
        for attempt in range(1, BRIEF_ATTEMPTS + 1):
            try:
                status = self._post(body)
            except httpx.HTTPError as exc:
                self._log.event(
                    f"the {body['event']} callback did not answer: {type(exc).__name__}"
                )
            else:
                if status < 300:
                    return
                if status in REFUSED:
                    raise RefusedError(status)
                self._log.event(f"the {body['event']} callback answered {status}")
            if attempt < BRIEF_ATTEMPTS:
                self._sleep(1.0)

    def _post(self, body: dict[str, Any]) -> int:
        response = self._client.post(
            self._url, json=body, headers={"Authorization": f"Bearer {self._token}"}
        )
        return response.status_code
