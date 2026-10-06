"""The run's two logs: the run log the task's organisers read, and the one for
staff.

The run log is the one thing the harness writes to object storage, uploaded
once at the end by the presigned PUT, and the forge shows it to the task's
organisers; it names every test, so no contestant reads it. It says what ran
and how it went: each step by its id and primitive, its outcome, test by
test, and the result. It never names an image, a volume, a container or a
machine, never carries what a step printed, and never says more about a
system error than that there was one.

Everything else is for staff and goes only to stdout, which is the CI's own
job log: the envelope's identity, the workspace volume, each step's image,
container exits, what each step printed (cut to its first and last 8K
characters when longer than 16K), callback trouble and the harness's own
faults. Every line of the run log goes there too, so the CI log reads as the
whole story.

Neither log ever carries the callback token, a presigned query or a secret's
value.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

LIMIT_BYTES = 8 * 1024 * 1024
BLOCK_LIMIT_CHARS = 16 * 1024
REDACTED = "<redacted>"


class RunLog:
    """Lines stamped with the seconds since the run began. `tell` is a line for
    the run log (and the CI log); `event` and `block` are for staff,
    in the CI log only. With `echo` off, as in tests, nothing is printed and
    the staff lines are kept for `staff_text`.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        echo: bool = True,
        limit_bytes: int = LIMIT_BYTES,
    ) -> None:
        self._clock = clock
        self._began = clock()
        self._echo = echo
        self._limit = limit_bytes
        self._told: list[str] = []
        self._staff: list[str] = []
        self._secrets: list[str] = []

    def hide(self, secret: str) -> None:
        """Never write `secret`, from here on: it is replaced wherever it appears.
        The longest is replaced first, so one that holds another goes whole.
        """
        if secret and secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)

    def tell(self, text: str) -> None:
        """A line of the run log."""
        line = self._stamped(text)
        self._told.append(line)
        self._print(line)

    def tell_block(self, title: str, text: str) -> None:
        """Text for the run log, indented under a title line."""
        self.tell(f"{title}:")
        for line in self._redacted(self._cut(text).rstrip("\n")).splitlines():
            indented = f"  | {line}"
            self._told.append(indented)
            self._print(indented)

    def event(self, text: str) -> None:
        """A line for staff only."""
        self._print(self._stamped(text))

    def block(self, title: str, text: str) -> None:
        """Text for staff only, such as what a step printed, cut in the middle
        when it is long.
        """
        self._print(self._stamped(f"{title}:"))
        for line in self._redacted(self._cut(text).rstrip("\n")).splitlines():
            self._print(f"  | {line}")

    def text(self) -> str:
        """The run log."""
        return "\n".join(self._told) + "\n"

    def staff_text(self) -> str:
        """Every line, as the CI log has it."""
        return "\n".join(self._staff) + "\n"

    def to_bytes(self) -> bytes:
        """The run log, cut in the middle when it is over the limit, so
        the start of the run and its end both survive.
        """
        data = self.text().encode("utf-8", errors="replace")
        if len(data) <= self._limit:
            return data
        half = self._limit // 2
        marker = f"\n... {len(data) - self._limit} bytes left out ...\n".encode()
        return data[:half] + marker + data[-half:]

    def _print(self, line: str) -> None:
        self._staff.append(line)
        if self._echo:
            print(line, file=sys.stdout, flush=True)

    def _stamped(self, text: str) -> str:
        elapsed = self._clock() - self._began
        return f"[{elapsed:9.3f}s] {self._redacted(text)}"

    def _redacted(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    @staticmethod
    def _cut(text: str) -> str:
        if len(text) <= BLOCK_LIMIT_CHARS:
            return text
        half = BLOCK_LIMIT_CHARS // 2
        dropped = len(text) - BLOCK_LIMIT_CHARS
        return f"{text[:half]}\n... {dropped} characters left out ...\n{text[-half:]}"
