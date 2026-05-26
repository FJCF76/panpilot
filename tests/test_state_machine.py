"""Tests for T9: per-ticket state machine."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from panpilot.intelligence.models import Decision
from panpilot.intelligence.state_machine import (
    _ESCALATE_REASONS,
    apply_transition,
    mark_pending_evaluation,
    transition,
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


def _decision(action: str, none_reason: str | None = None, **kwargs) -> Decision:
    defaults: dict = {"reasoning": "test"}
    if action in {"auto_respond", "remind"}:
        defaults["response_draft"] = "respuesta"
    if action == "none" and none_reason is None:
        none_reason = "no_action_warranted"
    return Decision(action=action, none_reason=none_reason, **{**defaults, **kwargs})


def _state(conn: sqlite3.Connection, ticket_id: str) -> dict | None:
    row = conn.execute(
        "SELECT state, priority, clarification_count, reminder_count FROM ticket_state "
        "WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# transition() — pure function tests
# ---------------------------------------------------------------------------

class TestTransition:
    """Pure state transition logic, no DB."""

    # Action → next state
    def test_clarify_returns_clr_req(self):
        assert transition(None, _decision("clarify")) == "CLR_REQ"

    def test_auto_respond_returns_auto_resp(self):
        assert transition(None, _decision("auto_respond")) == "AUTO_RESP"

    def test_remind_returns_waiting(self):
        assert transition(None, _decision("remind")) == "WAITING"

    def test_alert_returns_stale_alert(self):
        assert transition(None, _decision("alert")) == "STALE_ALERT"

    # action=none — escalation reasons → NEEDS_HUMAN
    @pytest.mark.parametrize("reason", sorted(_ESCALATE_REASONS))
    def test_none_escalate_reason_returns_needs_human(self, reason: str):
        assert transition(None, _decision("none", none_reason=reason)) == "NEEDS_HUMAN"

    # action=none — no_action_warranted → preserve current state
    def test_no_action_warranted_preserves_current_state(self):
        assert transition("CLR_REQ", _decision("none")) == "CLR_REQ"

    def test_no_action_warranted_preserves_auto_resp(self):
        assert transition("AUTO_RESP", _decision("none")) == "AUTO_RESP"

    def test_no_action_warranted_preserves_waiting(self):
        assert transition("WAITING", _decision("none")) == "WAITING"

    def test_no_action_warranted_preserves_needs_human(self):
        # Once NEEDS_HUMAN, a no_action_warranted evaluation doesn't reset it.
        assert transition("NEEDS_HUMAN", _decision("none")) == "NEEDS_HUMAN"

    def test_no_action_warranted_first_visit_returns_waiting(self):
        # No prior state: default to WAITING (ticket exists in system but is unknown).
        assert transition(None, _decision("none")) == "WAITING"

    # Transition ignores current state for action-driven transitions
    def test_clarify_overrides_existing_state(self):
        assert transition("AUTO_RESP", _decision("clarify")) == "CLR_REQ"

    def test_auto_respond_overrides_existing_state(self):
        assert transition("WAITING", _decision("auto_respond")) == "AUTO_RESP"

    def test_remind_overrides_clr_req(self):
        assert transition("CLR_REQ", _decision("remind")) == "WAITING"

    def test_alert_overrides_waiting(self):
        assert transition("WAITING", _decision("alert")) == "STALE_ALERT"

    # Unknown action → preserve
    def test_unknown_action_preserves_state(self):
        result = transition("WAITING", Decision(action="delete_ticket", reasoning="x"))  # type: ignore[arg-type]
        assert result == "WAITING"

    def test_unknown_action_no_prior_state_returns_waiting(self):
        # Unknown actions must never settle on PENDING_EVALUATION (transient marker).
        result = transition(None, Decision(action="delete_ticket", reasoning="x"))  # type: ignore[arg-type]
        assert result == "WAITING"

    def test_no_action_warranted_pending_evaluation_returns_waiting(self):
        # PENDING_EVALUATION is a transient claim marker — must never be the settled state.
        # If action=none and current_state=PENDING_EVALUATION, land on WAITING.
        assert transition("PENDING_EVALUATION", _decision("none")) == "WAITING"

    def test_unknown_action_pending_evaluation_returns_waiting(self):
        result = transition("PENDING_EVALUATION", Decision(action="delete_ticket", reasoning="x"))  # type: ignore[arg-type]
        assert result == "WAITING"


# ---------------------------------------------------------------------------
# apply_transition() — DB upsert tests
# ---------------------------------------------------------------------------

class TestApplyTransition:

    def test_inserts_row_on_first_visit(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("clarify"), "P2")
        assert _state(conn, "TKT-1") is not None

    def test_first_visit_sets_correct_state(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("auto_respond"), "P2")
        assert _state(conn, "TKT-1")["state"] == "AUTO_RESP"

    def test_second_visit_updates_state(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("clarify"), "P2")
        apply_transition(conn, "TKT-1", _decision("auto_respond"), "P2")
        assert _state(conn, "TKT-1")["state"] == "AUTO_RESP"

    def test_returns_new_state(self):
        conn = _conn()
        result = apply_transition(conn, "TKT-1", _decision("remind"), "P1")
        assert result == "WAITING"

    def test_priority_is_stored(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("clarify"), "P1")
        assert _state(conn, "TKT-1")["priority"] == "P1"

    def test_priority_is_updated_on_change(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("clarify"), "P1")
        apply_transition(conn, "TKT-1", _decision("auto_respond"), "P2")
        assert _state(conn, "TKT-1")["priority"] == "P2"

    def test_clarify_increments_clarification_count(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("clarify"), "P2")
        assert _state(conn, "TKT-1")["clarification_count"] == 1

    def test_second_clarify_increments_to_two(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("clarify"), "P2")
        apply_transition(conn, "TKT-1", _decision("clarify"), "P2")
        assert _state(conn, "TKT-1")["clarification_count"] == 2

    def test_remind_increments_reminder_count(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("remind"), "P2")
        assert _state(conn, "TKT-1")["reminder_count"] == 1

    def test_second_remind_increments_to_two(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("remind"), "P2")
        apply_transition(conn, "TKT-1", _decision("remind"), "P2")
        assert _state(conn, "TKT-1")["reminder_count"] == 2

    def test_auto_respond_does_not_increment_clarification_count(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("clarify"), "P2")
        apply_transition(conn, "TKT-1", _decision("auto_respond"), "P2")
        assert _state(conn, "TKT-1")["clarification_count"] == 1

    def test_none_does_not_increment_any_count(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("none"), "P2")
        s = _state(conn, "TKT-1")
        assert s["clarification_count"] == 0
        assert s["reminder_count"] == 0

    def test_alert_does_not_increment_any_count(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("alert"), "P2")
        s = _state(conn, "TKT-1")
        assert s["clarification_count"] == 0
        assert s["reminder_count"] == 0

    def test_only_one_row_per_ticket(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("clarify"), "P2")
        apply_transition(conn, "TKT-1", _decision("auto_respond"), "P2")
        apply_transition(conn, "TKT-1", _decision("none"), "P2")
        count = conn.execute(
            "SELECT COUNT(*) FROM ticket_state WHERE ticket_id='TKT-1'"
        ).fetchone()[0]
        assert count == 1

    def test_different_tickets_independent(self):
        conn = _conn()
        apply_transition(conn, "TKT-A", _decision("clarify"), "P1")
        apply_transition(conn, "TKT-B", _decision("auto_respond"), "P3")
        assert _state(conn, "TKT-A")["state"] == "CLR_REQ"
        assert _state(conn, "TKT-B")["state"] == "AUTO_RESP"

    def test_no_action_warranted_preserves_previous_state_in_db(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("clarify"), "P2")
        apply_transition(conn, "TKT-1", _decision("none"), "P2")
        assert _state(conn, "TKT-1")["state"] == "CLR_REQ"

    def test_needs_human_reason_sets_needs_human_in_db(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("none", "needs_human"), "P2")
        assert _state(conn, "TKT-1")["state"] == "NEEDS_HUMAN"

    def test_updated_at_is_set(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("clarify"), "P2")
        row = conn.execute(
            "SELECT updated_at FROM ticket_state WHERE ticket_id='TKT-1'"
        ).fetchone()
        assert row["updated_at"] is not None
        assert "T" in row["updated_at"]  # ISO format sanity check


# ---------------------------------------------------------------------------
# mark_pending_evaluation()
# ---------------------------------------------------------------------------

class TestMarkPendingEvaluation:

    def test_inserts_pending_evaluation_state(self):
        conn = _conn()
        mark_pending_evaluation(conn, "TKT-1", "P2")
        assert _state(conn, "TKT-1")["state"] == "PENDING_EVALUATION"

    def test_stores_priority(self):
        conn = _conn()
        mark_pending_evaluation(conn, "TKT-1", "P1")
        assert _state(conn, "TKT-1")["priority"] == "P1"

    def test_overrides_existing_state(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("auto_respond"), "P2")
        mark_pending_evaluation(conn, "TKT-1", "P2")
        assert _state(conn, "TKT-1")["state"] == "PENDING_EVALUATION"

    def test_preserves_clarification_count(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("clarify"), "P2")
        apply_transition(conn, "TKT-1", _decision("clarify"), "P2")
        mark_pending_evaluation(conn, "TKT-1", "P2")
        assert _state(conn, "TKT-1")["clarification_count"] == 2

    def test_preserves_reminder_count(self):
        conn = _conn()
        apply_transition(conn, "TKT-1", _decision("remind"), "P2")
        mark_pending_evaluation(conn, "TKT-1", "P2")
        assert _state(conn, "TKT-1")["reminder_count"] == 1

    def test_idempotent_on_fresh_ticket(self):
        conn = _conn()
        mark_pending_evaluation(conn, "TKT-1", "P2")
        mark_pending_evaluation(conn, "TKT-1", "P2")
        count = conn.execute(
            "SELECT COUNT(*) FROM ticket_state WHERE ticket_id='TKT-1'"
        ).fetchone()[0]
        assert count == 1

    def test_updates_priority_on_second_call(self):
        conn = _conn()
        mark_pending_evaluation(conn, "TKT-1", "P1")
        mark_pending_evaluation(conn, "TKT-1", "P2")
        assert _state(conn, "TKT-1")["priority"] == "P2"
