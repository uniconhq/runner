"""The run log keeps secrets out, keeps staff detail out of what the contestant
reads, and stays within its size.
"""

from __future__ import annotations

from unicon_harness.runlog import BLOCK_LIMIT_CHARS, REDACTED, RunLog


def test_a_hidden_secret_never_reaches_either_log() -> None:
    log = RunLog(echo=False)
    log.hide("token-123")
    log.tell("told with token-123")
    log.tell_block("summary", "and token-123 again")
    log.event("posting with token-123")
    log.block("step printed", "saw token-123 twice: token-123")

    assert "token-123" not in log.text()
    assert "token-123" not in log.staff_text()
    assert log.text().count(REDACTED) == 2
    assert log.staff_text().count(REDACTED) == 5


def test_staff_lines_stay_out_of_the_contestants_log() -> None:
    log = RunLog(echo=False)
    log.tell("compile (compile@v1): accepted")
    log.event("step compile: from ghcr.io/uniconhq/primitive-compile@sha256:1")
    log.block("step compile printed", "volume wp_1_default")

    assert log.text().splitlines()[0].endswith("compile (compile@v1): accepted")
    assert "sha256" not in log.text() and "wp_1_default" not in log.text()
    staff = log.staff_text()
    assert "compile@v1): accepted" in staff
    assert "sha256" in staff and "wp_1_default" in staff


def test_what_a_step_printed_is_cut_in_the_middle() -> None:
    log = RunLog(echo=False)
    log.block("step run printed", "start " + "x" * (BLOCK_LIMIT_CHARS * 2) + " end")

    staff = log.staff_text()
    assert "start" in staff and "end" in staff
    assert "characters left out" in staff
    assert len(staff) < BLOCK_LIMIT_CHARS * 1.2


def test_a_long_log_keeps_its_start_and_its_end() -> None:
    log = RunLog(echo=False, limit_bytes=1000)
    log.tell("the start")
    for n in range(500):
        log.tell(f"line {n}")
    log.tell("the end")

    data = log.to_bytes()
    assert len(data) < 1100
    assert b"the start" in data and b"the end" in data
    assert b"bytes left out" in data


def test_lines_are_stamped_with_seconds_since_the_run_began() -> None:
    ticks = iter([100.0, 101.5])
    log = RunLog(clock=lambda: next(ticks), echo=False)
    log.tell("hello")
    assert log.text().startswith("[    1.500s] hello")


def test_a_secret_holding_another_is_hidden_whole() -> None:
    log = RunLog(echo=False)
    log.hide("key")
    log.hide("key-and-more")
    log.tell("sent key-and-more")

    assert log.text().endswith(f"sent {REDACTED}\n")
