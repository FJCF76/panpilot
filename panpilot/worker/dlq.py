"""
T4 — Dead-letter queue worker.

When the main worker fails to process an event it calls create_dlq_entry().
DLQThread runs as a daemon thread and retries each DLQ entry on a backoff
schedule until it succeeds or reaches MAX_ATTEMPTS, at which point the entry
is marked exhausted=1 and waits for admin review / manual retry (T19).

Retry schedule (between failures):
  attempt 1 → wait 30 s → attempt 2
  attempt 2 → wait 5 min → attempt 3
  attempt 3 → exhausted=1

The conn passed to DLQThread must have check_same_thread=False because
_process_due() runs in the DLQ thread, not the thread that created conn.
get_connection() in db/connection.py always sets this flag.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from panpilot.worker.exceptions import TicketBusy as _TicketBusy

logger = logging.getLogger(__name__)

# Delay in seconds before each re-attempt after failure N.
# Index 0 = delay after 1st failure, index 1 = delay after 2nd failure.
# After the 3rd failure (index 2 would never be read) the entry is exhausted.
RETRY_DELAYS_SECONDS: list[int] = [30, 300, 1800]  # 30 s, 5 min, 30 min
MAX_ATTEMPTS: int = 3


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def create_dlq_entry(
    conn: sqlite3.Connection,
    event_id: str,
    error: str,
) -> None:
    """
    Record a first-attempt failure in the DLQ.

    Called by the main worker thread immediately after a processing failure.
    Sets attempts=1 and schedules the first DLQ retry in RETRY_DELAYS_SECONDS[0]
    seconds. Commits the transaction before returning.
    """
    next_retry = (
        datetime.now(timezone.utc) + timedelta(seconds=RETRY_DELAYS_SECONDS[0])
    ).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    conn.execute(
        "INSERT INTO dlq (event_id, error, attempts, next_retry) VALUES (?, ?, 1, ?)",
        (event_id, str(error), next_retry),
    )
    conn.commit()
    logger.warning("DLQ: event_id=%s added (1st failure: %s)", event_id, error)


class DLQThread:
    """
    Background daemon thread that retries exhausted DLQ entries.

    process_fn must accept a dict (the full events row, payload already a
    string) and raise on failure. On success the function must mark the event
    as processed (events.processed=1) itself; the DLQThread only deletes the
    DLQ row after a successful call.

    poll_interval controls how often the thread wakes to check for due entries.
    Keep at 10 s in production; lower it in tests via a fast-path mock.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        process_fn: Callable[[dict], None],
        *,
        poll_interval: float = 10.0,
    ) -> None:
        self._conn = conn
        self._process_fn = process_fn
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="panpilot-dlq", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("DLQ thread started (poll_interval=%.0fs)", self._poll_interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)
        logger.info("DLQ thread stopped")

    def _run(self) -> None:
        while True:
            try:
                self._process_due()
            except Exception:
                logger.exception("DLQ poll error — will retry next tick")
            if self._stop.wait(self._poll_interval):
                break

    def _process_due(self) -> None:
        """Process all DLQ entries whose next_retry is in the past."""
        now = _utcnow_iso()
        rows = self._conn.execute(
            "SELECT id, event_id, attempts, error FROM dlq "
            "WHERE exhausted=0 AND (next_retry IS NULL OR next_retry <= ?) "
            "ORDER BY next_retry ASC LIMIT 10",
            (now,),
        ).fetchall()
        for row in rows:
            self._retry_one(dict(row))

    def _retry_one(self, dlq_row: dict) -> None:
        event_row = self._conn.execute(
            "SELECT id, ticket_id, event_type, payload, received_at, processed "
            "FROM events WHERE id = ?",
            (dlq_row["event_id"],),
        ).fetchone()

        if event_row is None:
            # Event was deleted externally; nothing to retry.
            logger.warning(
                "DLQ entry id=%s references missing event_id=%s — removing",
                dlq_row["id"],
                dlq_row["event_id"],
            )
            self._conn.execute("DELETE FROM dlq WHERE id = ?", (dlq_row["id"],))
            self._conn.commit()
            return

        try:
            self._process_fn(dict(event_row))
        except _TicketBusy:
            # T15: main worker is currently evaluating this ticket.
            # Leave the DLQ entry untouched — next poll will try again.
            logger.debug(
                "DLQ: ticket busy for event_id=%s — skipping (no backoff)",
                dlq_row["event_id"],
            )
            return
        except Exception as exc:
            new_attempts = dlq_row["attempts"] + 1
            if new_attempts >= MAX_ATTEMPTS:
                self._conn.execute(
                    "UPDATE dlq SET attempts=?, exhausted=1, next_retry=NULL, error=? "
                    "WHERE id=?",
                    (new_attempts, str(exc), dlq_row["id"]),
                )
                logger.error(
                    "DLQ exhausted: event_id=%s after %d attempts — needs admin review",
                    dlq_row["event_id"],
                    new_attempts,
                )
            else:
                delay = RETRY_DELAYS_SECONDS[new_attempts - 1]
                next_retry = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
                self._conn.execute(
                    "UPDATE dlq SET attempts=?, next_retry=?, error=? WHERE id=?",
                    (new_attempts, next_retry, str(exc), dlq_row["id"]),
                )
                logger.warning(
                    "DLQ retry failed: event_id=%s attempts=%d next_retry=%s",
                    dlq_row["event_id"],
                    new_attempts,
                    next_retry,
                )
            self._conn.commit()
            return

        # Success — process_fn marked event processed; remove the DLQ entry.
        self._conn.execute("DELETE FROM dlq WHERE id = ?", (dlq_row["id"],))
        self._conn.commit()
        logger.info(
            "DLQ retry succeeded: event_id=%s (was attempt %d)",
            dlq_row["event_id"],
            dlq_row["attempts"] + 1,
        )
