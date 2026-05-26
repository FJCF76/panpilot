"""
T11 — Clarification cap enforcement.

Called by the worker between evaluate_ticket() and route() to ensure
PanPilot never sends more than clarification_max clarification questions
per ticket. When the cap is reached the decision is replaced with
action="none"/needs_human so a human agent handles the ticket instead.

T16 (reminder cap, Phase 2) follows the same pattern and will be added here.
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
