"""
T11/T16/T17 — Per-ticket and org-level action cap enforcement.

Called by the worker between evaluate_ticket() and route() to ensure
PanPilot never exceeds the configured per-ticket limits.  When a cap is
reached the decision is replaced with action="none"/needs_human so a
human agent handles the ticket instead.

  T11: clarification cap    — max clarification_max questions per ticket.
  T16: reminder cap         — max reminder_max_per_ticket reminders per ticket.
  T17: org reminder cap     — max reminder_org_max reminders across all tickets
                              for the same requester within reminder_org_window_days.
"""
from __future__ import annotations

import logging
import sqlite3

from panpilot.config import Settings
from panpilot.intelligence.models import Decision

logger = logging.getLogger(__name__)


def enforce_clarification_cap(
    conn: sqlite3.Connection,
    ticket_id: str,
    decision: Decision,
    settings: Settings,
) -> Decision:
    """
    If decision.action == "clarify" and the ticket has already reached
    clarification_max, replace the decision with needs_human.

    Returns the original decision unchanged for all other actions.
    """
    if decision.action != "clarify":
        return decision

    row = conn.execute(
        "SELECT clarification_count FROM ticket_state WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()
    count = row["clarification_count"] if row else 0

    if count >= settings.clarification_max:
        logger.info(
            "ticket=%s clarification cap reached (%d/%d) — escalating to needs_human",
            ticket_id,
            count,
            settings.clarification_max,
        )
        return Decision(
            action="none",
            reasoning=(
                f"Límite de aclaraciones alcanzado ({count}/{settings.clarification_max}). "
                "Se requiere atención de un agente."
            ),
            none_reason="needs_human",
        )

    return decision


def enforce_reminder_cap(
    conn: sqlite3.Connection,
    ticket_id: str,
    decision: Decision,
    settings: Settings,
) -> Decision:
    """
    If decision.action == "remind" and the ticket has already reached
    reminder_max_per_ticket, replace the decision with needs_human.

    Returns the original decision unchanged for all other actions.
    """
    if decision.action != "remind":
        return decision

    row = conn.execute(
        "SELECT reminder_count FROM ticket_state WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()
    count = row["reminder_count"] if row else 0

    if count >= settings.reminder_max_per_ticket:
        logger.info(
            "ticket=%s reminder cap reached (%d/%d) — escalating to needs_human",
            ticket_id,
            count,
            settings.reminder_max_per_ticket,
        )
        return Decision(
            action="none",
            reasoning=(
                f"Límite de recordatorios alcanzado ({count}/{settings.reminder_max_per_ticket}). "
                "Se requiere atención de un agente."
            ),
            none_reason="needs_human",
        )

    return decision


def enforce_org_reminder_cap(
    conn: sqlite3.Connection,
    ticket_id: str,
    requester_id: str | None,
    decision: Decision,
    settings: Settings,
) -> Decision:
    """
    T17: If decision.action == "remind" and the requester has already received
    reminder_org_max reminders across all their tickets within reminder_org_window_days,
    replace the decision with needs_human.

    Skipped entirely when requester_id is None (API-created tickets without a requester).
    Returns the original decision unchanged for all other actions.
    """
    if decision.action != "remind":
        return decision
    if requester_id is None:
        return decision

    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM audit_log al
        JOIN ticket_state ts ON al.ticket_id = ts.ticket_id
        WHERE ts.requester_id = ?
          AND al.action = 'remind'
          AND al.dry_run = 0
          AND al.evaluated_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now',
                                          '-' || ? || ' days')
        """,
        (requester_id, settings.reminder_org_window_days),
    ).fetchone()
    count = row["cnt"] if row else 0

    if count >= settings.reminder_org_max:
        logger.info(
            "ticket=%s org reminder cap reached (%d/%d in %d days, requester=%s) — escalating to needs_human",
            ticket_id,
            count,
            settings.reminder_org_max,
            settings.reminder_org_window_days,
            requester_id,
        )
        return Decision(
            action="none",
            reasoning=(
                f"Límite de recordatorios de organización alcanzado "
                f"({count}/{settings.reminder_org_max} en {settings.reminder_org_window_days} días). "
                "Se requiere atención de un agente."
            ),
            none_reason="needs_human",
        )

    return decision
