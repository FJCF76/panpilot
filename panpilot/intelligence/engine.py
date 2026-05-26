from __future__ import annotations

import logging
from typing import Any

import anthropic

from panpilot.config import Settings
from panpilot.intelligence.models import Decision, TicketContext
from panpilot.intelligence.prompts import DECISION_TOOL, SYSTEM_PROMPT, build_user_message

logger = logging.getLogger(__name__)

# Sonnet 4.6 is the default — strong reasoning at reasonable cost for per-ticket evaluation.
MODEL = "claude-sonnet-4-6"


def evaluate_ticket(
    ctx: TicketContext,
    settings: Settings,
    *,
    client: anthropic.Anthropic | None = None,
) -> Decision:
    """
    Evaluate a support ticket and return a Decision.

    Single entry point for the intelligence layer. Makes exactly one Claude call
    in Phase 1. Phase 2 (T12) adds a second pass on the auto_respond path using
    the Files API for RAG-backed responses.

    The optional `client` parameter exists for testing. Production code omits it
    and lets the function create the client from settings.anthropic_api_key.
    """
    if client is None:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    logger.debug(
        "Evaluating ticket %s (priority=%s, status=%s)",
        ctx.ticket_id,
        ctx.priority,
        ctx.status,
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_message(ctx)}],
        tools=[DECISION_TOOL],
        # Force Claude to always call record_decision — no free-text fallback.
        tool_choice={"type": "tool", "name": "record_decision"},
    )

    decision = _parse_decision(response)
    logger.info(
        "ticket=%s action=%s reasoning=%r",
        ctx.ticket_id,
        decision.action,
        decision.reasoning[:80],
    )
    return decision


def _parse_decision(response: Any) -> Decision:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_decision":
            data = block.input
            return Decision(
                action=data["action"],
                reasoning=data["reasoning"],
                response_draft=data.get("response_draft"),
                confidence=None,  # Phase 2 (T12) only — always None from the Phase 1 engine
                none_reason=data.get("none_reason"),
            )
    raise ValueError(
        f"Claude response contained no record_decision tool_use block. "
        f"stop_reason={getattr(response, 'stop_reason', '?')!r}, "
        f"content_types={[getattr(b, 'type', '?') for b in response.content]}"
    )
