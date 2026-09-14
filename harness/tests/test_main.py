"""The exit codes and messages a grading job sees from the entrypoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import EnvelopeServer
from unicon_harness import contracts, envelope
from unicon_harness import main as entrypoint
from unicon_harness.main import (
    ENVELOPE_URL_VARIABLE,
    EXIT_CANNOT_START,
    EXIT_OK,
    JUDGING_ID_VARIABLE,
    main,
)

UNUSED_PORT_URL = "http://127.0.0.1:9/envelope.json"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENVELOPE_URL_VARIABLE, raising=False)
    monkeypatch.delenv(JUDGING_ID_VARIABLE, raising=False)


def _set_environment(
    monkeypatch: pytest.MonkeyPatch, url: str, judging_id: str
) -> None:
    monkeypatch.setenv(ENVELOPE_URL_VARIABLE, url)
    monkeypatch.setenv(JUDGING_ID_VARIABLE, judging_id)


def test_a_good_envelope_exits_zero_with_a_one_line_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    envelope_server: EnvelopeServer,
    example_envelope: dict[str, Any],
) -> None:
    envelope_server.serve(json.dumps(example_envelope).encode("utf-8"))
    _set_environment(monkeypatch, envelope_server.url, example_envelope["judging_id"])

    assert main() == EXIT_OK

    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 1
    assert example_envelope["judging_id"] in printed[0]
    assert "acme-open-2026/alice-two-sum@submission/3" in printed[0]


def test_a_missing_envelope_url_names_the_variable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main() == EXIT_CANNOT_START
    assert ENVELOPE_URL_VARIABLE in capsys.readouterr().err


def test_a_missing_judging_id_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ENVELOPE_URL_VARIABLE, UNUSED_PORT_URL)

    assert main() == EXIT_CANNOT_START
    assert JUDGING_ID_VARIABLE in capsys.readouterr().err


def test_an_unreachable_url_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_environment(
        monkeypatch, UNUSED_PORT_URL, "0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31"
    )

    assert main() == EXIT_CANNOT_START
    assert "envelope_unreachable" in capsys.readouterr().err


def test_a_server_error_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    envelope_server: EnvelopeServer,
) -> None:
    envelope_server.serve(b"denied", status=403)
    _set_environment(
        monkeypatch, envelope_server.url, "0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31"
    )

    assert main() == EXIT_CANNOT_START
    assert "envelope_unreachable" in capsys.readouterr().err


def test_a_skewed_schema_version_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    envelope_server: EnvelopeServer,
    example_envelope: dict[str, Any],
) -> None:
    example_envelope["schema_version"] = 99
    envelope_server.serve(json.dumps(example_envelope).encode("utf-8"))
    _set_environment(monkeypatch, envelope_server.url, example_envelope["judging_id"])

    assert main() == EXIT_CANNOT_START
    assert "schema_version_mismatch" in capsys.readouterr().err


def test_an_envelope_for_another_job_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    envelope_server: EnvelopeServer,
    example_envelope: dict[str, Any],
) -> None:
    envelope_server.serve(json.dumps(example_envelope).encode("utf-8"))
    _set_environment(
        monkeypatch, envelope_server.url, "0199a2c1-6b7e-7c3a-9f10-000000000000"
    )

    assert main() == EXIT_CANNOT_START
    assert "judging_id_mismatch" in capsys.readouterr().err


def test_the_judging_id_is_compared_as_a_uuid_not_as_text(
    monkeypatch: pytest.MonkeyPatch,
    envelope_server: EnvelopeServer,
    example_envelope: dict[str, Any],
) -> None:
    """Woodpecker may pass the id in whatever case its own storage kept."""
    envelope_server.serve(json.dumps(example_envelope).encode("utf-8"))
    _set_environment(
        monkeypatch, envelope_server.url, example_envelope["judging_id"].upper()
    )

    assert main() == EXIT_OK


def test_a_judging_id_that_is_not_a_uuid_is_a_mismatch_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    envelope_server: EnvelopeServer,
    example_envelope: dict[str, Any],
) -> None:
    envelope_server.serve(json.dumps(example_envelope).encode("utf-8"))
    _set_environment(monkeypatch, envelope_server.url, "the-third-one")

    assert main() == EXIT_CANNOT_START
    assert "judging_id_mismatch" in capsys.readouterr().err


def test_storage_that_stops_answering_is_refused_on_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    envelope_server: EnvelopeServer,
    example_envelope: dict[str, Any],
) -> None:
    """The timeout is per operation, so a connection that opens and then goes
    quiet is refused rather than holding the job until Woodpecker kills it.
    """
    monkeypatch.setattr(entrypoint, "FETCH_IO_TIMEOUT_SECONDS", 0.05)
    envelope_server.serve(json.dumps(example_envelope).encode("utf-8"), delay=0.5)
    _set_environment(monkeypatch, envelope_server.url, example_envelope["judging_id"])

    assert main() == EXIT_CANNOT_START
    assert "envelope_unreachable" in capsys.readouterr().err


def test_an_image_built_without_the_schemas_says_so(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    envelope_server: EnvelopeServer,
    example_envelope: dict[str, Any],
) -> None:
    envelope_server.serve(json.dumps(example_envelope).encode("utf-8"))
    _set_environment(monkeypatch, envelope_server.url, example_envelope["judging_id"])
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
    """Both caches hold the directory found at the first grading, so a test has to
    clear them.
    """
    contracts.schema_dir.cache_clear()
    envelope._validator.cache_clear()
