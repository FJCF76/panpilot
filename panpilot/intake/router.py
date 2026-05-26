"""
POST /webhook — intake endpoint for Proactivanet event deliveries.

Receives the raw incident payload, resolves the ticket ID, computes the
idempotency key, and stores the event.  Returns 200 immediately so
Proactivanet does not time out waiting for evaluation to complete.

Auth: if WEBHOOK_SECRET is set, the caller must supply it in the
X-Webhook-Secret header (constant-time comparison via secrets.compare_digest).
If WEBHOOK_SECRET is empty, no header check is performed — appropriate for
deployments where the endpoint is only reachable from the internal network.

Payload normalisation:
Proactivanet sends four distinct payload shapes at this endpoint:

  1. Flat incident: {IncidentId, Id, Title, ...}  — standard create/save event.
  2. Modification diff: {OldValue: {incident}, NewValue: {incident}}  — fired on
     field edits.  We extract NewValue and proceed as shape 1.
  3. Annotation added: {Incident: {incident}, Action: int, Annotations: [...]}
     — fired whenever an annotation is added.  We unwrap Incident and proceed
     as shape 1.
  4. Status change: {Incident: {incident}, StatusOld: int, StatusNew: int,
     PadStatus_idOld: uuid, PadStatus_idNew: uuid}  — fired on status transitions.
     We unwrap Incident and proceed as shape 1.

  Shapes 3 and 4 both carry "Incident" as a key; the handler unwraps any
  "Incident"-wrapped payload and applies the annotation loop guard only when
  the "Annotations" key is also present.

Loop guard — annotation webhooks:
When PanPilot posts an annotation, Proactivanet fires an annotation webhook
back at us.  If processed, that would trigger another Claude evaluation →
another annotation → infinite loop.  We break it by inspecting
Annotations[*].PawSvcAuthUsers_id: if every annotation in the batch was
authored by PanPilot (PROACTIVANET_AUTHOR_ID), we drop the event immediately.
Human-authored annotations are NOT dropped and proceed to evaluation.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from typing import Any, Generator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

logger = logging.getLogger(__name__)

from panpilot.config import Settings, get_settings
from panpilot.db.connection import get_connection, main_db_path
from panpilot.intake.event_store import compute_idempotency_key, store_event

router = APIRouter()

# Reject webhook bodies larger than this to prevent memory exhaustion.
_MAX_WEBHOOK_BODY = 1 * 1024 * 1024  # 1 MiB

# Event types that PanPilot never needs to evaluate.
# "En anotación" is fired when any annotation is added to a ticket — including
# annotations we just posted.  Processing it would create an infinite loop.
# Proactivanet may use slightly different capitalisation; normalise to lower.
_IGNORED_EVENT_TYPES: frozenset[str] = frozenset({"en anotación", "en anotacion"})


def _conn() -> Generator[sqlite3.Connection, None, None]:
    settings = get_settings()
    conn = get_connection(main_db_path(settings))
    try:
        yield conn
    finally:
        conn.close()


def _verify_secret(
    x_webhook_secret: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.webhook_secret:
        return
    if x_webhook_secret is None or not secrets.compare_digest(
        x_webhook_secret, settings.webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")


@router.post("/webhook", status_code=200)
async def receive_webhook(
    request: Request,
    event_type: str = Query(default="Guardado"),
    conn: sqlite3.Connection = Depends(_conn),
    _auth: None = Depends(_verify_secret),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """
    Accept one Proactivanet incident event and queue it for evaluation.

    Returns {"status": "ok", "stored": true} on a new event, or
    {"status": "ok", "stored": false} if the idempotency key was already seen
    (duplicate delivery — safe to ignore).
    """
    # Enforce body size limit before reading the full body into memory.
    content_length = request.headers.get("content-length")
    if content_length is not None and int(content_length) > _MAX_WEBHOOK_BODY:
        raise HTTPException(status_code=413, detail="Payload too large")

    body = await request.body()
    if len(body) > _MAX_WEBHOOK_BODY:
        raise HTTPException(status_code=413, detail="Payload too large")

    # Loop guard: drop event types that would trigger re-evaluation of our own
    # annotations.  Log the payload once at WARNING so operators can identify
    # the author-field name if they need it in the future.
    if event_type.lower() in _IGNORED_EVENT_TYPES:
        logger.warning(
            "Dropping ignored event_type=%r — loop guard (payload follows for author-field discovery): %s",
            event_type,
            body.decode("utf-8", errors="replace")[:2000],
        )
        return {"status": "ok", "stored": False}

    parsed = json.loads(body)

    # Proactivanet payloads are always JSON objects.  A JSON array or primitive
    # would cause AttributeError on .get() — return 422 instead of 500.
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=422,
            detail=f"Payload must be a JSON object, got {type(parsed).__name__}",
        )
    payload: dict[str, Any] = parsed

    # Wrapped incident payloads — Proactivanet nests the incident in several shapes:
    #   Annotation: {Incident: {...}, Action: int, Annotations: [...]}
    #   Status change: {Incident: {...}, StatusOld: int, StatusNew: int, PadStatus_idOld, PadStatus_idNew}
    # Any wrapper with an "Incident" key is unwrapped to the inner object.
    # Before unwrapping annotation payloads, apply the PanPilot loop guard.
    if "Incident" in payload:
        if "Annotations" in payload:
            annotations: list[dict[str, Any]] = payload.get("Annotations") or []
            panpilot_author = settings.proactivanet_author_id
            if panpilot_author and annotations and all(
                a.get("PawSvcAuthUsers_id") == panpilot_author for a in annotations
            ):
                logger.info(
                    "Loop guard: dropping annotation webhook — all %d annotation(s) from PanPilot (author=%s)",
                    len(annotations),
                    panpilot_author,
                )
                return {"status": "ok", "stored": False}
        payload = payload["Incident"]

    # Modification diff webhook: {OldValue: {...}, NewValue: {...}}
    # Proactivanet sends the full before/after incident object; use NewValue.
    elif "NewValue" in payload and "OldValue" in payload:
        payload = payload["NewValue"]

    ticket_id = payload.get("IncidentId") or payload.get("Id")
    if not ticket_id:
        raise HTTPException(
            status_code=400,
            detail="Payload must contain IncidentId or Id",
        )
    ticket_id = str(ticket_id)

    key = compute_idempotency_key(payload, ticket_id, event_type, settings)
    stored = store_event(conn, key, ticket_id, event_type, payload)

    return {"status": "ok", "stored": stored}
