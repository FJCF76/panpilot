"""Tests for T2: event store, idempotency key, work queue."""
import hashlib
import sqlite3

import pytest

from panpilot.config import get_settings
from panpilot.db.connection import get_connection, init_db, main_db_path
from panpilot.intake.event_store import (
    claim_next_event,
    compute_idempotency_key,
    mark_event_processed,
    store_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_memory_conn() -> sqlite3.Connection:
    """Open an in-memory DB with schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    from pathlib import Path
    schema_sql = (Path(__file__).parent.parent / "panpilot" / "db" / "schema.sql").read_text()
    conn.executescript(schema_sql)
    return conn


def _payload(**kwargs) -> dict:
    return {"DateLastModified": "2026-05-25T10:00:00Z", **kwargs}


# ---------------------------------------------------------------------------
# compute_idempotency_key
# ---------------------------------------------------------------------------

def test_uses_configured_field_when_present(monkeypatch):
    monkeypatch.setenv("WEBHOOK_IDEMPOTENCY_FIELD", "DeliveryId")
    get_settings.cache_clear()
    payload = _payload(DeliveryId="abc-123")
    key = compute_idempotency_key(payload, "ticket-1", "Creación", get_settings())
    assert key == "abc-123"


def test_falls_back_when_configured_field_absent(monkeypatch):
    monkeypatch.setenv("WEBHOOK_IDEMPOTENCY_FIELD", "DeliveryId")
    get_settings.cache_clear()
    payload = _payload()  # no DeliveryId
    key = compute_idempotency_key(payload, "ticket-1", "Creación", get_settings())
    expected = hashlib.sha256("ticket-1:Creación:2026-05-25T10:00:00Z".encode()).hexdigest()
    assert key == expected


def test_falls_back_when_no_field_configured():
    # WEBHOOK_IDEMPOTENCY_FIELD is "" in test env (default)
    payload = _payload()
    key = compute_idempotency_key(payload, "ticket-1", "Creación", get_settings())
    expected = hashlib.sha256("ticket-1:Creación:2026-05-25T10:00:00Z".encode()).hexdigest()
    assert key == expected


def test_fallback_hash_is_stable_across_calls():
    payload = _payload()
    key1 = compute_idempotency_key(payload, "t-1", "Guardado", get_settings())
    key2 = compute_idempotency_key(payload, "t-1", "Guardado", get_settings())
    assert key1 == key2


def test_different_events_produce_different_keys():
    payload = _payload()
    key_create = compute_idempotency_key(payload, "t-1", "Creación", get_settings())
    key_save = compute_idempotency_key(payload, "t-1", "Guardado", get_settings())
    assert key_create != key_save


def test_different_tickets_produce_different_keys():
    payload = _payload()
    key1 = compute_idempotency_key(payload, "ticket-A", "Creación", get_settings())
    key2 = compute_idempotency_key(payload, "ticket-B", "Creación", get_settings())
    assert key1 != key2


def test_fallback_handles_missing_date_last_modified():
    # Payload without DateLastModified still produces a stable (non-crashing) key
    payload = {}
    key1 = compute_idempotency_key(payload, "t-1", "Creación", get_settings())
    key2 = compute_idempotency_key(payload, "t-1", "Creación", get_settings())
    assert key1 == key2  # stable even with missing field


# ---------------------------------------------------------------------------
# store_event / duplicate detection
# ---------------------------------------------------------------------------

def test_store_event_returns_true_for_new_event():
    conn = _in_memory_conn()
    stored = store_event(conn, "key-1", "ticket-1", "Creación", _payload())
    assert stored is True


def test_store_event_returns_false_for_duplicate():
    conn = _in_memory_conn()
    store_event(conn, "key-1", "ticket-1", "Creación", _payload())
    stored_again = store_event(conn, "key-1", "ticket-1", "Creación", _payload())
    assert stored_again is False


def test_duplicate_does_not_raise():
    conn = _in_memory_conn()
    store_event(conn, "key-1", "ticket-1", "Creación", _payload())
    # Must not raise — INSERT OR IGNORE silently drops the duplicate
    store_event(conn, "key-1", "ticket-1", "Creación", _payload())


def test_duplicate_leaves_exactly_one_row():
    conn = _in_memory_conn()
    store_event(conn, "key-1", "ticket-1", "Creación", _payload())
    store_event(conn, "key-1", "ticket-1", "Creación", _payload())
    count = conn.execute("SELECT COUNT(*) FROM events WHERE id = 'key-1'").fetchone()[0]
    assert count == 1


def test_different_keys_are_both_stored():
    conn = _in_memory_conn()
    store_event(conn, "key-1", "ticket-1", "Creación", _payload())
    store_event(conn, "key-2", "ticket-1", "Guardado", _payload())
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 2


# ---------------------------------------------------------------------------
# claim_next_event / mark_event_processed
# ---------------------------------------------------------------------------

def test_claim_next_event_returns_none_when_empty():
    conn = _in_memory_conn()
    assert claim_next_event(conn) is None


def test_claim_next_event_returns_oldest_first():
    conn = _in_memory_conn()
    # Insert with explicit received_at to control ordering
    conn.execute(
        "INSERT INTO events (id, ticket_id, event_type, payload, received_at) VALUES (?,?,?,?,?)",
        ("key-newer", "t-1", "Guardado", "{}", "2026-05-25T11:00:00Z"),
    )
    conn.execute(
        "INSERT INTO events (id, ticket_id, event_type, payload, received_at) VALUES (?,?,?,?,?)",
        ("key-older", "t-1", "Creación", "{}", "2026-05-25T09:00:00Z"),
    )
    conn.commit()
    event = claim_next_event(conn)
    assert event is not None
    assert event["id"] == "key-older"


def test_claim_next_event_deserializes_payload():
    conn = _in_memory_conn()
    payload = {"Title": "Test ticket", "DateLastModified": "2026-05-25T10:00:00Z"}
    store_event(conn, "key-1", "ticket-1", "Creación", payload)
    event = claim_next_event(conn)
    assert event is not None
    assert event["payload"]["Title"] == "Test ticket"


def test_claim_next_event_skips_processed():
    conn = _in_memory_conn()
    store_event(conn, "key-1", "ticket-1", "Creación", _payload())
    mark_event_processed(conn, "key-1")
    assert claim_next_event(conn) is None


def test_mark_event_processed_sets_flag():
    conn = _in_memory_conn()
    store_event(conn, "key-1", "ticket-1", "Creación", _payload())
    mark_event_processed(conn, "key-1")
    row = conn.execute("SELECT processed FROM events WHERE id = 'key-1'").fetchone()
    assert row["processed"] == 1


def test_unprocessed_event_remains_after_other_is_processed():
    conn = _in_memory_conn()
    store_event(conn, "key-1", "ticket-1", "Creación", _payload())
    store_event(conn, "key-2", "ticket-2", "Creación", _payload())
    mark_event_processed(conn, "key-1")
    event = claim_next_event(conn)
    assert event is not None
    assert event["id"] == "key-2"


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def test_init_db_creates_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings)
    conn = get_connection(main_db_path(settings))
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"events", "ticket_state", "audit_log", "dlq", "rag_misses"}.issubset(tables)
    conn.close()


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings)
    init_db(settings)  # must not raise


def test_init_db_creates_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "nested" / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    get_settings.cache_clear()
    init_db(get_settings())
    assert data_dir.exists()
