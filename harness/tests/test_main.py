"""The exit codes and messages a grading job sees when the run never begins."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import Platform
from unicon_harness import contracts
from unicon_harness import main as entrypoint
from unicon_harness.main import (
    ENVELOPE_URL_VARIABLE,
    EXIT_CANNOT_START,
    EXIT_NOT_DELIVERED,
    GRADING_ID_VARIABLE,
    main,
)

UNUSED_PORT_URL = "http://127.0.0.1:9/envelope.json"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENVELOPE_URL_VARIABLE, raising=False)
    monkeypatch.delenv(GRADING_ID_VARIABLE, raising=False)


def _set_environment(
    monkeypatch: pytest.MonkeyPatch, url: str, grading_id: str
) -> None:
    monkeypatch.setenv(ENVELOPE_URL_VARIABLE, url)
    monkeypatch.setenv(GRADING_ID_VARIABLE, grading_id)


def test_a_missing_envelope_url_names_the_variable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main() == EXIT_CANNOT_START
    assert ENVELOPE_URL_VARIABLE in capsys.readouterr().err


def test_a_missing_grading_id_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ENVELOPE_URL_VARIABLE, UNUSED_PORT_URL)

    assert main() == EXIT_CANNOT_START
    assert GRADING_ID_VARIABLE in capsys.readouterr().err


def test_an_unreachable_url_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_environment(
        monkeypatch, UNUSED_PORT_URL, "0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31"
    )

    assert main() == EXIT_CANNOT_START
    assert "envelope_unreachable" in capsys.readouterr().err


def test_a_server_error_is_refused_without_the_key_in_the_log(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: Platform,
) -> None:
    platform.serve(b"denied", status=403)
    _set_environment(
        monkeypatch, platform.envelope_url, "0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31"
    )

    assert main() == EXIT_CANNOT_START
    printed = capsys.readouterr().err
    assert "envelope_unreachable" in printed
    assert "envelope-secret" not in printed


def test_a_skewed_schema_version_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: Platform,
    example_envelope: dict[str, Any],
) -> None:
    example_envelope["schema_version"] = 99
    platform.serve_envelope(example_envelope)
    _set_environment(monkeypatch, platform.envelope_url, example_envelope["grading_id"])

    assert main() == EXIT_CANNOT_START
    assert "schema_version_mismatch" in capsys.readouterr().err


def test_a_malformed_envelope_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: Platform,
    example_envelope: dict[str, Any],
) -> None:
    del example_envelope["callback"]
    platform.serve_envelope(example_envelope)
    _set_environment(monkeypatch, platform.envelope_url, example_envelope["grading_id"])

    assert main() == EXIT_CANNOT_START
    assert "schema_violation" in capsys.readouterr().err
    assert platform.callbacks == []


def test_an_envelope_for_another_run_is_refused_and_reports_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: Platform,
    example_envelope: dict[str, Any],
) -> None:
    example_envelope["callback"]["url"] = f"{platform.url}/callback"
    platform.serve_envelope(example_envelope)
    _set_environment(
        monkeypatch, platform.envelope_url, "0199a2c1-6b7e-7c3a-9f10-000000000000"
    )

    assert main() == EXIT_CANNOT_START
    assert "grading_id_mismatch" in capsys.readouterr().err
    assert platform.callbacks == []


def test_the_grading_id_is_compared_as_a_uuid_not_as_text(
    monkeypatch: pytest.MonkeyPatch,
    platform: Platform,
    example_envelope: dict[str, Any],
) -> None:
    """The CI may pass the id in whatever case its own storage kept. Past the
    check the run goes on, and here stops at the deadline already gone.
    """
    example_envelope["deadline"] = "2000-01-01T00:00:00Z"
    platform.serve_envelope(example_envelope)
    _set_environment(
        monkeypatch, platform.envelope_url, example_envelope["grading_id"].upper()
    )

    assert main() == EXIT_NOT_DELIVERED


def test_a_grading_id_that_is_not_a_uuid_is_a_mismatch_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: Platform,
    example_envelope: dict[str, Any],
) -> None:
    platform.serve_envelope(example_envelope)
    _set_environment(monkeypatch, platform.envelope_url, "the-third-one")

    assert main() == EXIT_CANNOT_START
    assert "grading_id_mismatch" in capsys.readouterr().err


def test_storage_that_stops_answering_is_refused_on_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: Platform,
    example_envelope: dict[str, Any],
) -> None:
    """The timeout is per operation, so a connection that opens and then goes
    quiet is refused rather than holding the job until Woodpecker kills it.
    """
    monkeypatch.setattr(entrypoint, "FETCH_IO_TIMEOUT_SECONDS", 0.05)
    platform.serve(json.dumps(example_envelope).encode("utf-8"), delay=0.5)
    _set_environment(monkeypatch, platform.envelope_url, example_envelope["grading_id"])

    assert main() == EXIT_CANNOT_START
    assert "envelope_unreachable" in capsys.readouterr().err


def test_an_image_built_without_the_schemas_says_so(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: Platform,
    example_envelope: dict[str, Any],
) -> None:
    platform.serve_envelope(example_envelope)
    _set_environment(monkeypatch, platform.envelope_url, example_envelope["grading_id"])
    monkeypatch.setattr(contracts, "SCHEMA_CANDIDATES", (Path("nowhere/schemas"),))
    _forget_resolved_schemas()

    try:
        assert main() == EXIT_CANNOT_START
        printed = capsys.readouterr().err
        assert "schemas_missing" in printed
        assert "nowhere" in printed
    finally:
        _forget_resolved_schemas()


def _forget_resolved_schemas() -> None:
    """Both caches hold what was found at the first grading, so a test has to
    clear them.
    """
    contracts.schema_dir.cache_clear()
    contracts.validator.cache_clear()
