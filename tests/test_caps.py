"""Tests for T11/T16: per-ticket action cap enforcement."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from panpilot.config import get_settings
from panpilot.intelligence.caps import enforce_clarification_cap, enforce_org_reminder_cap, enforce_reminder_cap
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


# ---------------------------------------------------------------------------
# T16 — enforce_reminder_cap
# ---------------------------------------------------------------------------

def _set_reminder_count(conn: sqlite3.Connection, ticket_id: str, count: int) -> None:
    conn.execute(
        "INSERT INTO ticket_state (ticket_id, state, priority, reminder_count) "
        "VALUES (?, 'WAITING', 'P2', ?) "
        "ON CONFLICT(ticket_id) DO UPDATE SET reminder_count=excluded.reminder_count",
        (ticket_id, count),
    )
    conn.commit()


# Non-remind actions — always pass through unchanged

def test_reminder_cap_clarify_not_affected():
    conn = _conn()
    d = _decision("clarify")
    assert enforce_reminder_cap(conn, "TKT-1", d, get_settings()) is d


def test_reminder_cap_auto_respond_not_affected():
    conn = _conn()
    d = _decision("auto_respond")
    assert enforce_reminder_cap(conn, "TKT-1", d, get_settings()) is d


def test_reminder_cap_alert_not_affected():
    conn = _conn()
    d = _decision("alert")
    assert enforce_reminder_cap(conn, "TKT-1", d, get_settings()) is d


def test_reminder_cap_none_not_affected():
    conn = _conn()
    d = _decision("none")
    assert enforce_reminder_cap(conn, "TKT-1", d, get_settings()) is d


# remind — under cap → pass through

def test_remind_under_cap_is_unchanged():
    conn = _conn()
    _set_reminder_count(conn, "TKT-1", 0)
    d = _decision("remind")
    assert enforce_reminder_cap(conn, "TKT-1", d, get_settings()) is d


def test_remind_at_one_under_cap_is_unchanged(monkeypatch):
    # reminder_max_per_ticket default is 2; count=1 is still under cap
    conn = _conn()
    _set_reminder_count(conn, "TKT-1", 1)
    d = _decision("remind")
    assert enforce_reminder_cap(conn, "TKT-1", d, get_settings()) is d


def test_remind_no_prior_state_is_unchanged():
    conn = _conn()
    d = _decision("remind")
    assert enforce_reminder_cap(conn, "TKT-NEW", d, get_settings()) is d


# remind — at or over cap → escalate to needs_human

def test_remind_at_cap_returns_needs_human():
    conn = _conn()
    _set_reminder_count(conn, "TKT-1", 2)  # default cap is 2
    result = enforce_reminder_cap(conn, "TKT-1", _decision("remind"), get_settings())
    assert result.action == "none"
    assert result.none_reason == "needs_human"


def test_remind_over_cap_returns_needs_human():
    conn = _conn()
    _set_reminder_count(conn, "TKT-1", 5)
    result = enforce_reminder_cap(conn, "TKT-1", _decision("remind"), get_settings())
    assert result.action == "none"
    assert result.none_reason == "needs_human"


def test_reminder_cap_result_is_new_decision_object():
    conn = _conn()
    _set_reminder_count(conn, "TKT-1", 2)
    d = _decision("remind")
    result = enforce_reminder_cap(conn, "TKT-1", d, get_settings())
    assert result is not d


def test_reminder_cap_result_has_reasoning():
    conn = _conn()
    _set_reminder_count(conn, "TKT-1", 2)
    result = enforce_reminder_cap(conn, "TKT-1", _decision("remind"), get_settings())
    assert result.reasoning


def test_reminder_custom_cap_respected(monkeypatch):
    monkeypatch.setenv("REMINDER_MAX_PER_TICKET", "1")
    get_settings.cache_clear()
    conn = _conn()
    _set_reminder_count(conn, "TKT-1", 1)  # at custom cap of 1
    result = enforce_reminder_cap(conn, "TKT-1", _decision("remind"), get_settings())
    assert result.action == "none"
    assert result.none_reason == "needs_human"


# ---------------------------------------------------------------------------
# T17 — enforce_org_reminder_cap
# ---------------------------------------------------------------------------

def _seed_org_reminders(
    conn: sqlite3.Connection,
    requester_id: str,
    ticket_ids: list[str],
    n_reminders_each: int,
    *,
    dry_run: int = 0,
    days_ago: float = 0,
) -> None:
    """Seed ticket_state + audit_log rows so the org cap query has data to count."""
    ts = (
        f"strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-{days_ago} days')"
        if days_ago
        else "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
    )
    for tid in ticket_ids:
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority, requester_id) "
            "VALUES (?, 'WAITING', 'P2', ?) "
            "ON CONFLICT(ticket_id) DO UPDATE SET requester_id=excluded.requester_id",
            (tid, requester_id),
        )
        for _ in range(n_reminders_each):
            conn.execute(
                f"INSERT INTO audit_log (ticket_id, action, reasoning, dry_run, evaluated_at) "
                f"VALUES (?, 'remind', 'test', ?, {ts})",
                (tid, dry_run),
            )
    conn.commit()


# Non-remind actions — always pass through

def test_org_cap_clarify_not_affected():
    conn = _conn()
    d = _decision("clarify")
    assert enforce_org_reminder_cap(conn, "TKT-1", "REQ-1", d, get_settings()) is d


def test_org_cap_auto_respond_not_affected():
    conn = _conn()
    d = _decision("auto_respond")
    assert enforce_org_reminder_cap(conn, "TKT-1", "REQ-1", d, get_settings()) is d


def test_org_cap_none_not_affected():
    conn = _conn()
    d = _decision("none")
    assert enforce_org_reminder_cap(conn, "TKT-1", "REQ-1", d, get_settings()) is d


def test_org_cap_alert_not_affected():
    conn = _conn()
    _seed_org_reminders(conn, "REQ-1", ["TKT-1", "TKT-2", "TKT-3"], 1)
    d = _decision("alert")
    assert enforce_org_reminder_cap(conn, "TKT-NEW", "REQ-1", d, get_settings()) is d


# requester_id is None — skip entirely

def test_org_cap_skipped_when_requester_id_none():
    conn = _conn()
    _seed_org_reminders(conn, "REQ-1", ["TKT-1", "TKT-2"], 5)
    d = _decision("remind")
    assert enforce_org_reminder_cap(conn, "TKT-NEW", None, d, get_settings()) is d


# Under cap — pass through

def test_org_cap_under_cap_passes_through():
    conn = _conn()
    _seed_org_reminders(conn, "REQ-1", ["TKT-1"], 1)  # 1 remind, default cap is 3
    d = _decision("remind")
    result = enforce_org_reminder_cap(conn, "TKT-2", "REQ-1", d, get_settings())
    assert result is d


def test_org_cap_no_prior_reminders_passes_through():
    conn = _conn()
    d = _decision("remind")
    result = enforce_org_reminder_cap(conn, "TKT-1", "REQ-UNKNOWN", d, get_settings())
    assert result is d


# At or over cap — escalate

def test_org_cap_at_cap_returns_needs_human():
    conn = _conn()
    _seed_org_reminders(conn, "REQ-1", ["TKT-1", "TKT-2", "TKT-3"], 1)  # 3 reminders = default cap
    d = _decision("remind")
    result = enforce_org_reminder_cap(conn, "TKT-NEW", "REQ-1", d, get_settings())
    assert result.action == "none"
    assert result.none_reason == "needs_human"


def test_org_cap_over_cap_returns_needs_human():
    conn = _conn()
    _seed_org_reminders(conn, "REQ-1", ["TKT-1"], 5)  # 5 reminders >> cap of 3
    d = _decision("remind")
    result = enforce_org_reminder_cap(conn, "TKT-NEW", "REQ-1", d, get_settings())
    assert result.action == "none"
    assert result.none_reason == "needs_human"


def test_org_cap_result_is_new_decision_object():
    conn = _conn()
    _seed_org_reminders(conn, "REQ-1", ["TKT-1"], 3)
    d = _decision("remind")
    result = enforce_org_reminder_cap(conn, "TKT-NEW", "REQ-1", d, get_settings())
    assert result is not d


def test_org_cap_result_has_reasoning():
    conn = _conn()
    _seed_org_reminders(conn, "REQ-1", ["TKT-1"], 3)
    result = enforce_org_reminder_cap(conn, "TKT-NEW", "REQ-1", _decision("remind"), get_settings())
    assert result.reasoning


# dry_run rows are excluded from count

def test_org_cap_dry_run_rows_not_counted():
    conn = _conn()
    _seed_org_reminders(conn, "REQ-1", ["TKT-1", "TKT-2", "TKT-3"], 1, dry_run=1)
    d = _decision("remind")
    result = enforce_org_reminder_cap(conn, "TKT-NEW", "REQ-1", d, get_settings())
    assert result is d  # dry_run rows don't count toward cap


# Window boundary — reminders older than the window are excluded

def test_org_cap_old_reminders_outside_window_not_counted(monkeypatch):
    monkeypatch.setenv("REMINDER_ORG_WINDOW_DAYS", "3")
    monkeypatch.setenv("REMINDER_ORG_MAX", "2")
    get_settings.cache_clear()
    conn = _conn()
    # 3 reminders but all from 5 days ago — outside the 3-day window
    _seed_org_reminders(conn, "REQ-1", ["TKT-1"], 3, days_ago=5)
    d = _decision("remind")
    result = enforce_org_reminder_cap(conn, "TKT-NEW", "REQ-1", d, get_settings())
    assert result is d


def test_org_cap_reminders_inside_window_count_toward_cap(monkeypatch):
    monkeypatch.setenv("REMINDER_ORG_WINDOW_DAYS", "3")
    monkeypatch.setenv("REMINDER_ORG_MAX", "2")
    get_settings.cache_clear()
    conn = _conn()
    # 2 reminders from 1 day ago — inside the 3-day window → cap reached
    _seed_org_reminders(conn, "REQ-1", ["TKT-1", "TKT-2"], 1, days_ago=1)
    d = _decision("remind")
    result = enforce_org_reminder_cap(conn, "TKT-NEW", "REQ-1", d, get_settings())
    assert result.action == "none"
    assert result.none_reason == "needs_human"


def test_org_cap_custom_cap_respected(monkeypatch):
    monkeypatch.setenv("REMINDER_ORG_MAX", "1")
    get_settings.cache_clear()
    conn = _conn()
    _seed_org_reminders(conn, "REQ-1", ["TKT-1"], 1)  # 1 remind = custom cap of 1
    d = _decision("remind")
    result = enforce_org_reminder_cap(conn, "TKT-NEW", "REQ-1", d, get_settings())
    assert result.action == "none"
    assert result.none_reason == "needs_human"
