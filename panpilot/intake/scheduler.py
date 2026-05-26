"""
T6 — APScheduler stale ticket detector.

The stale detector runs every `stale_alert_poll_minutes` and looks for tickets
that have been inactive longer than the priority-specific threshold.  For each
stale ticket it builds a Decision(action="alert") directly — no Claude call —
and routes it through the standard execution layer so the audit log stays
complete.

States where stale alerting is suppressed (ticket is not actionable by agent
or has already been handled):
  PENDING_EVALUATION   — worker is currently processing it
  STALE_ALERT          — already notified; wait for agent to act or threshold to reset
  NEEDS_HUMAN          — escalated; agent is aware
  AWAITING_CLIENT_REPLY — waiting on the customer, not the agent

Repeat alerting is prevented by checking the audit_log: if the last "alert"
entry for a ticket is newer than its priority threshold, the ticket is skipped.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from panpilot.config import Settings, get_settings
from panpilot.db.connection import get_connection, main_db_path
from panpilot.execution.router import route
from panpilot.intelligence.models import Decision, TicketContext
from panpilot.intelligence.state_machine import apply_transition

logger = logging.getLogger(__name__)

# States where we never fire a stale alert (ticket is not agent-actionable).
_SKIP_STATES = frozenset({
    "PENDING_EVALUATION",
    "STALE_ALERT",
    "NEEDS_HUMAN",
    "AWAITING_CLIENT_REPLY",
    "AUTO_RESP",  # PanPilot already answered; no agent action pending on this ticket
})

# Default priority when ticket_state.priority is NULL (T9 not yet run).
_DEFAULT_PRIORITY = "P2"


def _threshold_for(priority: str | None, settings: Settings) -> timedelta:
    p = priority or _DEFAULT_PRIORITY
    if p == "P1":
        return timedelta(hours=settings.stale_threshold_p1_hours)
    if p == "P2":
        return timedelta(hours=settings.stale_threshold_p2_hours)
    return timedelta(hours=settings.stale_threshold_p3_hours)


def detect_stale_tickets(
    conn: sqlite3.Connection,
    settings: Settings,
    action_type_map: dict[str, str],
) -> int:
    """
    Detect stale tickets and route an alert Decision for each one.

    Returns the number of alert decisions routed.  The route() function handles
    DRY_RUN enforcement — no guard is needed here.

    Intended to be called from run_stale_detector() (the APScheduler job) and
    from tests (patch panpilot.intake.scheduler.route to avoid API calls).
    """
    now = datetime.now(timezone.utc)

    skip_placeholders = ",".join("?" * len(_SKIP_STATES))
    rows = conn.execute(
        f"SELECT ticket_id, state, priority, updated_at "
        f"FROM ticket_state "
        f"WHERE state NOT IN ({skip_placeholders})",
        tuple(_SKIP_STATES),
    ).fetchall()

    alerts_sent = 0

    for row in rows:
        ticket_id: str = row["ticket_id"]
        priority: str | None = row["priority"]
        threshold = _threshold_for(priority, settings)

        # Parse updated_at (SQLite stores with trailing 'Z')
        updated_at_raw: str = row["updated_at"]
        updated_at = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))

        if now - updated_at < threshold:
            continue  # not yet stale

        # Suppress repeat alerts: skip if we already alerted within this threshold window.
        last_alert = conn.execute(
            "SELECT evaluated_at FROM audit_log "
            "WHERE ticket_id=? AND action='alert' "
            "ORDER BY evaluated_at DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if last_alert is not None:
            last_alert_at = datetime.fromisoformat(
                last_alert["evaluated_at"].replace("Z", "+00:00")
            )
            if now - last_alert_at < threshold:
                continue  # already alerted recently enough

        effective_priority = priority or _DEFAULT_PRIORITY
        hours_stale = (now - updated_at).total_seconds() / 3600
        threshold_hours = int(threshold.total_seconds() // 3600)

        # Look up the human-readable ticket code from the most recent audit entry
        # so the stale alert shows the code in the admin UI instead of the UUID.
        code_row = conn.execute(
            "SELECT ticket_code FROM audit_log "
            "WHERE ticket_id=? AND ticket_code IS NOT NULL "
            "ORDER BY evaluated_at DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        ticket_code: str | None = code_row["ticket_code"] if code_row else None

        decision = Decision(
            action="alert",
            reasoning=(
                f"Ticket sin actividad durante {hours_stale:.1f}h "
                f"(umbral {effective_priority}: {threshold_hours}h)."
            ),
        )
        ctx = TicketContext(
            ticket_id=ticket_id,
            title="",
            description="",
            status=row["state"],
            priority=effective_priority,
            created_at=updated_at_raw,
            last_modified=updated_at_raw,
            awaiting_client_reply=False,
            ticket_code=ticket_code,
        )

        try:
            route(decision, ctx, settings, conn, action_type_map)
            apply_transition(conn, ticket_id, decision, effective_priority)
            alerts_sent += 1
            logger.info(
                "Stale alert: ticket=%s priority=%s inactive=%.1fh",
                ticket_id,
                effective_priority,
                hours_stale,
            )
        except Exception:
            logger.exception("Failed to route stale alert for ticket=%s", ticket_id)

    return alerts_sent


def run_stale_detector(action_type_map: dict[str, str]) -> None:
    """
    Module-level APScheduler job function.

    Must be a module-level function (not a closure) so APScheduler can
    serialize it by dotted import path when persisting jobs to the SQLite
    job store.  Creates its own DB connection because APScheduler runs
    jobs in a thread pool.
    """
    settings = get_settings()
    conn = get_connection(main_db_path(settings))
    try:
        count = detect_stale_tickets(conn, settings, action_type_map)
        if count:
            logger.info("Stale detector: %d alert(s) sent", count)
    except Exception:
        logger.exception("Stale detector job error")
    finally:
        conn.close()


def build_scheduler(
    settings: Settings,
    action_type_map: dict[str, str],
) -> BackgroundScheduler:
    """
    Build (but do not start) the APScheduler instance.

    Uses a SQLite job store so missed runs survive process restarts.
    coalesce=True ensures that if multiple stale-detector runs were missed
    (e.g. during downtime), only one catch-up run executes.

    Call scheduler.start() in the lifespan after this returns,
    and scheduler.shutdown(wait=False) at shutdown.
    """
    scheduler_db = settings.data_dir / "scheduler.db"
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    jobstores = {
        "default": SQLAlchemyJobStore(url=f"sqlite:///{scheduler_db}"),
    }
    job_defaults = {
        "coalesce": True,
        "misfire_grace_time": 60,
    }
    scheduler = BackgroundScheduler(
        jobstores=jobstores,
        job_defaults=job_defaults,
    )
    scheduler.add_job(
        run_stale_detector,
        "interval",
        minutes=settings.stale_alert_poll_minutes,
        id="stale_detector",
        replace_existing=True,
        kwargs={"action_type_map": action_type_map},
    )
    return scheduler
