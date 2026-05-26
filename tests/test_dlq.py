"""Tests for T4: dead-letter queue worker (create_dlq_entry + DLQThread)."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from panpilot.worker.dlq import (
    MAX_ATTEMPTS,
    RETRY_DELAYS_SECONDS,
    DLQThread,
    create_dlq_entry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    schema = (Path(__file__).parent.parent / "panpilot" / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    return conn


def _past(seconds: int = 60) -> str:
    """ISO timestamp that is `seconds` in the past."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"


def _future(seconds: int = 60) -> str:
    """ISO timestamp that is `seconds` in the future."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"


def _insert_event(conn: sqlite3.Connection, event_id: str = "evt-1", processed: int = 1) -> None:
    conn.execute(
        "INSERT INTO events (id, ticket_id, event_type, payload, processed) "
        "VALUES (?, 'TKT-001', 'Guardado', '{\"test\": true}', ?)",
        (event_id, processed),
    )
    conn.commit()


def _insert_dlq(
    conn: sqlite3.Connection,
    event_id: str = "evt-1",
    attempts: int = 1,
    next_retry: str | None = None,
    exhausted: int = 0,
    error: str = "RuntimeError: boom",
) -> int:
    if next_retry is None:
        next_retry = _past(60)
    cur = conn.execute(
        "INSERT INTO dlq (event_id, error, attempts, next_retry, exhausted) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_id, error, attempts, next_retry, exhausted),
    )
    conn.commit()
    return cur.lastrowid


def _noop(event: dict) -> None:
    pass


def _fail(event: dict) -> None:
    raise RuntimeError("process failed")


# ---------------------------------------------------------------------------
# create_dlq_entry
# ---------------------------------------------------------------------------

def test_create_dlq_entry_inserts_row():
    conn = _conn()
    _insert_event(conn)
    create_dlq_entry(conn, "evt-1", "ValueError: bad")
    row = conn.execute("SELECT * FROM dlq WHERE event_id='evt-1'").fetchone()
    assert row is not None


def test_create_dlq_entry_sets_attempts_to_1():
    conn = _conn()
    _insert_event(conn)
    create_dlq_entry(conn, "evt-1", "error")
    row = conn.execute("SELECT attempts FROM dlq WHERE event_id='evt-1'").fetchone()
    assert row["attempts"] == 1


def test_create_dlq_entry_next_retry_is_roughly_30s_ahead():
    conn = _conn()
    _insert_event(conn)
    before = datetime.now(timezone.utc)
    create_dlq_entry(conn, "evt-1", "error")
    after = datetime.now(timezone.utc)
    row = conn.execute("SELECT next_retry FROM dlq WHERE event_id='evt-1'").fetchone()
    nr = datetime.fromisoformat(row["next_retry"].replace("Z", "+00:00"))
    assert before + timedelta(seconds=RETRY_DELAYS_SECONDS[0] - 1) <= nr
    assert nr <= after + timedelta(seconds=RETRY_DELAYS_SECONDS[0] + 1)


def test_create_dlq_entry_exhausted_is_0():
    conn = _conn()
    _insert_event(conn)
    create_dlq_entry(conn, "evt-1", "error")
    row = conn.execute("SELECT exhausted FROM dlq WHERE event_id='evt-1'").fetchone()
    assert row["exhausted"] == 0


def test_create_dlq_entry_stores_error_message():
    conn = _conn()
    _insert_event(conn)
    create_dlq_entry(conn, "evt-1", "TypeError: bad value")
    row = conn.execute("SELECT error FROM dlq WHERE event_id='evt-1'").fetchone()
    assert "TypeError" in row["error"]


# ---------------------------------------------------------------------------
# DLQThread._process_due — success path
# ---------------------------------------------------------------------------

def test_process_due_calls_process_fn_for_due_entry():
    conn = _conn()
    _insert_event(conn)
    _insert_dlq(conn, next_retry=_past(60))
    called = []
    thread = DLQThread(conn, lambda e: called.append(e["id"]))
    thread._process_due()
    assert called == ["evt-1"]


def test_process_due_deletes_dlq_entry_on_success():
    conn = _conn()
    _insert_event(conn)
    entry_id = _insert_dlq(conn, next_retry=_past(60))
    thread = DLQThread(conn, _noop)
    thread._process_due()
    row = conn.execute("SELECT id FROM dlq WHERE id=?", (entry_id,)).fetchone()
    assert row is None


def test_process_due_skips_future_entries():
    conn = _conn()
    _insert_event(conn)
    _insert_dlq(conn, next_retry=_future(300))
    called = []
    thread = DLQThread(conn, lambda e: called.append(e["id"]))
    thread._process_due()
    assert called == []


def test_process_due_skips_exhausted_entries():
    conn = _conn()
    _insert_event(conn)
    _insert_dlq(conn, exhausted=1, next_retry=_past(60))
    called = []
    thread = DLQThread(conn, lambda e: called.append(e["id"]))
    thread._process_due()
    assert called == []


def test_process_due_processes_multiple_due_entries():
    conn = _conn()
    _insert_event(conn, "evt-1")
    _insert_event(conn, "evt-2")
    _insert_dlq(conn, "evt-1", next_retry=_past(60))
    _insert_dlq(conn, "evt-2", next_retry=_past(30))
    called = []
    thread = DLQThread(conn, lambda e: called.append(e["id"]))
    thread._process_due()
    assert set(called) == {"evt-1", "evt-2"}


# ---------------------------------------------------------------------------
# DLQThread._retry_one — failure path (backoff)
# ---------------------------------------------------------------------------

def test_retry_increments_attempts_on_failure():
    conn = _conn()
    _insert_event(conn)
    entry_id = _insert_dlq(conn, attempts=1, next_retry=_past(60))
    thread = DLQThread(conn, _fail)
    thread._process_due()
    row = conn.execute("SELECT attempts FROM dlq WHERE id=?", (entry_id,)).fetchone()
    assert row["attempts"] == 2


def test_retry_updates_next_retry_on_second_failure():
    conn = _conn()
    _insert_event(conn)
    entry_id = _insert_dlq(conn, attempts=1, next_retry=_past(60))
    before = datetime.now(timezone.utc)
    thread = DLQThread(conn, _fail)
    thread._process_due()
    after = datetime.now(timezone.utc)
    row = conn.execute("SELECT next_retry FROM dlq WHERE id=?", (entry_id,)).fetchone()
    nr = datetime.fromisoformat(row["next_retry"].replace("Z", "+00:00"))
    expected_delay = RETRY_DELAYS_SECONDS[1]  # delay after 2nd failure
    assert before + timedelta(seconds=expected_delay - 1) <= nr
    assert nr <= after + timedelta(seconds=expected_delay + 1)


def test_retry_sets_exhausted_on_third_failure():
    conn = _conn()
    _insert_event(conn)
    # attempts=2 means this is the 3rd attempt (MAX_ATTEMPTS=3)
    entry_id = _insert_dlq(conn, attempts=MAX_ATTEMPTS - 1, next_retry=_past(60))
    thread = DLQThread(conn, _fail)
    thread._process_due()
    row = conn.execute("SELECT exhausted FROM dlq WHERE id=?", (entry_id,)).fetchone()
    assert row["exhausted"] == 1


def test_exhausted_entry_has_null_next_retry():
    conn = _conn()
    _insert_event(conn)
    entry_id = _insert_dlq(conn, attempts=MAX_ATTEMPTS - 1, next_retry=_past(60))
    thread = DLQThread(conn, _fail)
    thread._process_due()
    row = conn.execute("SELECT next_retry FROM dlq WHERE id=?", (entry_id,)).fetchone()
    assert row["next_retry"] is None


def test_exhausted_entry_records_latest_error():
    conn = _conn()
    _insert_event(conn)
    entry_id = _insert_dlq(conn, attempts=MAX_ATTEMPTS - 1, next_retry=_past(60))

    def _fail_with_message(event: dict) -> None:
        raise RuntimeError("final failure message")

    thread = DLQThread(conn, _fail_with_message)
    thread._process_due()
    row = conn.execute("SELECT error FROM dlq WHERE id=?", (entry_id,)).fetchone()
    assert "final failure message" in row["error"]


def test_retry_does_not_exceed_max_attempts():
    """After exhaustion, attempts == MAX_ATTEMPTS (not higher)."""
    conn = _conn()
    _insert_event(conn)
    entry_id = _insert_dlq(conn, attempts=MAX_ATTEMPTS - 1, next_retry=_past(60))
    thread = DLQThread(conn, _fail)
    thread._process_due()
    row = conn.execute("SELECT attempts FROM dlq WHERE id=?", (entry_id,)).fetchone()
    assert row["attempts"] == MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Missing event edge case
# ---------------------------------------------------------------------------

def test_missing_event_removes_dlq_entry():
    conn = _conn()
    # Insert DLQ entry with no matching event row
    entry_id = _insert_dlq(conn, event_id="ghost-event", next_retry=_past(60))
    called = []
    thread = DLQThread(conn, lambda e: called.append(e))
    thread._process_due()
    # DLQ entry should be removed
    row = conn.execute("SELECT id FROM dlq WHERE id=?", (entry_id,)).fetchone()
    assert row is None
    # process_fn should not have been called
    assert called == []


# ---------------------------------------------------------------------------
# Thread lifecycle
# ---------------------------------------------------------------------------

def test_dlq_thread_starts_and_stops():
    conn = _conn()
    thread = DLQThread(conn, _noop, poll_interval=0.05)
    thread.start()
    assert thread._thread.is_alive()
    thread.stop(timeout=1.0)
    assert not thread._thread.is_alive()


def test_dlq_thread_processes_entry_while_running():
    conn = _conn()
    _insert_event(conn)
    _insert_dlq(conn, next_retry=_past(60))

    processed = threading.Event()

    def _mark(event: dict) -> None:
        processed.set()

    thread = DLQThread(conn, _mark, poll_interval=0.05)
    thread.start()
    assert processed.wait(timeout=2.0), "DLQ thread did not process entry within 2s"
    thread.stop(timeout=1.0)


def test_dlq_thread_stops_cleanly_when_no_entries():
    conn = _conn()
    thread = DLQThread(conn, _noop, poll_interval=0.05)
    thread.start()
    thread.stop(timeout=1.0)
    assert not thread._thread.is_alive()


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------

def test_retry_delays_has_correct_count():
    assert len(RETRY_DELAYS_SECONDS) == MAX_ATTEMPTS


def test_retry_delays_are_increasing():
    for i in range(len(RETRY_DELAYS_SECONDS) - 1):
        assert RETRY_DELAYS_SECONDS[i] < RETRY_DELAYS_SECONDS[i + 1]
