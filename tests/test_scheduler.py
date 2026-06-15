"""Tests for T6: APScheduler stale ticket detector."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apscheduler.schedulers.background import BackgroundScheduler

from panpilot.config import get_settings
from panpilot.execution.proactivanet import ProactivanetClient
from panpilot.intake.scheduler import (
    _DEFAULT_PRIORITY,
    _SKIP_STATES,
    _threshold_for,
    build_scheduler,
    detect_stale_tickets,
    send_proactive_reminders,
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


def _past_iso(hours: float) -> str:
    """Return an ISO timestamp `hours` hours in the past."""
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _insert_ticket_state(
    conn: sqlite3.Connection,
    ticket_id: str,
    state: str = "WAITING",
    priority: str | None = "P2",
    hours_old: float = 0.0,
) -> None:
    updated_at = _past_iso(hours_old)
    conn.execute(
        "INSERT INTO ticket_state (ticket_id, state, priority, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (ticket_id, state, priority, updated_at),
    )
    conn.commit()


def _insert_audit_alert(
    conn: sqlite3.Connection,
    ticket_id: str,
    hours_ago: float = 0.0,
) -> None:
    evaluated_at = _past_iso(hours_ago)
    conn.execute(
        "INSERT INTO audit_log (ticket_id, action, reasoning, dry_run, evaluated_at) "
        "VALUES (?, 'alert', 'test', 1, ?)",
        (ticket_id, evaluated_at),
    )
    conn.commit()


_ACTION_TYPE_MAP = {
    "Annotation": "uuid-annotation",
    "UserTextQuestion": "uuid-clarify",
    "AutomaticResponse": "uuid-auto",
    "PublishedAction": "uuid-remind",
}

# Default Proactivanet response used when existing tests don't care about
# verification: ticket exists, active, no DateLastModified clock refresh.
_DEFAULT_PN_RESPONSE = {"Status": "Open", "DateLastModified": None}


def _run(conn: sqlite3.Connection) -> int:
    """Run detect_stale_tickets with route() and ProactivanetClient patched."""
    with patch("panpilot.intake.scheduler.route") as mock_route, \
         patch("panpilot.intake.scheduler.ProactivanetClient") as mock_pn_cls:
        mock_route.return_value = None
        mock_instance = MagicMock()
        mock_instance.get_ticket.return_value = _DEFAULT_PN_RESPONSE
        mock_pn_cls.return_value = mock_instance
        return detect_stale_tickets(conn, get_settings(), _ACTION_TYPE_MAP)


def _run_and_capture(conn: sqlite3.Connection) -> tuple[int, list]:
    """Run detect_stale_tickets, returning (count, list of call_args)."""
    calls: list = []

    def _capture(*args, **kwargs):
        calls.append(args)

    with patch("panpilot.intake.scheduler.route", side_effect=_capture), \
         patch("panpilot.intake.scheduler.ProactivanetClient") as mock_pn_cls:
        mock_instance = MagicMock()
        mock_instance.get_ticket.return_value = _DEFAULT_PN_RESPONSE
        mock_pn_cls.return_value = mock_instance
        count = detect_stale_tickets(conn, get_settings(), _ACTION_TYPE_MAP)
    return count, calls


# ---------------------------------------------------------------------------
# _threshold_for
# ---------------------------------------------------------------------------

def test_threshold_p1():
    assert _threshold_for("P1", get_settings()) == timedelta(hours=4)


def test_threshold_p2():
    assert _threshold_for("P2", get_settings()) == timedelta(hours=24)


def test_threshold_p3():
    assert _threshold_for("P3", get_settings()) == timedelta(hours=120)


def test_threshold_none_defaults_to_p2():
    assert _threshold_for(None, get_settings()) == timedelta(hours=24)


def test_threshold_unknown_defaults_to_p3_hours():
    # Unknown string falls through to else branch (P3 hours)
    assert _threshold_for("P9", get_settings()) == timedelta(hours=120)


# ---------------------------------------------------------------------------
# No tickets — no alerts
# ---------------------------------------------------------------------------

def test_empty_db_returns_zero():
    conn = _conn()
    assert _run(conn) == 0


# ---------------------------------------------------------------------------
# Freshness: ticket not yet stale → no alert
# ---------------------------------------------------------------------------

def test_fresh_p1_ticket_not_alerted():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P1", hours_old=3.9)
    assert _run(conn) == 0


def test_fresh_p2_ticket_not_alerted():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-2", priority="P2", hours_old=23.9)
    assert _run(conn) == 0


def test_fresh_p3_ticket_not_alerted():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-3", priority="P3", hours_old=119.9)
    assert _run(conn) == 0


# ---------------------------------------------------------------------------
# Staleness: ticket past threshold → alert sent
# ---------------------------------------------------------------------------

def test_stale_p1_ticket_is_alerted():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P1", hours_old=4.1)
    assert _run(conn) == 1


def test_stale_p2_ticket_is_alerted():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-2", priority="P2", hours_old=25.0)
    assert _run(conn) == 1


def test_stale_p3_ticket_is_alerted():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-3", priority="P3", hours_old=121.0)
    assert _run(conn) == 1


def test_p2_stale_threshold_not_triggered_at_p1_age():
    # A P2 ticket that's 5h old is NOT stale (P2 threshold is 24h).
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P2", hours_old=5.0)
    assert _run(conn) == 0


def test_null_priority_uses_p2_threshold():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority=None, hours_old=25.0)
    assert _run(conn) == 1


def test_multiple_stale_tickets_all_alerted():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P1", hours_old=5.0)
    _insert_ticket_state(conn, "TKT-2", priority="P2", hours_old=25.0)
    assert _run(conn) == 2


# ---------------------------------------------------------------------------
# Skip states
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", sorted(_SKIP_STATES))
def test_skip_state_not_alerted(state: str):
    conn = _conn()
    _insert_ticket_state(conn, "TKT-SKIP", state=state, priority="P1", hours_old=100.0)
    assert _run(conn) == 0


def test_waiting_state_is_alerted_when_stale():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-W", state="WAITING", priority="P2", hours_old=25.0)
    assert _run(conn) == 1


def test_clr_req_state_is_alerted_when_stale():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-C", state="CLR_REQ", priority="P2", hours_old=25.0)
    assert _run(conn) == 1


def test_pending_agent_action_is_alerted_when_stale():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-P", state="PENDING_AGENT_ACTION", priority="P2", hours_old=25.0)
    assert _run(conn) == 1


# ---------------------------------------------------------------------------
# Repeat-alert suppression via audit_log
# ---------------------------------------------------------------------------

def test_recent_alert_prevents_repeat():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P2", hours_old=25.0)
    _insert_audit_alert(conn, "TKT-1", hours_ago=1.0)  # alerted 1h ago, threshold 24h
    assert _run(conn) == 0


def test_old_alert_allows_repeat():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P2", hours_old=50.0)
    _insert_audit_alert(conn, "TKT-1", hours_ago=25.0)  # alerted 25h ago, threshold 24h
    assert _run(conn) == 1


def test_non_alert_audit_entry_does_not_suppress():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P2", hours_old=25.0)
    # A clarify action in audit, not an alert
    conn.execute(
        "INSERT INTO audit_log (ticket_id, action, reasoning, dry_run) "
        "VALUES ('TKT-1', 'clarify', 'test', 1)"
    )
    conn.commit()
    assert _run(conn) == 1


def test_p1_recent_alert_uses_p1_threshold_window():
    # P1 threshold is 4h. Alert 3h ago → should suppress (3h < 4h).
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P1", hours_old=5.0)
    _insert_audit_alert(conn, "TKT-1", hours_ago=3.0)
    assert _run(conn) == 0


def test_p1_old_alert_allows_repeat_with_p1_threshold():
    # P1 threshold is 4h. Alert 5h ago → should allow repeat (5h > 4h).
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P1", hours_old=10.0)
    _insert_audit_alert(conn, "TKT-1", hours_ago=5.0)
    assert _run(conn) == 1


# ---------------------------------------------------------------------------
# Decision content
# ---------------------------------------------------------------------------

def test_alert_decision_action_is_alert():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P2", hours_old=25.0)
    _, calls = _run_and_capture(conn)
    assert len(calls) == 1
    decision = calls[0][0]  # first positional arg to route()
    assert decision.action == "alert"


def test_alert_reasoning_mentions_ticket_inactive():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P2", hours_old=25.0)
    _, calls = _run_and_capture(conn)
    reasoning = calls[0][0].reasoning
    assert "actividad" in reasoning


def test_alert_context_has_correct_ticket_id():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-ABC", priority="P2", hours_old=25.0)
    _, calls = _run_and_capture(conn)
    ctx = calls[0][1]  # second positional arg = TicketContext
    assert ctx.ticket_id == "TKT-ABC"


def test_alert_context_has_correct_priority():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P1", hours_old=5.0)
    _, calls = _run_and_capture(conn)
    ctx = calls[0][1]
    assert ctx.priority == "P1"


# ---------------------------------------------------------------------------
# State machine transition after stale alert
# ---------------------------------------------------------------------------

def test_stale_alert_transitions_state_to_stale_alert():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P2", hours_old=25.0)
    _run(conn)
    row = conn.execute(
        "SELECT state FROM ticket_state WHERE ticket_id = ?", ("TKT-1",)
    ).fetchone()
    assert row["state"] == "STALE_ALERT"


def test_stale_alert_state_prevents_repeat_alert():
    # After a stale alert fires and transitions state to STALE_ALERT,
    # the next detector run should skip the ticket (STALE_ALERT is in _SKIP_STATES).
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P2", hours_old=25.0)
    first_run = _run(conn)
    assert first_run == 1
    second_run = _run(conn)
    assert second_run == 0  # STALE_ALERT state suppresses re-detection


def test_route_error_does_not_transition_state():
    # If route() raises, apply_transition() must not be called.
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P2", hours_old=25.0)
    with patch("panpilot.intake.scheduler.route", side_effect=RuntimeError("network error")), \
         patch("panpilot.intake.scheduler.ProactivanetClient") as mock_pn_cls:
        mock_pn_cls.return_value.get_ticket.return_value = _DEFAULT_PN_RESPONSE
        detect_stale_tickets(conn, get_settings(), _ACTION_TYPE_MAP)
    row = conn.execute(
        "SELECT state FROM ticket_state WHERE ticket_id = ?", ("TKT-1",)
    ).fetchone()
    assert row["state"] == "WAITING"  # state unchanged after failed route


# ---------------------------------------------------------------------------
# Route error is caught (does not propagate to caller)
# ---------------------------------------------------------------------------

def test_route_error_does_not_propagate():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-1", priority="P2", hours_old=25.0)
    with patch("panpilot.intake.scheduler.route", side_effect=RuntimeError("network error")), \
         patch("panpilot.intake.scheduler.ProactivanetClient") as mock_pn_cls:
        mock_pn_cls.return_value.get_ticket.return_value = _DEFAULT_PN_RESPONSE
        count = detect_stale_tickets(conn, get_settings(), _ACTION_TYPE_MAP)
    assert count == 0  # error swallowed, not propagated


def test_route_error_on_one_ticket_does_not_stop_others():
    conn = _conn()
    _insert_ticket_state(conn, "TKT-FAIL", priority="P1", hours_old=5.0)
    _insert_ticket_state(conn, "TKT-OK", priority="P1", hours_old=5.0)
    call_count = [0]

    def _sometimes_fail(decision, ctx, *args, **kwargs):
        call_count[0] += 1
        if ctx.ticket_id == "TKT-FAIL":
            raise RuntimeError("boom")

    with patch("panpilot.intake.scheduler.route", side_effect=_sometimes_fail), \
         patch("panpilot.intake.scheduler.ProactivanetClient") as mock_pn_cls:
        mock_pn_cls.return_value.get_ticket.return_value = _DEFAULT_PN_RESPONSE
        count = detect_stale_tickets(conn, get_settings(), _ACTION_TYPE_MAP)

    assert call_count[0] == 2   # both tickets were attempted
    assert count == 1           # only one succeeded


# ---------------------------------------------------------------------------
# Scheduler lifecycle (build_scheduler)
# ---------------------------------------------------------------------------

def test_build_scheduler_returns_background_scheduler(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    scheduler = build_scheduler(get_settings(), _ACTION_TYPE_MAP)
    assert isinstance(scheduler, BackgroundScheduler)


def test_scheduler_has_stale_detector_job(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    scheduler = build_scheduler(get_settings(), _ACTION_TYPE_MAP)
    job_ids = [j.id for j in scheduler.get_jobs()]
    assert "stale_detector" in job_ids


def test_scheduler_starts_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    scheduler = build_scheduler(get_settings(), _ACTION_TYPE_MAP)
    scheduler.start()
    assert scheduler.running
    scheduler.shutdown(wait=False)
    assert not scheduler.running


def test_build_scheduler_registers_reminder_job(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    scheduler = build_scheduler(get_settings(), _ACTION_TYPE_MAP)
    job_ids = [j.id for j in scheduler.get_jobs()]
    assert "reminder_scheduler" in job_ids


# ---------------------------------------------------------------------------
# H18 Gap 2 — send_proactive_reminders
# ---------------------------------------------------------------------------

def _insert_waiting(
    conn: sqlite3.Connection,
    ticket_id: str,
    hours_old: float,
    priority: str = "P2",
    reminder_count: int = 0,
    requester_id: str | None = None,
) -> None:
    updated_at = _past_iso(hours_old)
    conn.execute(
        "INSERT INTO ticket_state "
        "(ticket_id, state, priority, updated_at, reminder_count, requester_id) "
        "VALUES (?, 'WAITING', ?, ?, ?, ?)",
        (ticket_id, priority, updated_at, reminder_count, requester_id),
    )
    conn.commit()


def _insert_audit_remind(
    conn: sqlite3.Connection,
    ticket_id: str,
    hours_ago: float,
    dry_run: int = 1,
) -> None:
    evaluated_at = _past_iso(hours_ago)
    conn.execute(
        "INSERT INTO audit_log (ticket_id, action, reasoning, dry_run, evaluated_at) "
        "VALUES (?, 'remind', 'test', ?, ?)",
        (ticket_id, dry_run, evaluated_at),
    )
    conn.commit()


def _run_reminders(conn: sqlite3.Connection) -> int:
    with patch("panpilot.intake.scheduler.route") as mock_route, \
         patch("panpilot.intake.scheduler.ProactivanetClient") as mock_pn_cls:
        mock_route.return_value = None
        mock_instance = MagicMock()
        mock_instance.get_ticket.return_value = _DEFAULT_PN_RESPONSE
        mock_pn_cls.return_value = mock_instance
        return send_proactive_reminders(conn, get_settings(), _ACTION_TYPE_MAP)


def test_reminder_fresh_waiting_below_threshold_no_send():
    conn = _conn()
    _insert_waiting(conn, "TKT-R1", hours_old=12.0)  # threshold is 24h by default
    assert _run_reminders(conn) == 0


def test_reminder_waiting_past_threshold_sends():
    conn = _conn()
    _insert_waiting(conn, "TKT-R1", hours_old=25.0)
    assert _run_reminders(conn) == 1


def test_reminder_multiple_waiting_all_reminded():
    conn = _conn()
    _insert_waiting(conn, "TKT-R1", hours_old=25.0)
    _insert_waiting(conn, "TKT-R2", hours_old=30.0)
    assert _run_reminders(conn) == 2


def test_reminder_recent_audit_log_suppresses_repeat():
    conn = _conn()
    _insert_waiting(conn, "TKT-R1", hours_old=30.0)
    _insert_audit_remind(conn, "TKT-R1", hours_ago=5.0)  # reminded 5h ago, threshold 24h
    assert _run_reminders(conn) == 0


def test_reminder_old_audit_log_allows_repeat():
    conn = _conn()
    _insert_waiting(conn, "TKT-R1", hours_old=30.0)
    _insert_audit_remind(conn, "TKT-R1", hours_ago=25.0)  # reminded 25h ago — past threshold
    assert _run_reminders(conn) == 1


def test_reminder_non_waiting_state_not_reminded():
    conn = _conn()
    conn.execute(
        "INSERT INTO ticket_state (ticket_id, state, priority, updated_at) "
        "VALUES ('TKT-R1', 'CLR_REQ', 'P2', ?)",
        (_past_iso(30.0),),
    )
    conn.commit()
    assert _run_reminders(conn) == 0


def test_reminder_per_ticket_cap_escalates():
    conn = _conn()
    _insert_waiting(conn, "TKT-R1", hours_old=25.0, reminder_count=2)  # default cap is 2
    calls = []

    def _capture(*args, **kwargs):
        calls.append(args[0])  # first arg is the Decision

    with patch("panpilot.intake.scheduler.route", side_effect=_capture), \
         patch("panpilot.intake.scheduler.ProactivanetClient") as mock_pn_cls:
        mock_pn_cls.return_value.get_ticket.return_value = _DEFAULT_PN_RESPONSE
        with patch("panpilot.intake.scheduler.apply_transition"):
            send_proactive_reminders(conn, get_settings(), _ACTION_TYPE_MAP)

    assert len(calls) == 1
    assert calls[0].action == "none"
    assert calls[0].none_reason == "needs_human"


def test_reminder_decision_action_is_remind():
    conn = _conn()
    _insert_waiting(conn, "TKT-R1", hours_old=25.0)
    calls = []

    def _capture(*args, **kwargs):
        calls.append(args[0])

    with patch("panpilot.intake.scheduler.route", side_effect=_capture), \
         patch("panpilot.intake.scheduler.ProactivanetClient") as mock_pn_cls:
        mock_pn_cls.return_value.get_ticket.return_value = _DEFAULT_PN_RESPONSE
        with patch("panpilot.intake.scheduler.apply_transition"):
            send_proactive_reminders(conn, get_settings(), _ACTION_TYPE_MAP)

    assert len(calls) == 1
    assert calls[0].action == "remind"


def test_reminder_response_draft_is_spanish():
    conn = _conn()
    _insert_waiting(conn, "TKT-R1", hours_old=25.0)
    calls = []

    def _capture(*args, **kwargs):
        calls.append(args[0])

    with patch("panpilot.intake.scheduler.route", side_effect=_capture), \
         patch("panpilot.intake.scheduler.ProactivanetClient") as mock_pn_cls:
        mock_pn_cls.return_value.get_ticket.return_value = _DEFAULT_PN_RESPONSE
        with patch("panpilot.intake.scheduler.apply_transition"):
            send_proactive_reminders(conn, get_settings(), _ACTION_TYPE_MAP)

    draft = calls[0].response_draft or ""
    assert "Estimado cliente" in draft


def test_reminder_routing_failure_does_not_crash_loop():
    """An exception in route() for one ticket does not prevent others from being reminded."""
    conn = _conn()
    _insert_waiting(conn, "TKT-R1", hours_old=25.0)
    _insert_waiting(conn, "TKT-R2", hours_old=25.0)
    call_count = 0

    def _fail_first(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated API failure")

    with patch("panpilot.intake.scheduler.route", side_effect=_fail_first), \
         patch("panpilot.intake.scheduler.ProactivanetClient") as mock_pn_cls:
        mock_pn_cls.return_value.get_ticket.return_value = _DEFAULT_PN_RESPONSE
        with patch("panpilot.intake.scheduler.apply_transition"):
            count = send_proactive_reminders(conn, get_settings(), _ACTION_TYPE_MAP)

    # One failed, one succeeded
    assert count == 1


# ---------------------------------------------------------------------------
# Pre-verify: Proactivanet as source of truth
# ---------------------------------------------------------------------------

_TERMINAL_NAMES = frozenset({"closed", "resolved", "cancelled", "rejected"})


def _mock_pn(get_ticket_return) -> MagicMock:
    """Return a ProactivanetClient mock with get_ticket() preset."""
    m = MagicMock(spec=ProactivanetClient)
    m.get_ticket.return_value = get_ticket_return
    return m


# --- detect_stale_tickets ---

def test_stale_deleted_ticket_marked_closed_externally():
    """404 from Proactivanet → CLOSED_EXTERNALLY, no route() call."""
    conn = _conn()
    _insert_ticket_state(conn, "T1", state="CLR_REQ", priority="P2", hours_old=30)
    pn = _mock_pn(None)  # None = 404
    with patch("panpilot.intake.scheduler.route") as mock_route:
        count = detect_stale_tickets(conn, get_settings(), _ACTION_TYPE_MAP,
                                     _TERMINAL_NAMES, proactivanet_client=pn)
    assert count == 0
    assert mock_route.call_count == 0
    row = conn.execute(
        "SELECT state FROM ticket_state WHERE ticket_id='T1'").fetchone()
    assert row["state"] == "CLOSED_EXTERNALLY"


def test_stale_terminal_ticket_marked_closed_externally():
    """Terminal Proactivanet status → CLOSED_EXTERNALLY, no route() call."""
    conn = _conn()
    _insert_ticket_state(conn, "T2", state="CLR_REQ", priority="P2", hours_old=30)
    pn = _mock_pn({"Status": "Closed", "DateLastModified": None})
    with patch("panpilot.intake.scheduler.route") as mock_route:
        count = detect_stale_tickets(conn, get_settings(), _ACTION_TYPE_MAP,
                                     _TERMINAL_NAMES, proactivanet_client=pn)
    assert count == 0
    assert mock_route.call_count == 0
    row = conn.execute(
        "SELECT state FROM ticket_state WHERE ticket_id='T2'").fetchone()
    assert row["state"] == "CLOSED_EXTERNALLY"


def test_stale_clock_drift_refreshes_timestamp_and_suppresses_alert():
    """Proactivanet DateLastModified more recent → updated_at refreshed, alert suppressed."""
    conn = _conn()
    _insert_ticket_state(conn, "T3", state="CLR_REQ", priority="P2", hours_old=30)
    recent_iso = _past_iso(1.0)  # modified 1 hour ago — well within P2 threshold (24h)
    pn = _mock_pn({"Status": "Open", "DateLastModified": recent_iso})
    with patch("panpilot.intake.scheduler.route") as mock_route:
        count = detect_stale_tickets(conn, get_settings(), _ACTION_TYPE_MAP,
                                     _TERMINAL_NAMES, proactivanet_client=pn)
    assert count == 0
    assert mock_route.call_count == 0


def test_stale_active_ticket_routes_alert():
    """Active ticket past threshold → alert fires as normal."""
    conn = _conn()
    _insert_ticket_state(conn, "T4", state="CLR_REQ", priority="P2", hours_old=30)
    past_iso = _past_iso(28.0)  # modified 28h ago — still stale (P2 threshold = 24h)
    pn = _mock_pn({"Status": "Open", "DateLastModified": past_iso})
    with patch("panpilot.intake.scheduler.route"):
        count = detect_stale_tickets(conn, get_settings(), _ACTION_TYPE_MAP,
                                     _TERMINAL_NAMES, proactivanet_client=pn)
    assert count == 1


# --- send_proactive_reminders ---

def test_reminder_deleted_ticket_marked_closed_externally():
    """404 from Proactivanet on WAITING ticket → CLOSED_EXTERNALLY, no remind."""
    conn = _conn()
    _insert_waiting(conn, "W1", hours_old=30)
    pn = _mock_pn(None)
    with patch("panpilot.intake.scheduler.route") as mock_route:
        count = send_proactive_reminders(conn, get_settings(), _ACTION_TYPE_MAP,
                                         _TERMINAL_NAMES, proactivanet_client=pn)
    assert count == 0
    assert mock_route.call_count == 0
    row = conn.execute(
        "SELECT state FROM ticket_state WHERE ticket_id='W1'").fetchone()
    assert row["state"] == "CLOSED_EXTERNALLY"


def test_reminder_terminal_ticket_marked_closed_externally():
    """Terminal status on WAITING ticket → CLOSED_EXTERNALLY, no remind."""
    conn = _conn()
    _insert_waiting(conn, "W2", hours_old=30)
    pn = _mock_pn({"Status": "Resolved", "DateLastModified": None})
    with patch("panpilot.intake.scheduler.route") as mock_route:
        count = send_proactive_reminders(conn, get_settings(), _ACTION_TYPE_MAP,
                                         _TERMINAL_NAMES, proactivanet_client=pn)
    assert count == 0
    assert mock_route.call_count == 0
    row = conn.execute(
        "SELECT state FROM ticket_state WHERE ticket_id='W2'").fetchone()
    assert row["state"] == "CLOSED_EXTERNALLY"


def test_reminder_clock_drift_refreshes_timestamp_and_suppresses_reminder():
    """Fresh DateLastModified on WAITING ticket → updated_at refreshed, reminder suppressed."""
    conn = _conn()
    _insert_waiting(conn, "W3", hours_old=30)
    recent_iso = _past_iso(1.0)  # modified 1h ago — well within reminder threshold (24h)
    pn = _mock_pn({"Status": "Open", "DateLastModified": recent_iso})
    with patch("panpilot.intake.scheduler.route") as mock_route:
        count = send_proactive_reminders(conn, get_settings(), _ACTION_TYPE_MAP,
                                         _TERMINAL_NAMES, proactivanet_client=pn)
    assert count == 0
    assert mock_route.call_count == 0


def test_reminder_active_ticket_sends_reminder():
    """Active WAITING ticket past threshold → reminder fires as normal."""
    conn = _conn()
    _insert_waiting(conn, "W4", hours_old=30)
    past_iso = _past_iso(28.0)
    pn = _mock_pn({"Status": "Open", "DateLastModified": past_iso})
    with patch("panpilot.intake.scheduler.route"):
        count = send_proactive_reminders(conn, get_settings(), _ACTION_TYPE_MAP,
                                         _TERMINAL_NAMES, proactivanet_client=pn)
    assert count == 1
