"""Tests for T11: clarification cap enforcement."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from panpilot.config import get_settings
from panpilot.intelligence.caps import enforce_clarification_cap
from panpilot.intelligence.models import Decision


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    schema = (Path(__file__).parent.parent / "panpilot" / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    return conn


def _decision(action: str, **kwargs) -> Decision:
    base: dict = {"reasoning": "test"}
    if action in {"auto_respond", "remind"}:
        base["response_draft"] = "resp"
    if action == "none":
        base.setdefault("none_reason", "no_action_warranted")
    return Decision(action=action, **{**base, **kwargs})


def _set_clarification_count(conn: sqlite3.Connection, ticket_id: str, count: int) -> None:
    conn.execute(
        "INSERT INTO ticket_state (ticket_id, state, priority, clarification_count) "
        "VALUES (?, 'CLR_REQ', 'P2', ?) "
        "ON CONFLICT(ticket_id) DO UPDATE SET clarification_count=excluded.clarification_count",
        (ticket_id, count),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Non-clarify actions — always pass through unchanged
# ---------------------------------------------------------------------------

def test_auto_respond_not_affected():
    conn = _conn()
    d = _decision("auto_respond")
    assert enforce_clarification_cap(conn, "TKT-1", d, get_settings()) is d


def test_remind_not_affected():
    conn = _conn()
    d = _decision("remind")
    assert enforce_clarification_cap(conn, "TKT-1", d, get_settings()) is d


def test_alert_not_affected():
    conn = _conn()
    d = _decision("alert")
    assert enforce_clarification_cap(conn, "TKT-1", d, get_settings()) is d


def test_none_not_affected():
    conn = _conn()
    d = _decision("none")
    assert enforce_clarification_cap(conn, "TKT-1", d, get_settings()) is d


# ---------------------------------------------------------------------------
# clarify — under cap → pass through
# ---------------------------------------------------------------------------

def test_clarify_under_cap_is_unchanged():
    conn = _conn()
    _set_clarification_count(conn, "TKT-1", 0)
    d = _decision("clarify")
    result = enforce_clarification_cap(conn, "TKT-1", d, get_settings())
    assert result is d


def test_clarify_at_one_under_cap_is_unchanged(monkeypatch):
    # clarification_max default is 2; count=1 is still under cap
    conn = _conn()
    _set_clarification_count(conn, "TKT-1", 1)
    d = _decision("clarify")
    result = enforce_clarification_cap(conn, "TKT-1", d, get_settings())
    assert result is d


def test_clarify_no_prior_state_is_unchanged():
    # No ticket_state row yet → count=0, under cap
    conn = _conn()
    d = _decision("clarify")
    result = enforce_clarification_cap(conn, "TKT-NEW", d, get_settings())
    assert result is d


# ---------------------------------------------------------------------------
# clarify — at or over cap → escalate to needs_human
# ---------------------------------------------------------------------------

def test_clarify_at_cap_returns_needs_human():
    conn = _conn()
    _set_clarification_count(conn, "TKT-1", 2)  # default cap is 2
    d = _decision("clarify")
    result = enforce_clarification_cap(conn, "TKT-1", d, get_settings())
    assert result.action == "none"
    assert result.none_reason == "needs_human"


def test_clarify_over_cap_returns_needs_human():
    conn = _conn()
    _set_clarification_count(conn, "TKT-1", 5)
    d = _decision("clarify")
    result = enforce_clarification_cap(conn, "TKT-1", d, get_settings())
    assert result.action == "none"
    assert result.none_reason == "needs_human"


def test_cap_result_is_a_new_decision_object():
    conn = _conn()
    _set_clarification_count(conn, "TKT-1", 2)
    d = _decision("clarify")
    result = enforce_clarification_cap(conn, "TKT-1", d, get_settings())
    assert result is not d


def test_cap_result_has_reasoning():
    conn = _conn()
    _set_clarification_count(conn, "TKT-1", 2)
    result = enforce_clarification_cap(conn, "TKT-1", _decision("clarify"), get_settings())
    assert result.reasoning


def test_custom_cap_respected(monkeypatch):
    monkeypatch.setenv("CLARIFICATION_MAX", "1")
    get_settings.cache_clear()
    conn = _conn()
    _set_clarification_count(conn, "TKT-1", 1)  # at custom cap of 1
    result = enforce_clarification_cap(conn, "TKT-1", _decision("clarify"), get_settings())
    assert result.action == "none"
    assert result.none_reason == "needs_human"
