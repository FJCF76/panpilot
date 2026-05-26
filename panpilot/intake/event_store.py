from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from panpilot.config import Settings


def compute_idempotency_key(
    payload: dict[str, Any],
    ticket_id: str,
    event_type: str,
    settings: Settings,
) -> str:
    """
    Derive a stable idempotency key for this webhook delivery.

    Option A (preferred): if WEBHOOK_IDEMPOTENCY_FIELD is configured and the
    named field is present in the payload, use that value directly. This is
    the most reliable source — a delivery ID assigned by Proactivanet.

    Option B (fallback): sha256(ticket_id + ":" + event_type + ":" + DateLastModified).
    Stable across restarts as long as the same event isn't modified before delivery.
    Used when WEBHOOK_IDEMPOTENCY_FIELD is unset or the field is absent from the payload.

    Option C — receive timestamp alone — is NEVER correct. Timestamps are not
    idempotent: a redelivered webhook arrives at a different time and would produce
    a different key, allowing the same event to be processed twice.
    """
    if settings.webhook_idempotency_field:
        field_value = payload.get(settings.webhook_idempotency_field)
        if field_value is not None:
            return str(field_value)

    # Option B fallback: deterministic from event content, not delivery time
    date_last_modified = str(payload.get("DateLastModified", ""))
    raw = f"{ticket_id}:{event_type}:{date_last_modified}"
    return hashlib.sha256(raw.encode()).hexdigest()


def store_event(
    conn: sqlite3.Connection,
    idempotency_key: str,
    ticket_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> bool:
    """
    Write the event to the queue if it hasn't been seen before.

    Uses INSERT OR IGNORE on the idempotency key (PRIMARY KEY) so duplicate
    deliveries are silently dropped at the DB layer with no error raised.

    Returns True if the event was stored (new), False if it was a duplicate.
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO events (id, ticket_id, event_type, payload)
        VALUES (?, ?, ?, ?)
        """,
        (idempotency_key, ticket_id, event_type, json.dumps(payload)),
    )
    conn.commit()
    return cursor.rowcount == 1


def claim_next_event(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """
    Return the oldest unprocessed event as a plain dict, or None if the queue is empty.
    Does not mark the event as processed — call mark_event_processed() after handling.
    """
    row = conn.execute(
        """
        SELECT id, ticket_id, event_type, payload, received_at
        FROM events
        WHERE processed = 0
        ORDER BY received_at ASC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "ticket_id": row["ticket_id"],
        "event_type": row["event_type"],
        "payload": json.loads(row["payload"]),
        "received_at": row["received_at"],
    }


def mark_event_processed(conn: sqlite3.Connection, event_id: str) -> None:
    """Mark an event as processed so the worker won't pick it up again."""
    conn.execute("UPDATE events SET processed = 1 WHERE id = ?", (event_id,))
    conn.commit()
