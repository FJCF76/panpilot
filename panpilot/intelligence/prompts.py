"""
T7 — Prompt templates and tool schema for the evaluation engine.

Prompts are in English (planning language). response_draft sent to customers is Spanish.
Ticket content is wrapped in XML delimiters to harden against prompt injection in
user-supplied fields (Title, Description).
"""
from __future__ import annotations

import html

from panpilot.intelligence.models import TicketContext

SYSTEM_PROMPT = """\
You are PanPilot, an AI triage assistant for a Proactivanet software support team.
Your job is to analyze incoming support tickets and decide on the appropriate automated action.
You must always call the record_decision tool with your analysis.

Available actions:

- clarify: The ticket is missing critical information needed to diagnose the issue \
(environment, software version, error message, steps to reproduce, or supporting evidence). \
Use this when the first response should request this missing information. \
Only use clarify when information is genuinely absent — not merely incomplete.

- auto_respond: You can answer this ticket directly from official product documentation. \
response_draft MUST contain a complete, helpful answer written in Spanish. \
Only use this when you are confident the documentation covers the customer's question.

- remind: The ticket is waiting on a client response (awaiting_client_reply=yes) and \
has been silent too long. Send a polite follow-up in Spanish via response_draft.

- alert: The ticket has been open and unactioned for too long given its priority level. \
Flag it for agent attention with a brief internal note in reasoning.

- none: No automated action is warranted right now. Set none_reason to the most \
specific reason: no_action_warranted, needs_human, no_doc_coverage, or low_confidence.

Rules you must follow:
- PanPilot NEVER changes ticket status, priority, or assignment. It only posts \
comments and annotations.
- response_draft must be in Spanish and must directly address the customer's question.
- reasoning must be 2-3 sentences explaining WHY you chose this action, not what it is.
- When uncertain between acting and not acting, prefer none over a potentially wrong action.\
"""

DECISION_TOOL: dict = {
    "name": "record_decision",
    "description": "Record the triage decision for this support ticket.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["clarify", "auto_respond", "remind", "alert", "none"],
                "description": "The triage action to take on this ticket.",
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "2-3 sentences explaining why this action was chosen. "
                    "Focus on WHY, not what the action is."
                ),
            },
            "response_draft": {
                "type": "string",
                "description": (
                    "Customer-facing text in Spanish. "
                    "Required for auto_respond and remind. Null for all other actions."
                ),
            },
            "none_reason": {
                "type": "string",
                "enum": [
                    "no_action_warranted",
                    "needs_human",
                    "no_doc_coverage",
                    "low_confidence",
                ],
                "description": "Required when action is none. Omit for all other actions.",
            },
        },
        "required": ["action", "reasoning"],
    },
}


def build_user_message(ctx: TicketContext) -> str:
    """
    Build the user turn for the evaluation prompt.

    Ticket content is wrapped in XML delimiters so that injection attempts in
    user-supplied fields (e.g. a Title containing </ticket><system>...</system>)
    remain scoped as data and do not affect the prompt structure.
    """
    awaiting = "yes" if ctx.awaiting_client_reply else "no"
    # html.escape on user-supplied fields so injection attempts in Title/Description
    # remain scoped as data and cannot break the XML structure of the prompt.
    return (
        "Analyze the following support ticket and record your decision "
        "using the record_decision tool.\n\n"
        "<ticket>\n"
        f"<id>{ctx.ticket_id}</id>\n"
        f"<title>{html.escape(ctx.title)}</title>\n"
        f"<description>{html.escape(ctx.description)}</description>\n"
        f"<status>{ctx.status}</status>\n"
        f"<priority>{ctx.priority}</priority>\n"
        f"<created_at>{ctx.created_at}</created_at>\n"
        f"<last_modified>{ctx.last_modified}</last_modified>\n"
        f"<awaiting_client_reply>{awaiting}</awaiting_client_reply>\n"
        "</ticket>"
    )
