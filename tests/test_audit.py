"""Tests for execution/audit.py — translation and storage behaviour."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from panpilot.config import get_settings
from panpilot.execution.audit import write_audit
from panpilot.intelligence.models import Decision


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    schema = (Path(__file__).parent.parent / "panpilot" / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    return conn


def _decision(**kwargs) -> Decision:
    defaults = dict(
        action="none",
        reasoning="The ticket has no actionable content.",
        none_reason="no_action_warranted",
    )
    return Decision(**{**defaults, **kwargs})


def _mock_anthropic(translated: str) -> MagicMock:
    """Return a fake Anthropic client whose messages.create returns translated."""
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=translated)]
    client.messages.create.return_value = msg
    return client


# ---------------------------------------------------------------------------
# Translation — DB stores Spanish, Decision object stays English
# ---------------------------------------------------------------------------

def test_audit_db_reasoning_is_translated_to_spanish(monkeypatch):
    spanish = "El ticket no tiene contenido procesable."
    monkeypatch.setattr(
        "panpilot.execution.audit._translate_to_spanish",
        lambda text, client: spanish,
    )
    conn = _conn()
    decision = _decision(reasoning="The ticket has no actionable content.")
    write_audit(conn, "TKT-001", decision, dry_run=True, settings=get_settings())
    row = conn.execute("SELECT reasoning FROM audit_log").fetchone()
    assert row["reasoning"] == spanish


def test_decision_reasoning_unchanged_after_audit(monkeypatch):
    monkeypatch.setattr(
        "panpilot.execution.audit._translate_to_spanish",
        lambda text, client: "Texto traducido.",
    )
    conn = _conn()
    decision = _decision(reasoning="The ticket has no actionable content.")
    write_audit(conn, "TKT-001", decision, dry_run=True, settings=get_settings())
    assert decision.reasoning == "The ticket has no actionable content."


def test_translate_called_with_english_reasoning(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "panpilot.execution.audit._translate_to_spanish",
        lambda text, client: calls.append(text) or "Traducido.",
    )
    conn = _conn()
    english = "Ticket stale for 48 hours with no technician reply."
    write_audit(conn, "TKT-001", _decision(reasoning=english), dry_run=True, settings=get_settings())
    assert calls == [english]


def test_anthropic_client_injected_into_translate(monkeypatch):
    """write_audit passes the Anthropic client through to _translate_to_spanish."""
    received: list = []
    monkeypatch.setattr(
        "panpilot.execution.audit._translate_to_spanish",
        lambda text, client: received.append(client) or "ok",
    )
    conn = _conn()
    fake_client = object()
    write_audit(
        conn, "TKT-001", _decision(), dry_run=True,
        settings=get_settings(), anthropic_client=fake_client,
    )
    assert received == [fake_client]


# ---------------------------------------------------------------------------
# Translation failure — fall back to English rather than crashing
# ---------------------------------------------------------------------------

def test_translation_failure_falls_back_to_english(monkeypatch):
    def _raise(text, client):
        raise RuntimeError("API timeout")

    monkeypatch.setattr("panpilot.execution.audit._translate_to_spanish", _raise)
    conn = _conn()
    english = "The ticket has no actionable content."
    write_audit(conn, "TKT-001", _decision(reasoning=english), dry_run=True, settings=get_settings())
    row = conn.execute("SELECT reasoning FROM audit_log").fetchone()
    assert row["reasoning"] == english


def test_translation_failure_still_writes_audit_row(monkeypatch):
    monkeypatch.setattr(
        "panpilot.execution.audit._translate_to_spanish",
        lambda text, client: (_ for _ in ()).throw(RuntimeError("network error")),
    )
    conn = _conn()
    write_audit(conn, "TKT-001", _decision(), dry_run=True, settings=get_settings())
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Other fields are stored correctly alongside translated reasoning
# ---------------------------------------------------------------------------

def test_audit_fields_stored_correctly(monkeypatch):
    monkeypatch.setattr(
        "panpilot.execution.audit._translate_to_spanish",
        lambda text, client: "Texto en español.",
    )
    conn = _conn()
    decision = _decision(action="none", none_reason="needs_human", reasoning="Needs expert.")
    write_audit(conn, "TKT-XYZ", decision, dry_run=False, settings=get_settings())
    row = conn.execute("SELECT * FROM audit_log WHERE ticket_id='TKT-XYZ'").fetchone()
    assert row["action"] == "none"
    assert row["none_reason"] == "needs_human"
    assert row["dry_run"] == 0
    assert row["reasoning"] == "Texto en español."


# ---------------------------------------------------------------------------
# ticket_code stored and displayed
# ---------------------------------------------------------------------------

def test_ticket_code_stored_in_audit_log(monkeypatch):
    monkeypatch.setattr(
        "panpilot.execution.audit._translate_to_spanish",
        lambda text, client: "Traducido.",
    )
    conn = _conn()
    write_audit(
        conn, "TKT-001", _decision(), dry_run=True,
        settings=get_settings(), ticket_code="INC 2026-000042",
    )
    row = conn.execute("SELECT ticket_code FROM audit_log").fetchone()
    assert row["ticket_code"] == "INC 2026-000042"


def test_ticket_code_none_when_not_provided(monkeypatch):
    monkeypatch.setattr(
        "panpilot.execution.audit._translate_to_spanish",
        lambda text, client: "Traducido.",
    )
    conn = _conn()
    write_audit(conn, "TKT-001", _decision(), dry_run=True, settings=get_settings())
    row = conn.execute("SELECT ticket_code FROM audit_log").fetchone()
    assert row["ticket_code"] is None
