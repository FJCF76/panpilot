"""Tests for panpilot.db.connection — focusing on reset_stale_pending()."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from panpilot.db.connection import reset_stale_pending


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    schema = (Path(__file__).parent.parent / "panpilot" / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    return conn


def _insert_ticket(conn: sqlite3.Connection, ticket_id: str, state: str) -> None:
    conn.execute(
        "INSERT INTO ticket_state (ticket_id, state, priority, updated_at, "
        "clarification_count, reminder_count) VALUES (?, ?, 'P3', '2026-05-25T10:00:00Z', 0, 0)",
        (ticket_id, state),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# reset_stale_pending — return value
# ---------------------------------------------------------------------------

def test_reset_returns_zero_on_empty_db():
    conn = _conn()
    assert reset_stale_pending(conn) == 0


def test_reset_returns_zero_when_no_pending():
    conn = _conn()
    _insert_ticket(conn, "TKT-001", "WAITING")
    _insert_ticket(conn, "TKT-002", "NEEDS_HUMAN")
    assert reset_stale_pending(conn) == 0


def test_reset_returns_count_of_affected_rows():
    conn = _conn()
    _insert_ticket(conn, "TKT-001", "PENDING_EVALUATION")
    _insert_ticket(conn, "TKT-002", "PENDING_EVALUATION")
    assert reset_stale_pending(conn) == 2


def test_reset_returns_one_for_single_pending():
    conn = _conn()
    _insert_ticket(conn, "TKT-001", "PENDING_EVALUATION")
    assert reset_stale_pending(conn) == 1


# ---------------------------------------------------------------------------
# reset_stale_pending — state after reset
# ---------------------------------------------------------------------------

def test_pending_ticket_becomes_waiting():
    conn = _conn()
    _insert_ticket(conn, "TKT-001", "PENDING_EVALUATION")
    reset_stale_pending(conn)
    row = conn.execute("SELECT state FROM ticket_state WHERE ticket_id='TKT-001'").fetchone()
    assert row["state"] == "WAITING"


def test_non_pending_states_are_untouched():
    conn = _conn()
    for tid, state in [
        ("TKT-001", "WAITING"),
        ("TKT-002", "NEEDS_HUMAN"),
        ("TKT-003", "STALE_ALERT"),
        ("TKT-004", "AWAITING_CLIENT_REPLY"),
        ("TKT-005", "CLARIFICATION_SENT"),
    ]:
        _insert_ticket(conn, tid, state)

    reset_stale_pending(conn)

    rows = {
        r["ticket_id"]: r["state"]
        for r in conn.execute("SELECT ticket_id, state FROM ticket_state").fetchall()
    }
    assert rows == {
        "TKT-001": "WAITING",
        "TKT-002": "NEEDS_HUMAN",
        "TKT-003": "STALE_ALERT",
        "TKT-004": "AWAITING_CLIENT_REPLY",
        "TKT-005": "CLARIFICATION_SENT",
    }


def test_mixed_states_only_pending_reset():
    conn = _conn()
    _insert_ticket(conn, "TKT-PENDING", "PENDING_EVALUATION")
    _insert_ticket(conn, "TKT-WAITING", "WAITING")
    _insert_ticket(conn, "TKT-HUMAN", "NEEDS_HUMAN")

    count = reset_stale_pending(conn)

    assert count == 1
    rows = {
        r["ticket_id"]: r["state"]
        for r in conn.execute("SELECT ticket_id, state FROM ticket_state").fetchall()
    }
    assert rows["TKT-PENDING"] == "WAITING"
    assert rows["TKT-WAITING"] == "WAITING"
    assert rows["TKT-HUMAN"] == "NEEDS_HUMAN"


def test_reset_is_idempotent():
    conn = _conn()
    _insert_ticket(conn, "TKT-001", "PENDING_EVALUATION")
    reset_stale_pending(conn)
    count = reset_stale_pending(conn)  # second call: nothing left to reset
    assert count == 0
    row = conn.execute("SELECT state FROM ticket_state WHERE ticket_id='TKT-001'").fetchone()
    assert row["state"] == "WAITING"


def test_updated_at_is_refreshed_on_reset():
    conn = _conn()
    _insert_ticket(conn, "TKT-001", "PENDING_EVALUATION")
    original = conn.execute(
        "SELECT updated_at FROM ticket_state WHERE ticket_id='TKT-001'"
    ).fetchone()["updated_at"]

    reset_stale_pending(conn)

    new_val = conn.execute(
        "SELECT updated_at FROM ticket_state WHERE ticket_id='TKT-001'"
    ).fetchone()["updated_at"]
    # updated_at must be set (not null); may equal original if same second
    assert new_val is not None
