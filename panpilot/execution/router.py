from __future__ import annotations

import logging
import sqlite3

import anthropic

from panpilot.config import Settings
from panpilot.execution.audit import write_audit
from panpilot.execution.proactivanet import (
    ANNOTATION_TYPE_AUTO_RESPOND,
    ANNOTATION_TYPE_CLARIFY,
    ANNOTATION_TYPE_INTERNAL,
    ANNOTATION_TYPE_REMIND,
    ProactivanetClient,
)
from panpilot.intelligence.models import Decision, TicketContext

logger = logging.getLogger(__name__)

# Maps each Decision action to the IncidentActionType name used for annotation.
# These names are resolved to UUIDs via app.state.action_type_map at call time.
_ANNOTATION_TYPE_NAME: dict[str, str] = {
    "clarify": ANNOTATION_TYPE_CLARIFY,        # UserTextQuestion — customer-visible
    "auto_respond": ANNOTATION_TYPE_AUTO_RESPOND,  # AutomaticResponse — customer-visible
    "remind": ANNOTATION_TYPE_REMIND,          # PublishedAction — customer-visible
    "alert": ANNOTATION_TYPE_INTERNAL,         # Annotation — internal only
}

# All actions the router is permitted to handle. Any action outside this set
# is a contract violation between the engine and the router.
_ALLOWED_ACTIONS = frozenset(_ANNOTATION_TYPE_NAME) | {"none"}

# Hard cap on annotation text length sent to Proactivanet.
_MAX_ANNOTATION_LEN = 4000


class PolicyViolation(Exception):
    """
    Raised when the router receives a Decision whose action is not in the allowlist.

    This should never happen in normal operation — it indicates either a Claude
    API contract change (tool schema drift) or a bug in the evaluation engine.
    The worker must catch this and route the event to the DLQ.
    """


def route(
    decision: Decision,
    ctx: TicketContext,
    settings: Settings,
    conn: sqlite3.Connection,
    action_type_map: dict[str, str],
    *,
    proactivanet_client: ProactivanetClient | None = None,
    anthropic_client: anthropic.Anthropic | None = None,
) -> None:
    """
    Dispatch a Decision to its executor and write the audit log entry.

    Execution contract:
    - action="none": audit only, no API call.
    - action in {clarify, auto_respond, remind, alert}:
        DRY_RUN=true  → audit with dry_run=1, no API call.
        DRY_RUN=false → audit first (so DLQ retries don't double-post), then post annotation.
    - unknown action: write audit, raise PolicyViolation.

    The audit entry is ALWAYS written, including for dry-run and policy violations,
    so every decision is traceable in the audit log regardless of outcome.

    action_type_map must be app.state.action_type_map from startup (T18 / T3).
    proactivanet_client is optional; omit in production (created from settings, closed after use).
    anthropic_client is optional; omit to create per-call (pass the startup singleton in production).
    """
    if decision.action not in _ALLOWED_ACTIONS:
        write_audit(conn, ctx.ticket_id, decision, dry_run=settings.dry_run, settings=settings,
                    ticket_code=ctx.ticket_code, anthropic_client=anthropic_client)
        raise PolicyViolation(
            f"Decision action {decision.action!r} is not in the router allowlist "
            f"{sorted(_ALLOWED_ACTIONS)}. Event will be sent to DLQ."
        )

    if decision.action == "none":
        logger.info("ticket=%s action=none reason=%s", ctx.ticket_id, decision.none_reason)
        write_audit(conn, ctx.ticket_id, decision, dry_run=settings.dry_run, settings=settings,
                    ticket_code=ctx.ticket_code, anthropic_client=anthropic_client)
        return

    if settings.dry_run:
        logger.info(
            "DRY_RUN ticket=%s action=%s (no API call)",
            ctx.ticket_id,
            decision.action,
        )
        write_audit(conn, ctx.ticket_id, decision, dry_run=True, settings=settings,
                    ticket_code=ctx.ticket_code, anthropic_client=anthropic_client)
        return

    # Live mode: resolve the action type UUID and verify it exists before posting.
    type_name = _ANNOTATION_TYPE_NAME[decision.action]
    action_type_id = action_type_map.get(type_name)
    if action_type_id is None:
        write_audit(conn, ctx.ticket_id, decision, dry_run=False, settings=settings,
                    ticket_code=ctx.ticket_code, anthropic_client=anthropic_client)
        raise PolicyViolation(
            f"action_type_map is missing {type_name!r}. "
            "Was reference data loaded correctly at startup?"
        )

    # P1.1: write audit BEFORE posting annotation.
    # If the DB write fails, no annotation is posted — the event stays in the DLQ
    # and the retry will post exactly once.  Posting first then failing the DB write
    # would cause a double-post on retry.
    write_audit(conn, ctx.ticket_id, decision, dry_run=False, settings=settings,
                ticket_code=ctx.ticket_code, anthropic_client=anthropic_client)

    # P2.2: cap and strip annotation text before sending to Proactivanet.
    text = (decision.response_draft or decision.reasoning)[:_MAX_ANNOTATION_LEN].strip()

    # P1.3: close client after use if we created it (prevent FD leak).
    owned = proactivanet_client is None
    _client = proactivanet_client or ProactivanetClient(settings)
    try:
        _client.post_annotation(ctx.ticket_id, text=text, action_type_id=action_type_id)
    finally:
        if owned:
            _client.close()

    logger.info(
        "ticket=%s action=%s annotation_type=%s posted",
        ctx.ticket_id,
        decision.action,
        type_name,
    )
