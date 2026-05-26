from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Valid actions the engine can return. Router enforces this allowlist.
Action = Literal["clarify", "auto_respond", "remind", "alert", "none"]

# Reasons recorded when action == "none"
NoneReason = Literal[
    "no_action_warranted",
    "needs_human",
    "no_doc_coverage",
    "low_confidence",
]

# Per-ticket states for the state machine (T9)
TicketState = Literal[
    "PENDING_EVALUATION",
    "AUTO_RESP",
    "CLR_REQ",
    "WAITING",
    "STALE_ALERT",
    "PENDING_AGENT_ACTION",
    "NEEDS_HUMAN",
    "AWAITING_CLIENT_REPLY",
]


@dataclass
class TicketContext:
    """
    Clean, resolved view of a Proactivanet ticket passed to the intelligence layer.

    UUID foreign keys (PadPriorities_id, PadStatus_id) are resolved to readable
    labels by the worker before constructing this object. The engine never touches
    raw UUIDs or the Proactivanet API.
    """

    ticket_id: str
    title: str
    description: str
    status: str              # resolved status name, e.g. "Assigned"
    priority: str            # "P1" | "P2" | "P3"
    created_at: str
    last_modified: str
    awaiting_client_reply: bool
    ticket_code: str | None = None   # human-readable code, e.g. "INC 2026-000001"
    requester_id: str | None = None  # T17: PanUsers_idSource or PadCustomers_id


@dataclass
class Decision:
    """
    The output of evaluate_ticket(). Crosses from intelligence → execution.
    Immutable after creation — execution reads it, never writes back.

    confidence is always None in Phase 1. T12 (Files API RAG) populates it on
    the auto_respond path in Phase 2.
    """

    action: Action
    reasoning: str
    response_draft: str | None = None  # Spanish customer-facing text; auto_respond + remind only
    confidence: float | None = None    # Phase 2 (T12) only; None in Phase 1
    none_reason: str | None = None     # required when action == "none"
