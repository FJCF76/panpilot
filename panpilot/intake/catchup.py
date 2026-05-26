"""
T10 — Startup catch-up loader.

On every startup, PanPilot queries Proactivanet for incidents modified since
the most recent event received_at stored in the events table.  Any incidents
not already present (idempotency key prevents duplicates) are injected as
synthetic events so the worker picks them up on its first poll.

This covers tickets whose webhooks were missed during downtime.

The catch-up runs synchronously in the lifespan (before the worker thread
starts) via asyncio.to_thread so the async context is not blocked.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from panpilot.config import Settings
from panpilot.execution.proactivanet import ProactivanetClient
from panpilot.intake.event_store import compute_idempotency_key, store_event

logger = logging.getLogger(__name__)

# Synthetic event type used for catch-up events so the worker can distinguish
# them from live webhook events if needed.
CATCHUP_EVENT_TYPE = "Catchup"

# How far back to look on the very first run (no events in DB yet).
_DEFAULT_LOOKBACK_HOURS = 24


def get_last_received_at(conn: sqlite3.Connection) -> str | None:
    """Return the ISO timestamp of the most recently received event, or None."""
    row = conn.execute(
        "SELECT MAX(received_at) AS last FROM events"
    ).fetchone()
    return row["last"] if row and row["last"] else None


def run_startup_catchup(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    terminal_status_names: frozenset[str] = frozenset(),
    client: ProactivanetClient | None = None,
) -> int:
    """
    Fetch incidents modified since the last stored event and store any new ones.

    `since` is a pre-snapshotted ISO timestamp watermark.  Pass it from the
    lifespan caller so the watermark is captured before the server starts
    accepting new webhooks — otherwise a webhook arriving between startup and
    catchup execution could advance the watermark and cause events to be missed.
    When None (e.g. in tests), falls back to querying the DB directly.

    Incidents whose Status field (lowercase) is in terminal_status_names are skipped —
    they are already closed/resolved/rejected and do not need evaluation.
    Note: PadStatus_id is always null in Proactivanet Incidents API responses; the
    Status string field is the only reliable terminal-state indicator.

    Returns the number of new events injected (duplicates are silently ignored).
    Logs a warning on API error but does not raise — a catchup failure must not
    prevent the service from starting.
    """
    if since is None:
        since = get_last_received_at(conn)

    if since is None:
        since = (
            datetime.now(timezone.utc) - timedelta(hours=_DEFAULT_LOOKBACK_HOURS)
        ).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        logger.info("Catchup: no prior events — looking back %dh", _DEFAULT_LOOKBACK_HOURS)
    else:
        logger.info("Catchup: fetching incidents modified since %s", since)

    _client = client or ProactivanetClient(settings)
    owned_client = client is None

    try:
        incidents = _client.get_incidents_modified_since(since)
    except Exception:
        logger.exception("Catchup: failed to fetch incidents from Proactivanet — skipping")
        return 0
    finally:
        if owned_client:
            _client.close()

    injected = 0
    skipped_terminal = 0
    for incident in incidents:
        ticket_id = str(incident.get("IncidentId") or incident.get("Id") or "")
        if not ticket_id:
            logger.warning("Catchup: incident with no ID — skipping %r", incident)
            continue

        status_str = str(incident.get("Status") or "").lower()
        if status_str and status_str in terminal_status_names:
            skipped_terminal += 1
            logger.debug(
                "Catchup: skipping terminal-state ticket=%s (Status=%s)",
                ticket_id, incident.get("Status"),
            )
            continue

        key = compute_idempotency_key(
            incident, ticket_id, CATCHUP_EVENT_TYPE, settings
        )
        if store_event(conn, key, ticket_id, CATCHUP_EVENT_TYPE, incident):
            injected += 1

    logger.info(
        "Catchup: %d new event(s) injected, %d skipped (terminal state), from %d incident(s) fetched",
        injected,
        skipped_terminal,
        len(incidents),
    )
    return injected
