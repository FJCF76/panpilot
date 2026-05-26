"""Tests for T10: startup catch-up loader."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from panpilot.config import get_settings
from panpilot.intake.catchup import (
    CATCHUP_EVENT_TYPE,
    _DEFAULT_LOOKBACK_HOURS,
    get_last_received_at,
    run_startup_catchup,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    schema = (Path(__file__).parent.parent / "panpilot" / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    return conn


def _mock_client(incidents: list[dict]) -> MagicMock:
    client = MagicMock()
    client.get_incidents_modified_since.return_value = incidents
    return client


def _incident(id_: str = "TKT-001", **kwargs) -> dict:
    return {
        "IncidentId": id_,
        "Title": "Test ticket",
        "DateLastModified": "2026-05-25T10:00:00Z",
        **kwargs,
    }


def _insert_event(conn: sqlite3.Connection, received_at: str) -> None:
    conn.execute(
        "INSERT INTO events (id, ticket_id, event_type, payload, received_at) "
        "VALUES ('key-1', 'TKT-001', 'Guardado', '{}', ?)",
        (received_at,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# get_last_received_at
# ---------------------------------------------------------------------------

def test_get_last_received_at_returns_none_on_empty_db():
    conn = _conn()
    assert get_last_received_at(conn) is None


def test_get_last_received_at_returns_max_timestamp():
    conn = _conn()
    _insert_event(conn, "2026-05-25T08:00:00.000000Z")
    conn.execute(
        "INSERT INTO events (id, ticket_id, event_type, payload, received_at) "
        "VALUES ('key-2', 'TKT-002', 'Guardado', '{}', '2026-05-25T12:00:00.000000Z')"
    )
    conn.commit()
    result = get_last_received_at(conn)
    assert result == "2026-05-25T12:00:00.000000Z"


# ---------------------------------------------------------------------------
# run_startup_catchup — happy path
# ---------------------------------------------------------------------------

def test_catchup_injects_new_incidents():
    conn = _conn()
    client = _mock_client([_incident("TKT-001"), _incident("TKT-002")])
    count = run_startup_catchup(get_settings(), conn, client=client)
    assert count == 2


def test_catchup_uses_since_from_db():
    conn = _conn()
    _insert_event(conn, "2026-05-25T10:00:00.000000Z")
    client = _mock_client([])
    run_startup_catchup(get_settings(), conn, client=client)
    called_since = client.get_incidents_modified_since.call_args[0][0]
    assert called_since == "2026-05-25T10:00:00.000000Z"


def test_catchup_uses_default_lookback_on_empty_db():
    from datetime import datetime, timezone
    conn = _conn()
    client = _mock_client([])
    before = datetime.now(timezone.utc)
    run_startup_catchup(get_settings(), conn, client=client)
    called_since = client.get_incidents_modified_since.call_args[0][0]
    # Should be roughly _DEFAULT_LOOKBACK_HOURS in the past
    from datetime import timedelta
    since_dt = datetime.fromisoformat(called_since.replace("Z", "+00:00"))
    assert since_dt < before - timedelta(hours=_DEFAULT_LOOKBACK_HOURS - 1)


def test_catchup_deduplicated_on_second_run():
    conn = _conn()
    incidents = [_incident("TKT-001")]
    client = _mock_client(incidents)
    first = run_startup_catchup(get_settings(), conn, client=client)
    client2 = _mock_client(incidents)
    second = run_startup_catchup(get_settings(), conn, client=client2)
    assert first == 1
    assert second == 0  # same idempotency key — INSERT OR IGNORE


def test_catchup_stores_correct_event_type():
    conn = _conn()
    client = _mock_client([_incident("TKT-X")])
    run_startup_catchup(get_settings(), conn, client=client)
    row = conn.execute("SELECT event_type FROM events WHERE ticket_id='TKT-X'").fetchone()
    assert row["event_type"] == CATCHUP_EVENT_TYPE


def test_catchup_uses_id_field_as_fallback():
    conn = _conn()
    incident = {"Id": "TKT-ALT", "DateLastModified": "2026-05-25T10:00:00Z"}
    client = _mock_client([incident])
    count = run_startup_catchup(get_settings(), conn, client=client)
    assert count == 1
    row = conn.execute("SELECT ticket_id FROM events").fetchone()
    assert row["ticket_id"] == "TKT-ALT"


def test_catchup_skips_incidents_with_no_id():
    conn = _conn()
    incident = {"Title": "No ID", "DateLastModified": "2026-05-25T10:00:00Z"}
    client = _mock_client([incident])
    count = run_startup_catchup(get_settings(), conn, client=client)
    assert count == 0


def test_catchup_returns_zero_on_empty_incident_list():
    conn = _conn()
    count = run_startup_catchup(get_settings(), conn, client=_mock_client([]))
    assert count == 0


# ---------------------------------------------------------------------------
# run_startup_catchup — API error swallowed
# ---------------------------------------------------------------------------

def test_catchup_api_error_returns_zero_and_does_not_raise():
    conn = _conn()
    client = MagicMock()
    client.get_incidents_modified_since.side_effect = httpx.ConnectError("timeout")
    count = run_startup_catchup(get_settings(), conn, client=client)
    assert count == 0


# ---------------------------------------------------------------------------
# run_startup_catchup — terminal status filtering
# ---------------------------------------------------------------------------

_TERMINAL_NAMES: frozenset[str] = frozenset({"closed", "rejected"})


def test_catchup_skips_terminal_status_incident():
    conn = _conn()
    incident = _incident("TKT-001", Status="Closed")
    client = _mock_client([incident])
    count = run_startup_catchup(get_settings(), conn, client=client, terminal_status_names=_TERMINAL_NAMES)
    assert count == 0


def test_catchup_terminal_incident_not_stored_in_db():
    conn = _conn()
    incident = _incident("TKT-001", Status="Rejected")
    client = _mock_client([incident])
    run_startup_catchup(get_settings(), conn, client=client, terminal_status_names=_TERMINAL_NAMES)
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 0


def test_catchup_non_terminal_incident_is_stored():
    conn = _conn()
    incident = _incident("TKT-001", Status="New")
    client = _mock_client([incident])
    count = run_startup_catchup(get_settings(), conn, client=client, terminal_status_names=_TERMINAL_NAMES)
    assert count == 1


def test_catchup_mixed_skips_only_terminal():
    conn = _conn()
    incidents = [
        _incident("TKT-001", Status="Closed"),
        _incident("TKT-002", Status="New"),
        _incident("TKT-003", Status="Rejected"),
        _incident("TKT-004", Status="Assigned"),
    ]
    client = _mock_client(incidents)
    count = run_startup_catchup(get_settings(), conn, client=client, terminal_status_names=_TERMINAL_NAMES)
    assert count == 2
    stored_ids = {r[0] for r in conn.execute("SELECT ticket_id FROM events").fetchall()}
    assert stored_ids == {"TKT-002", "TKT-004"}


def test_catchup_empty_terminal_set_processes_all():
    conn = _conn()
    client = _mock_client([_incident("TKT-001"), _incident("TKT-002")])
    count = run_startup_catchup(get_settings(), conn, client=client, terminal_status_names=frozenset())
    assert count == 2


def test_catchup_incident_with_no_status_field_is_not_skipped():
    # Incidents missing the Status field entirely should still be stored.
    conn = _conn()
    incident = {"IncidentId": "TKT-001", "DateLastModified": "2026-05-25T10:00:00Z"}
    client = _mock_client([incident])
    count = run_startup_catchup(get_settings(), conn, client=client, terminal_status_names=_TERMINAL_NAMES)
    assert count == 1


def test_catchup_http_error_returns_zero_and_does_not_raise():
    conn = _conn()
    client = MagicMock()
    client.get_incidents_modified_since.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock()
    )
    count = run_startup_catchup(get_settings(), conn, client=client)
    assert count == 0


# ---------------------------------------------------------------------------
# run_startup_catchup — since= parameter (Bug 2 fix)
# ---------------------------------------------------------------------------

def test_catchup_since_param_overrides_db_watermark():
    conn = _conn()
    # DB has an event at 12:00; caller passes an earlier watermark of 08:00
    _insert_event(conn, "2026-05-25T12:00:00.000000Z")
    client = _mock_client([])
    run_startup_catchup(get_settings(), conn, since="2026-05-25T08:00:00Z", client=client)
    called_since = client.get_incidents_modified_since.call_args[0][0]
    assert called_since == "2026-05-25T08:00:00Z"


def test_catchup_since_none_falls_back_to_db():
    conn = _conn()
    _insert_event(conn, "2026-05-25T09:30:00.000000Z")
    client = _mock_client([])
    run_startup_catchup(get_settings(), conn, since=None, client=client)
    called_since = client.get_incidents_modified_since.call_args[0][0]
    assert called_since == "2026-05-25T09:30:00.000000Z"


def test_catchup_since_none_empty_db_uses_default_lookback():
    from datetime import datetime, timedelta, timezone
    conn = _conn()
    client = _mock_client([])
    before = datetime.now(timezone.utc)
    run_startup_catchup(get_settings(), conn, since=None, client=client)
    called_since = client.get_incidents_modified_since.call_args[0][0]
    since_dt = datetime.fromisoformat(called_since.replace("Z", "+00:00"))
    assert since_dt < before - timedelta(hours=_DEFAULT_LOOKBACK_HOURS - 1)


def test_catchup_since_param_with_new_incidents():
    conn = _conn()
    client = _mock_client([_incident("TKT-001"), _incident("TKT-002")])
    count = run_startup_catchup(
        get_settings(), conn,
        since="2026-05-25T08:00:00Z",
        client=client,
    )
    assert count == 2
    called_since = client.get_incidents_modified_since.call_args[0][0]
    assert called_since == "2026-05-25T08:00:00Z"
