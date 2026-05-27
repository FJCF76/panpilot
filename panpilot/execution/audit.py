"""
T5 — Audit log writer.

The audit_log table is append-only. This module never issues UPDATE or DELETE
against it. The admin UI (T5 read side) and flagging (Phase 2) are separate.
"""
from __future__ import annotations

import logging
import sqlite3

import anthropic

from panpilot.config import Settings
from panpilot.intelligence.models import Decision

logger = logging.getLogger(__name__)

_TRANSLATE_PROMPT = (
    "The following text is either in English or already in Spanish. "
    "If it is in English, translate it to Spanish. "
    "If it is already in Spanish, return it unchanged. "
    "Preserve technical terms. "
    "Output only the result, no preamble.\n\n"
)


def _translate_to_spanish(text: str, client: anthropic.Anthropic) -> str:
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": _TRANSLATE_PROMPT + text}],
    )
    return message.content[0].text


def write_audit(
    conn: sqlite3.Connection,
    ticket_id: str,
    decision: Decision,
    *,
    dry_run: bool,
    settings: Settings,
    ticket_code: str | None = None,
    anthropic_client: anthropic.Anthropic | None = None,
) -> None:
    """
    Append a decision record to the audit log.

    Must be called for every routed decision — including action=none and dry-run
    decisions — so the audit log is a complete record of everything PanPilot evaluated.
    decision.reasoning is translated to Spanish before storage; the Decision object
    itself is never mutated.
    ticket_code is the human-readable Proactivanet code (e.g. "INC 2026-000001").
    flagged_by and flag_reason are Phase 2 fields; always NULL here.

    On DLQ retry a second audit entry is written intentionally — the retry is a new
    evaluation attempt and produces its own record with a fresh timestamp.  The two
    entries share the same ticket_id and are distinguishable by evaluated_at.
    """
    client = anthropic_client or anthropic.Anthropic(api_key=settings.anthropic_api_key)
    try:
        reasoning_es = _translate_to_spanish(decision.reasoning, client)
    except Exception:
        logger.warning(
            "Audit: translation failed for ticket=%s; storing English reasoning",
            ticket_id,
        )
        reasoning_es = decision.reasoning

    conn.execute(
        """
        INSERT INTO audit_log
            (ticket_id, ticket_code, action, none_reason, reasoning,
             confidence, response_draft, dry_run)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticket_id,
            ticket_code,
            decision.action,
            decision.none_reason,
            reasoning_es,
            decision.confidence,
            decision.response_draft,
            1 if dry_run else 0,
        ),
    )
    conn.commit()
    logger.debug(
        "Audit: ticket=%s action=%s dry_run=%s",
        ticket_id,
        decision.action,
        dry_run,
    )
