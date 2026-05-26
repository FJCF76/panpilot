"""Tests for POST /webhook intake endpoint."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from panpilot.config import get_settings
from panpilot.intake.router import _conn, _verify_secret, router as intake_router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn_mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    schema = (Path(__file__).parent.parent / "panpilot" / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    return conn


def _client(conn: sqlite3.Connection) -> TestClient:
    """Test client with auth bypassed — for functional (non-auth) tests."""
    app = FastAPI()
    app.include_router(intake_router)

    def _override_conn():
        yield conn

    app.dependency_overrides[_conn] = _override_conn
    app.dependency_overrides[_verify_secret] = lambda: None  # bypass secret check
    return TestClient(app)


def _auth_client(conn: sqlite3.Connection) -> TestClient:
    """Test client with real auth — for auth-specific tests."""
    app = FastAPI()
    app.include_router(intake_router)

    def _override_conn():
        yield conn

    app.dependency_overrides[_conn] = _override_conn
    return TestClient(app)


def _payload(**kwargs) -> dict:
    base = {
        "IncidentId": "TKT-001",
        "Title": "Something broken",
        "DateLastModified": "2026-05-25T09:00:00Z",
    }
    return {**base, **kwargs}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_post_webhook_returns_200():
    conn = _conn_mem()
    resp = _client(conn).post("/webhook", json=_payload())
    assert resp.status_code == 200


def test_post_webhook_stored_true_on_new_event():
    conn = _conn_mem()
    resp = _client(conn).post("/webhook", json=_payload())
    assert resp.json()["stored"] is True


def test_post_webhook_status_ok():
    conn = _conn_mem()
    resp = _client(conn).post("/webhook", json=_payload())
    assert resp.json()["status"] == "ok"


def test_duplicate_delivery_returns_stored_false():
    conn = _conn_mem()
    client = _client(conn)
    client.post("/webhook", json=_payload())
    resp = client.post("/webhook", json=_payload())
    assert resp.json()["stored"] is False


def test_duplicate_delivery_still_200():
    conn = _conn_mem()
    client = _client(conn)
    client.post("/webhook", json=_payload())
    resp = client.post("/webhook", json=_payload())
    assert resp.status_code == 200


def test_event_written_to_db():
    conn = _conn_mem()
    _client(conn).post("/webhook", json=_payload())
    row = conn.execute("SELECT * FROM events WHERE ticket_id='TKT-001'").fetchone()
    assert row is not None


def test_event_ticket_id_stored_correctly():
    conn = _conn_mem()
    _client(conn).post("/webhook", json=_payload(IncidentId="TKT-999"))
    row = conn.execute("SELECT ticket_id FROM events WHERE ticket_id='TKT-999'").fetchone()
    assert row is not None


def test_event_type_defaults_to_guardado():
    conn = _conn_mem()
    _client(conn).post("/webhook", json=_payload())
    row = conn.execute("SELECT event_type FROM events").fetchone()
    assert row["event_type"] == "Guardado"


def test_event_type_overridable_via_query_param():
    conn = _conn_mem()
    _client(conn).post("/webhook?event_type=Actualizado", json=_payload())
    row = conn.execute("SELECT event_type FROM events").fetchone()
    assert row["event_type"] == "Actualizado"


def test_event_marked_unprocessed_on_insert():
    conn = _conn_mem()
    _client(conn).post("/webhook", json=_payload())
    row = conn.execute("SELECT processed FROM events").fetchone()
    assert row["processed"] == 0


# ---------------------------------------------------------------------------
# Ticket ID fallback: Id field
# ---------------------------------------------------------------------------

def test_id_field_used_as_fallback():
    conn = _conn_mem()
    payload = {"Id": "TKT-ALT", "DateLastModified": "2026-05-25T09:00:00Z"}
    resp = _client(conn).post("/webhook", json=payload)
    assert resp.status_code == 200
    row = conn.execute("SELECT ticket_id FROM events").fetchone()
    assert row["ticket_id"] == "TKT-ALT"


def test_incident_id_takes_priority_over_id():
    conn = _conn_mem()
    payload = {"IncidentId": "TKT-PRIMARY", "Id": "TKT-SECONDARY", "DateLastModified": "2026-05-25T09:00:00Z"}
    _client(conn).post("/webhook", json=payload)
    row = conn.execute("SELECT ticket_id FROM events").fetchone()
    assert row["ticket_id"] == "TKT-PRIMARY"


# ---------------------------------------------------------------------------
# Validation — missing ticket ID
# ---------------------------------------------------------------------------

def test_missing_ticket_id_returns_400():
    conn = _conn_mem()
    payload = {"Title": "No ID here", "DateLastModified": "2026-05-25T09:00:00Z"}
    resp = _client(conn).post("/webhook", json=payload)
    assert resp.status_code == 400


def test_missing_ticket_id_stores_nothing():
    conn = _conn_mem()
    payload = {"Title": "No ID here"}
    _client(conn).post("/webhook", json=payload)
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# Auth — webhook_secret
# ---------------------------------------------------------------------------

def test_correct_secret_accepted(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cr3t")
    get_settings.cache_clear()
    conn = _conn_mem()
    resp = _auth_client(conn).post(
        "/webhook",
        json=_payload(),
        headers={"X-Webhook-Secret": "s3cr3t"},
    )
    assert resp.status_code == 200


def test_wrong_secret_rejected(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cr3t")
    get_settings.cache_clear()
    conn = _conn_mem()
    resp = _auth_client(conn).post(
        "/webhook",
        json=_payload(),
        headers={"X-Webhook-Secret": "wrong"},
    )
    assert resp.status_code == 401


def test_missing_secret_header_rejected(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cr3t")
    get_settings.cache_clear()
    conn = _conn_mem()
    resp = _auth_client(conn).post("/webhook", json=_payload())
    assert resp.status_code == 401


def test_no_auth_when_webhook_secret_empty(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    conn = _conn_mem()
    resp = _auth_client(conn).post("/webhook", json=_payload())
    assert resp.status_code == 200


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Loop guard — "En anotación" events must be silently dropped
# ---------------------------------------------------------------------------

def test_en_anotacion_event_returns_200():
    conn = _conn_mem()
    resp = _client(conn).post("/webhook?event_type=En+anotaci%C3%B3n", json=_payload())
    assert resp.status_code == 200


def test_en_anotacion_event_stored_false():
    conn = _conn_mem()
    resp = _client(conn).post("/webhook?event_type=En+anotaci%C3%B3n", json=_payload())
    assert resp.json()["stored"] is False


def test_en_anotacion_event_not_written_to_db():
    conn = _conn_mem()
    _client(conn).post("/webhook?event_type=En+anotaci%C3%B3n", json=_payload())
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 0


def test_en_anotacion_case_insensitive():
    conn = _conn_mem()
    # Capitalised variant — same guard should apply
    resp = _client(conn).post("/webhook?event_type=EN+ANOTACI%C3%93N", json=_payload())
    assert resp.json()["stored"] is False


def test_en_anotacion_without_accent_dropped():
    conn = _conn_mem()
    resp = _client(conn).post("/webhook?event_type=En+anotacion", json=_payload())
    assert resp.json()["stored"] is False


# ---------------------------------------------------------------------------
# Trust boundary — JSON body type check
# ---------------------------------------------------------------------------

def test_json_array_body_returns_422():
    conn = _conn_mem()
    import json as _json
    resp = _client(conn).post(
        "/webhook",
        content=_json.dumps([{"IncidentId": "TKT-001"}]),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_json_array_body_stores_nothing():
    conn = _conn_mem()
    import json as _json
    _client(conn).post(
        "/webhook",
        content=_json.dumps([{"IncidentId": "TKT-001"}]),
        headers={"Content-Type": "application/json"},
    )
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 0
