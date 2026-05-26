"""
T9 — Per-ticket state machine.

Tracks PanPilot's relationship with each ticket in the ticket_state table.
Called by the worker after evaluate_ticket() + route() complete.

State meanings
--------------
PENDING_EVALUATION   Worker claimed the event; evaluation in progress.
CLR_REQ              Clarification question sent to customer.
AUTO_RESP            Auto-response sent to customer.
WAITING              PanPilot acted; waiting for human agent to follow through.
STALE_ALERT          Internal stale alert posted to agent.
PENDING_AGENT_ACTION Explicit escalation short of NEEDS_HUMAN.
NEEDS_HUMAN          PanPilot will take no further autonomous action on this ticket.
AWAITING_CLIENT_REPLY Waiting for customer reply (set externally, e.g. via webhook).

Transition rules
----------------
clarify          → CLR_REQ
auto_respond     → AUTO_RESP
remind           → WAITING
alert            → STALE_ALERT
none / needs_human, no_doc_coverage, low_confidence → NEEDS_HUMAN
none / no_action_warranted → preserve current state (WAITING if first visit)

none_reason cases that escalate to NEEDS_HUMAN (PanPilot cannot help):
  needs_human      — engine determined human required
  no_doc_coverage  — Phase 1: no indexed docs to draw from
  low_confidence   — Phase 1: confidence below threshold
"""
from __future__ import annotations

import logging
import sqlite3

from panpilot.intelligence.models import Decision

logger = logging.getLogger(__name__)

# none_reason values that escalate the ticket to NEEDS_HUMAN.
_ESCALATE_REASONS = frozenset({"needs_human", "no_doc_coverage", "low_confidence"})


# ---------------------------------------------------------------------------
# Pure transition function
# ---------------------------------------------------------------------------

def transition(current_state: str | None, decision: Decision) -> str:
    """
    Compute the next TicketState given the current state and a Decision.

    Pure function — no I/O. Separated from apply_transition() so it can be
    unit-tested without a database.
    """
    action = decision.action

    if action == "clarify":
        return "CLR_REQ"
    if action == "auto_respond":
        return "AUTO_RESP"
    if action == "remind":
        return "WAITING"
    if action == "alert":
        return "STALE_ALERT"
    if action == "none":
        if decision.none_reason in _ESCALATE_REASONS:
            return "NEEDS_HUMAN"
        # no_action_warranted: nothing for PanPilot to do right now.
        # Preserve the existing state so a previous CLR_REQ / AUTO_RESP is not
        # overwritten by a later no-op evaluation.
        # PENDING_EVALUATION is a transient claim marker, not a real settled state
        # — treat it the same as no prior state so we always land on WAITING.
        if current_state and current_state != "PENDING_EVALUATION":
            return current_state
        return "WAITING"

    # Unknown action — PolicyViolation should have been raised before this point.
    logger.warning(
        "state_machine.transition: unknown action %r — current state preserved", action
    )
    if current_state and current_state != "PENDING_EVALUATION":
        return current_state
    return "WAITING"


# ---------------------------------------------------------------------------
# DB-aware upsert
# ---------------------------------------------------------------------------

def apply_transition(
    conn: sqlite3.Connection,
    ticket_id: str,
    decision: Decision,
    priority: str,
) -> str:
    """
    Compute the next state and upsert ticket_state.

    - Updates clarification_count when action == "clarify".
    - Updates reminder_count when action == "remind".
    - Always writes the supplied priority (callers resolve UUIDs before calling).
    - Returns the new state string.

    Must be called after route() so the audit log entry is guaranteed to exist
    before the state transitions (audit is always written, state only on success).
    """
    row = conn.execute(
        "SELECT state, clarification_count, reminder_count "
        "FROM ticket_state WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()

    current_state: str | None = row["state"] if row else None
    clarification_count = (row["clarification_count"] if row else 0) + (
        1 if decision.action == "clarify" else 0
    )
    reminder_count = (row["reminder_count"] if row else 0) + (
        1 if decision.action == "remind" else 0
    )

    new_state = transition(current_state, decision)

    conn.execute(
        """
        INSERT INTO ticket_state
            (ticket_id, state, priority, updated_at, clarification_count, reminder_count)
        VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?, ?)
        ON CONFLICT(ticket_id) DO UPDATE SET
            state               = excluded.state,
            priority            = excluded.priority,
            updated_at          = excluded.updated_at,
            clarification_count = excluded.clarification_count,
            reminder_count      = excluded.reminder_count
        """,
        (ticket_id, new_state, priority, clarification_count, reminder_count),
    )
    conn.commit()

    logger.debug(
        "ticket=%s  %s → %s  (action=%s priority=%s)",
        ticket_id,
        current_state or "NEW",
        new_state,
        decision.action,
        priority,
    )
    return new_state


# ---------------------------------------------------------------------------
# Worker pre-evaluation marker
# ---------------------------------------------------------------------------

def mark_pending_evaluation(
    conn: sqlite3.Connection,
    ticket_id: str,
    priority: str,
) -> None:
    """
    Set a ticket to PENDING_EVALUATION before evaluate_ticket() is called.

    Called by the worker immediately after claiming an event so that the stale
    detector skips this ticket while it is being processed.  apply_transition()
    is called again after routing with the real Decision to set the final state.

    Preserves clarification_count and reminder_count if a row already exists.
    """
    conn.execute(
        """
        INSERT INTO ticket_state
            (ticket_id, state, priority, updated_at, clarification_count, reminder_count)
        VALUES (?, 'PENDING_EVALUATION', ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 0, 0)
        ON CONFLICT(ticket_id) DO UPDATE SET
            state    = 'PENDING_EVALUATION',
            priority = excluded.priority,
            updated_at = excluded.updated_at
        """,
        (ticket_id, priority),
    )
    conn.commit()
    logger.debug("ticket=%s → PENDING_EVALUATION (priority=%s)", ticket_id, priority)
