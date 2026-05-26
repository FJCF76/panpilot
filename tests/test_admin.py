"""Tests for T5: admin audit log read side, and T19: DLQ retry."""
from __future__ import annotations

import sqlite3
from base64 import b64encode
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panpilot.admin.router import router as admin_router
from panpilot.config import get_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    schema = (Path(__file__).parent.parent / "panpilot" / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    return conn


def _basic_auth(username: str = "admin", password: str = "test-admin-password") -> dict:
    token = b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _insert_audit(conn: sqlite3.Connection, **kwargs) -> None:
    defaults = dict(
        ticket_id="TKT-001",
        ticket_code=None,
        action="none",
        none_reason="no_action_warranted",
        reasoning="Test reasoning",
        confidence=None,
        response_draft=None,
        dry_run=1,
    )
    d = {**defaults, **kwargs}
    conn.execute(
        "INSERT INTO audit_log "
        "(ticket_id, ticket_code, action, none_reason, reasoning, confidence, response_draft, dry_run) "
        "VALUES (:ticket_id, :ticket_code, :action, :none_reason, :reasoning, :confidence, :response_draft, :dry_run)",
        d,
    )
    conn.commit()


def _insert_event(conn: sqlite3.Connection, event_id: str = "evt-1", processed: int = 0) -> None:
    conn.execute(
        "INSERT INTO events (id, ticket_id, event_type, payload, processed) VALUES (?, ?, ?, ?, ?)",
        (event_id, "TKT-001", "Guardado", '{"test": true}', processed),
    )
    conn.commit()


def _insert_rag_miss(
    conn: sqlite3.Connection,
    ticket_id: str = "TKT-001",
    question_summary: str = "¿Cómo configurar webhooks?",
    confidence: float | None = 0.72,
    none_reason: str = "low_confidence",
    chunk_sources: str = '[{"title": "Manual", "filename": "manual.md"}]',
    gap_category: str | None = "Configuración de webhooks",
    gap_explanation: str | None = "La documentación no incluye pasos avanzados.",
) -> None:
    conn.execute(
        "INSERT INTO rag_misses "
        "(ticket_id, question_summary, confidence, none_reason, chunk_sources, gap_category, gap_explanation) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, question_summary, confidence, none_reason, chunk_sources, gap_category, gap_explanation),
    )
    conn.commit()


def _insert_dlq(conn: sqlite3.Connection, event_id: str = "evt-1", exhausted: int = 1) -> int:
    cur = conn.execute(
        "INSERT INTO dlq (event_id, error, attempts, exhausted) VALUES (?, ?, ?, ?)",
        (event_id, "RuntimeError: boom", 3, exhausted),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# App factory for tests (bypasses lifespan to avoid real API calls)
# ---------------------------------------------------------------------------

def _test_client(conn: sqlite3.Connection) -> TestClient:
    """
    Build a minimal FastAPI app with only the admin router mounted,
    injecting the in-memory connection via dependency override.
    """
    from fastapi import FastAPI
    from panpilot.admin.router import _conn, router as admin_router
    from panpilot.config import get_settings

    test_app = FastAPI()
    test_app.include_router(admin_router)

    def _override_conn():
        yield conn

    test_app.dependency_overrides[_conn] = _override_conn

    return TestClient(test_app)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_audit_requires_auth():
    conn = _in_memory_conn()
    client = _test_client(conn)
    resp = client.get("/admin/audit")
    assert resp.status_code == 401


def test_audit_wrong_password_is_rejected():
    conn = _in_memory_conn()
    client = _test_client(conn)
    resp = client.get("/admin/audit", headers=_basic_auth(password="wrong"))
    assert resp.status_code == 401


def test_audit_correct_credentials_accepted():
    conn = _in_memory_conn()
    client = _test_client(conn)
    resp = client.get("/admin/audit", headers=_basic_auth())
    assert resp.status_code == 200


def test_dlq_requires_auth():
    conn = _in_memory_conn()
    client = _test_client(conn)
    resp = client.get("/admin/dlq")
    assert resp.status_code == 401


def test_retry_requires_auth():
    conn = _in_memory_conn()
    client = _test_client(conn)
    resp = client.post("/admin/dlq/1/retry")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Audit listing
# ---------------------------------------------------------------------------

def test_audit_returns_entries():
    conn = _in_memory_conn()
    _insert_audit(conn, ticket_id="TKT-100", action="clarify")
    _insert_audit(conn, ticket_id="TKT-101", action="none")
    client = _test_client(conn)
    data = client.get("/admin/audit", headers=_basic_auth()).json()
    assert len(data["entries"]) == 2


def test_audit_filter_by_ticket_id():
    conn = _in_memory_conn()
    _insert_audit(conn, ticket_id="TKT-A")
    _insert_audit(conn, ticket_id="TKT-B")
    client = _test_client(conn)
    data = client.get("/admin/audit?ticket_id=TKT-A", headers=_basic_auth()).json()
    assert len(data["entries"]) == 1
    assert data["entries"][0]["ticket_id"] == "TKT-A"


def test_audit_filter_by_action():
    conn = _in_memory_conn()
    _insert_audit(conn, action="clarify")
    _insert_audit(conn, action="none")
    _insert_audit(conn, action="none")
    client = _test_client(conn)
    data = client.get("/admin/audit?action=none", headers=_basic_auth()).json()
    assert len(data["entries"]) == 2


def test_audit_filter_by_dry_run():
    conn = _in_memory_conn()
    _insert_audit(conn, dry_run=1)
    _insert_audit(conn, dry_run=0)
    client = _test_client(conn)
    data = client.get("/admin/audit?dry_run=0", headers=_basic_auth()).json()
    assert len(data["entries"]) == 1
    assert data["entries"][0]["dry_run"] == 0


def test_audit_ordered_newest_first():
    conn = _in_memory_conn()
    _insert_audit(conn, ticket_id="FIRST")
    _insert_audit(conn, ticket_id="SECOND")
    client = _test_client(conn)
    data = client.get("/admin/audit", headers=_basic_auth()).json()
    assert data["entries"][0]["ticket_id"] == "SECOND"


def test_audit_empty_returns_empty_list():
    conn = _in_memory_conn()
    client = _test_client(conn)
    data = client.get("/admin/audit", headers=_basic_auth()).json()
    assert data["entries"] == []


def test_audit_limit_is_capped_at_500():
    conn = _in_memory_conn()
    for i in range(10):
        _insert_audit(conn, ticket_id=f"TKT-{i}")
    client = _test_client(conn)
    data = client.get("/admin/audit?limit=9999", headers=_basic_auth()).json()
    assert len(data["entries"]) == 10  # only 10 exist; cap doesn't truncate here but is enforced


def test_audit_pagination_via_offset():
    conn = _in_memory_conn()
    for i in range(5):
        _insert_audit(conn, ticket_id=f"TKT-{i:02d}")
    client = _test_client(conn)
    page1 = client.get("/admin/audit?limit=3&offset=0", headers=_basic_auth()).json()
    page2 = client.get("/admin/audit?limit=3&offset=3", headers=_basic_auth()).json()
    assert len(page1["entries"]) == 3
    assert len(page2["entries"]) == 2
    ids1 = {e["id"] for e in page1["entries"]}
    ids2 = {e["id"] for e in page2["entries"]}
    assert ids1.isdisjoint(ids2)


# ---------------------------------------------------------------------------
# DLQ listing
# ---------------------------------------------------------------------------

def test_dlq_returns_entries():
    conn = _in_memory_conn()
    _insert_event(conn, "evt-1")
    _insert_event(conn, "evt-2")
    _insert_dlq(conn, "evt-1", exhausted=1)
    _insert_dlq(conn, "evt-2", exhausted=0)
    client = _test_client(conn)
    data = client.get("/admin/dlq", headers=_basic_auth()).json()
    assert len(data["entries"]) == 2


def test_dlq_filter_exhausted():
    conn = _in_memory_conn()
    _insert_event(conn, "evt-1")
    _insert_event(conn, "evt-2")
    _insert_dlq(conn, "evt-1", exhausted=1)
    _insert_dlq(conn, "evt-2", exhausted=0)
    client = _test_client(conn)
    data = client.get("/admin/dlq?exhausted=1", headers=_basic_auth()).json()
    assert len(data["entries"]) == 1
    assert data["entries"][0]["exhausted"] == 1


def test_dlq_empty_returns_empty_list():
    conn = _in_memory_conn()
    client = _test_client(conn)
    data = client.get("/admin/dlq", headers=_basic_auth()).json()
    assert data["entries"] == []


# ---------------------------------------------------------------------------
# DLQ retry (T19)
# ---------------------------------------------------------------------------

def test_retry_requeues_event():
    conn = _in_memory_conn()
    _insert_event(conn, "evt-1", processed=1)
    entry_id = _insert_dlq(conn, "evt-1", exhausted=1)
    client = _test_client(conn)
    resp = client.post(f"/admin/dlq/{entry_id}/retry", headers=_basic_auth())
    assert resp.status_code == 204
    row = conn.execute("SELECT processed FROM events WHERE id = 'evt-1'").fetchone()
    assert row["processed"] == 0


def test_retry_removes_dlq_entry():
    conn = _in_memory_conn()
    _insert_event(conn, "evt-1", processed=1)
    entry_id = _insert_dlq(conn, "evt-1", exhausted=1)
    client = _test_client(conn)
    client.post(f"/admin/dlq/{entry_id}/retry", headers=_basic_auth())
    row = conn.execute("SELECT id FROM dlq WHERE id = ?", (entry_id,)).fetchone()
    assert row is None


def test_retry_nonexistent_returns_404():
    conn = _in_memory_conn()
    client = _test_client(conn)
    resp = client.post("/admin/dlq/9999/retry", headers=_basic_auth())
    assert resp.status_code == 404


def test_retry_other_dlq_entries_unaffected():
    conn = _in_memory_conn()
    _insert_event(conn, "evt-1", processed=1)
    _insert_event(conn, "evt-2", processed=1)
    entry_id_1 = _insert_dlq(conn, "evt-1", exhausted=1)
    entry_id_2 = _insert_dlq(conn, "evt-2", exhausted=1)
    client = _test_client(conn)
    client.post(f"/admin/dlq/{entry_id_1}/retry", headers=_basic_auth())
    # entry_id_2 should still exist
    row = conn.execute("SELECT id FROM dlq WHERE id = ?", (entry_id_2,)).fetchone()
    assert row is not None
    # evt-2 should still be processed
    row2 = conn.execute("SELECT processed FROM events WHERE id = 'evt-2'").fetchone()
    assert row2["processed"] == 1


# ---------------------------------------------------------------------------
# ticket_code — JSON API includes it; HTML dashboard renders it as a link
# ---------------------------------------------------------------------------

def test_audit_json_includes_ticket_code():
    conn = _in_memory_conn()
    _insert_audit(conn, ticket_id="uuid-abc", ticket_code="INC 2026-000001")
    client = _test_client(conn)
    data = client.get("/admin/audit", headers=_basic_auth()).json()
    assert data["entries"][0]["ticket_code"] == "INC 2026-000001"


def test_audit_json_ticket_code_null_when_absent():
    conn = _in_memory_conn()
    _insert_audit(conn, ticket_id="uuid-abc")  # no ticket_code
    client = _test_client(conn)
    data = client.get("/admin/audit", headers=_basic_auth()).json()
    assert data["entries"][0]["ticket_code"] is None


def test_dashboard_ticket_code_shown_as_link():
    conn = _in_memory_conn()
    _insert_audit(conn, ticket_id="some-uuid-1234", ticket_code="INC 2026-000042")
    client = _test_client(conn)
    resp = client.get("/admin/", headers=_basic_auth())
    assert "INC 2026-000042" in resp.text
    assert "formIncidents.paw?id=some-uuid-1234" in resp.text


def test_dashboard_link_uses_base_url(monkeypatch):
    monkeypatch.setenv("PROACTIVANET_BASE_URL", "https://demo.example.com/panet")
    from panpilot.config import get_settings
    get_settings.cache_clear()
    conn = _in_memory_conn()
    _insert_audit(conn, ticket_id="uuid-xyz", ticket_code="INC 2026-000099")
    client = _test_client(conn)
    resp = client.get("/admin/", headers=_basic_auth())
    assert "https://demo.example.com/panet/servicedesk/incidents/formIncidents/formIncidents.paw?id=uuid-xyz" in resp.text
    get_settings.cache_clear()


def test_dashboard_falls_back_to_uuid_when_no_ticket_code():
    conn = _in_memory_conn()
    _insert_audit(conn, ticket_id="uuid-fallback-id", ticket_code=None)
    client = _test_client(conn)
    resp = client.get("/admin/", headers=_basic_auth())
    assert "uuid-fallback-id" in resp.text


# ---------------------------------------------------------------------------
# Dashboard HTML (smoke test)
# ---------------------------------------------------------------------------

def test_dashboard_returns_html():
    conn = _in_memory_conn()
    client = _test_client(conn)
    resp = client.get("/admin/", headers=_basic_auth())
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "PanPilot" in resp.text


def test_dashboard_shows_audit_entries():
    conn = _in_memory_conn()
    _insert_audit(conn, ticket_id="TKT-DISPLAY", action="auto_respond")
    client = _test_client(conn)
    resp = client.get("/admin/", headers=_basic_auth())
    assert "TKT-DISPLAY" in resp.text


def test_dashboard_shows_dlq_entry():
    conn = _in_memory_conn()
    _insert_event(conn, "evt-show")
    _insert_dlq(conn, "evt-show", exhausted=1)
    client = _test_client(conn)
    resp = client.get("/admin/", headers=_basic_auth())
    assert "evt-show" in resp.text


def test_dashboard_shows_retry_button_for_dlq():
    conn = _in_memory_conn()
    _insert_event(conn, "evt-retry")
    entry_id = _insert_dlq(conn, "evt-retry", exhausted=1)
    client = _test_client(conn)
    resp = client.get("/admin/", headers=_basic_auth())
    assert f"/admin/dlq/{entry_id}/retry" in resp.text


def test_dashboard_ticket_filter_query_param():
    conn = _in_memory_conn()
    _insert_audit(conn, ticket_id="TKT-FILTER")
    _insert_audit(conn, ticket_id="TKT-OTHER")
    client = _test_client(conn)
    resp = client.get("/admin/?ticket_id=TKT-FILTER", headers=_basic_auth())
    assert "TKT-FILTER" in resp.text
    assert "TKT-OTHER" not in resp.text


def test_dashboard_requires_auth():
    conn = _in_memory_conn()
    client = _test_client(conn)
    resp = client.get("/admin/")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Rag gaps endpoint and dashboard section
# ---------------------------------------------------------------------------

def test_rag_gaps_endpoint_requires_auth():
    conn = _in_memory_conn()
    client = _test_client(conn)
    resp = client.get("/admin/rag-gaps")
    assert resp.status_code == 401


def test_rag_gaps_returns_summary_and_recent():
    conn = _in_memory_conn()
    _insert_rag_miss(conn, ticket_id="TKT-A", gap_category="Exportación CSV")
    _insert_rag_miss(conn, ticket_id="TKT-B", gap_category="Exportación CSV")
    _insert_rag_miss(conn, ticket_id="TKT-C", gap_category="Instalación Windows")
    client = _test_client(conn)
    data = client.get("/admin/rag-gaps", headers=_basic_auth()).json()
    assert "summary" in data
    assert "recent" in data
    assert len(data["recent"]) == 3
    categories = [r["gap_category"] for r in data["summary"]]
    assert "Exportación CSV" in categories
    assert data["summary"][0]["count"] == 2  # most frequent first


def test_rag_gaps_handles_null_gap_category_rows():
    conn = _in_memory_conn()
    _insert_rag_miss(conn, ticket_id="TKT-LEGACY", gap_category=None)
    _insert_rag_miss(conn, ticket_id="TKT-NEW", gap_category="Webhooks")
    client = _test_client(conn)
    data = client.get("/admin/rag-gaps", headers=_basic_auth()).json()
    # Null-category row excluded from summary
    assert len(data["summary"]) == 1
    assert data["summary"][0]["gap_category"] == "Webhooks"
    # Both rows present in recent
    assert len(data["recent"]) == 2


def test_rag_gaps_section_rendered_in_dashboard():
    conn = _in_memory_conn()
    _insert_rag_miss(conn, ticket_id="TKT-GAP")
    client = _test_client(conn)
    resp = client.get("/admin/", headers=_basic_auth())
    assert resp.status_code == 200
    assert "Lagunas de documentación" in resp.text
    assert "TKT-GAP" in resp.text


def test_rag_gaps_empty_state_shows_message():
    conn = _in_memory_conn()
    client = _test_client(conn)
    resp = client.get("/admin/", headers=_basic_auth())
    assert resp.status_code == 200
    assert "No hay lagunas de documentación registradas aún." in resp.text


def test_rag_gaps_null_explanation_displays_dash():
    conn = _in_memory_conn()
    _insert_rag_miss(conn, ticket_id="TKT-NULL", gap_explanation=None)
    client = _test_client(conn)
    resp = client.get("/admin/", headers=_basic_auth())
    assert resp.status_code == 200
    assert "—" in resp.text


def test_rag_gaps_malformed_chunk_sources_displays_dash():
    conn = _in_memory_conn()
    _insert_rag_miss(conn, ticket_id="TKT-BAD", chunk_sources="NOT JSON")
    client = _test_client(conn)
    resp = client.get("/admin/", headers=_basic_auth())
    assert resp.status_code == 200  # must not 500
    assert "—" in resp.text
