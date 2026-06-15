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
  CLOSED_EXTERNALLY    — ticket deleted or terminal in Proactivanet; stop acting on it

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
from panpilot.execution.proactivanet import ProactivanetClient
from panpilot.execution.router import TicketNotFound, route
from panpilot.intelligence.caps import enforce_org_reminder_cap, enforce_reminder_cap
from panpilot.intelligence.models import Decision, TicketContext
from panpilot.intelligence.state_machine import apply_transition

logger = logging.getLogger(__name__)

# States where we never fire a stale alert (ticket is not agent-actionable).
_SKIP_STATES = frozenset({
    "PENDING_EVALUATION",
    "STALE_ALERT",
    "NEEDS_HUMAN",
    "AWAITING_CLIENT_REPLY",
    "AUTO_RESP",             # PanPilot already answered; no agent action pending on this ticket
    "CLOSED_EXTERNALLY",     # ticket deleted or terminal in Proactivanet
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


def _verify_or_close(
    conn: sqlite3.Connection,
    pn_client: ProactivanetClient,
    ticket_id: str,
    updated_at: datetime,
    terminal_status_names: frozenset[str],
    now: datetime,
) -> datetime | None:
    """
    Verify ticket still exists and is active in Proactivanet.

    Returns refreshed updated_at datetime if the ticket is active — use this
    value when re-evaluating the staleness/reminder threshold, as it may be
    newer than the local value (clock drift fix).

    Returns None if the ticket is deleted or in terminal state; local state
    is already updated to CLOSED_EXTERNALLY before returning.

    Known limitation: a 404 can also indicate a misconfigured API URL or a
    temporary permissions change, not just deletion. CLOSED_EXTERNALLY is
    permanent, so we log at WARNING to aid diagnosis.
    """
    try:
        ticket_data = pn_client.get_ticket(ticket_id)
    except Exception:
        # Transient network error — treat as active (assume ticket still exists).
        # Returning updated_at means the caller re-evaluates the threshold normally;
        # no state change is made, so the ticket stays in the queue for the next run.
        logger.warning("verify: get_ticket failed for ticket=%s — treating as active", ticket_id)
        return updated_at
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

    if ticket_data is None:
        conn.execute(
            "UPDATE ticket_state SET state='CLOSED_EXTERNALLY', updated_at=? "
            "WHERE ticket_id=?", (now_iso, ticket_id))
        conn.commit()
        logger.warning(
            "verify: ticket=%s not found in Proactivanet (deleted or unreachable) "
            "— marked CLOSED_EXTERNALLY", ticket_id)
        return None

    if not isinstance(ticket_data, dict):
        logger.warning(
            "verify: unexpected get_ticket response type %s for ticket=%s — treating as active",
            type(ticket_data).__name__, ticket_id)
        return updated_at

    pn_status = str(ticket_data.get("Status") or "").lower()
    if pn_status in terminal_status_names:
        conn.execute(
            "UPDATE ticket_state SET state='CLOSED_EXTERNALLY', updated_at=? "
            "WHERE ticket_id=?", (now_iso, ticket_id))
        conn.commit()
        logger.info(
            "verify: ticket=%s terminal status '%s' — marked CLOSED_EXTERNALLY",
            ticket_id, pn_status)
        return None

    # Clock drift fix: if Proactivanet DateLastModified is more recent than local
    # updated_at, refresh the local timestamp and return the newer value so the
    # caller can re-evaluate whether the threshold is actually exceeded.
    pn_modified_raw = ticket_data.get("DateLastModified")
    if pn_modified_raw:
        try:
            pn_modified = datetime.fromisoformat(pn_modified_raw.replace("Z", "+00:00"))
            if pn_modified > updated_at:
                conn.execute(
                    "UPDATE ticket_state SET updated_at=? WHERE ticket_id=?",
                    (pn_modified_raw, ticket_id))
                conn.commit()
                return pn_modified  # rebind: caller re-checks threshold with this value
        except (ValueError, TypeError):
            pass  # malformed or naive DateLastModified — ignore, proceed with local timestamp

    return updated_at  # unchanged


def detect_stale_tickets(
    conn: sqlite3.Connection,
    settings: Settings,
    action_type_map: dict[str, str],
    terminal_status_names: frozenset[str] = frozenset(),
    *,
    proactivanet_client: ProactivanetClient | None = None,
) -> int:
    """
    Detect stale tickets and route an alert Decision for each one.

    Returns the number of alert decisions routed.  The route() function handles
    DRY_RUN enforcement — no guard is needed here.

    terminal_status_names: frozenset of lowercase Proactivanet status names that
    indicate a ticket is closed/resolved/rejected. Tickets in these states are
    marked CLOSED_EXTERNALLY and skipped. Loaded from reference_data at startup;
    frozen at job-registration time (restart required if Proactivanet renames
    statuses, same as action_type_map).

    proactivanet_client: inject a mock for tests. If None, a real client is
    created lazily when the first candidate ticket survives both local filters.

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
    _pn_client: ProactivanetClient | None = None

    try:
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

            # Pre-verify: only GET for tickets that passed both local filters.
            # Client opens lazily — not created if no ticket survives to this point.
            if _pn_client is None:
                _pn_client = proactivanet_client or ProactivanetClient(settings)
            refreshed_at = _verify_or_close(
                conn, _pn_client, ticket_id, updated_at, terminal_status_names, now)
            if refreshed_at is None:
                continue  # deleted or terminal — CLOSED_EXTERNALLY already set
            if now - refreshed_at < threshold:
                continue  # not stale after clock refresh

            effective_priority = priority or _DEFAULT_PRIORITY
            hours_stale = (now - refreshed_at).total_seconds() / 3600
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
            except TicketNotFound:
                pass  # CLOSED_EXTERNALLY set inside route(); skip apply_transition
            except Exception:
                logger.exception("Failed to route stale alert for ticket=%s", ticket_id)
    finally:
        if _pn_client is not None and proactivanet_client is None:
            _pn_client.close()

    return alerts_sent


def run_stale_detector(
    action_type_map: dict[str, str],
    terminal_status_names: frozenset[str] = frozenset(),
) -> None:
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
        count = detect_stale_tickets(conn, settings, action_type_map, terminal_status_names)
        if count:
            logger.info("Stale detector: %d alert(s) sent", count)
    except Exception:
        logger.exception("Stale detector job error")
    finally:
        conn.close()


_REMINDER_RESPONSE_DRAFT = (
    "Estimado cliente, le contactamos para recordarle que su solicitud "
    "de soporte sigue pendiente de su respuesta. En cuanto podamos "
    "recibir la información necesaria, continuaremos con la gestión de "
    "su ticket. Gracias por su atención."
)


def send_proactive_reminders(
    conn: sqlite3.Connection,
    settings: Settings,
    action_type_map: dict[str, str],
    terminal_status_names: frozenset[str] = frozenset(),
    *,
    proactivanet_client: ProactivanetClient | None = None,
) -> int:
    """
    Find WAITING tickets past the reminder threshold and send proactive reminders.

    Returns the number of reminders sent.  DRY_RUN is enforced by route().

    terminal_status_names and proactivanet_client: same semantics as
    detect_stale_tickets() — see that function's docstring.

    De-duplication: apply_transition() resets updated_at on every call including
    after sending a reminder, so the updated_at check is only a quick pre-filter.
    The audit-log guard (last remind entry) is the authoritative repeat suppressor.
    """
    now = datetime.now(timezone.utc)
    threshold = timedelta(hours=settings.reminder_threshold_hours)

    rows = conn.execute(
        "SELECT ticket_id, priority, updated_at, reminder_count, requester_id "
        "FROM ticket_state WHERE state = 'WAITING'",
    ).fetchall()

    reminders_sent = 0
    _pn_client: ProactivanetClient | None = None

    try:
        for row in rows:
            ticket_id: str = row["ticket_id"]
            updated_at_raw: str = row["updated_at"]
            updated_at = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))

            if now - updated_at < threshold:
                continue

            # Audit-log de-duplication: skip if already reminded within this threshold window.
            last_remind = conn.execute(
                "SELECT evaluated_at FROM audit_log "
                "WHERE ticket_id=? AND action='remind' "
                "ORDER BY evaluated_at DESC LIMIT 1",
                (ticket_id,),
            ).fetchone()
            if last_remind:
                last_remind_at = datetime.fromisoformat(
                    last_remind["evaluated_at"].replace("Z", "+00:00")
                )
                if now - last_remind_at < threshold:
                    continue

            # Pre-verify: only GET for tickets that passed both local filters.
            if _pn_client is None:
                _pn_client = proactivanet_client or ProactivanetClient(settings)
            refreshed_at = _verify_or_close(
                conn, _pn_client, ticket_id, updated_at, terminal_status_names, now)
            if refreshed_at is None:
                continue  # deleted or terminal — CLOSED_EXTERNALLY already set
            if now - refreshed_at < threshold:
                continue  # not overdue after clock refresh

            hours_inactive = (now - refreshed_at).total_seconds() / 3600
            decision = Decision(
                action="remind",
                reasoning=(
                    f"El ticket lleva {hours_inactive:.0f}h sin respuesta del cliente. "
                    "Se envía recordatorio proactivo."
                ),
                response_draft=_REMINDER_RESPONSE_DRAFT,
            )

            decision = enforce_reminder_cap(conn, ticket_id, decision, settings)
            requester_id: str | None = row["requester_id"]
            decision = enforce_org_reminder_cap(conn, ticket_id, requester_id, decision, settings)

            code_row = conn.execute(
                "SELECT ticket_code FROM audit_log "
                "WHERE ticket_id=? AND ticket_code IS NOT NULL "
                "ORDER BY evaluated_at DESC LIMIT 1",
                (ticket_id,),
            ).fetchone()
            ticket_code: str | None = code_row["ticket_code"] if code_row else None

            ctx = TicketContext(
                ticket_id=ticket_id,
                title="",
                description="",
                status="WAITING",
                priority=row["priority"] or _DEFAULT_PRIORITY,
                # updated_at approximates the tech-contact or last-reminder time.
                created_at=updated_at_raw,
                last_modified=updated_at_raw,
                awaiting_client_reply=True,
                ticket_code=ticket_code,
            )

            try:
                route(decision, ctx, settings, conn, action_type_map)
                apply_transition(conn, ticket_id, decision, ctx.priority)
                logger.info(
                    "Proactive reminder: ticket=%s inactive=%.1fh",
                    ticket_id,
                    hours_inactive,
                )
                reminders_sent += 1
            except TicketNotFound:
                pass  # CLOSED_EXTERNALLY set inside route(); skip apply_transition
            except Exception:
                logger.exception("Failed to send proactive reminder for ticket=%s", ticket_id)
    finally:
        if _pn_client is not None and proactivanet_client is None:
            _pn_client.close()

    return reminders_sent


def run_reminder_scheduler(
    action_type_map: dict[str, str],
    terminal_status_names: frozenset[str] = frozenset(),
) -> None:
    """
    Module-level APScheduler job function for proactive reminders (H18 Gap 2).

    Must be module-level (not a closure) so APScheduler can serialise it by
    dotted import path when persisting to the SQLite job store.
    """
    settings = get_settings()
    conn = get_connection(main_db_path(settings))
    try:
        count = send_proactive_reminders(conn, settings, action_type_map, terminal_status_names)
        if count:
            logger.info("Reminder scheduler: %d reminder(s) sent", count)
    except Exception:
        logger.exception("Reminder scheduler job error")
    finally:
        conn.close()


def build_scheduler(
    settings: Settings,
    action_type_map: dict[str, str],
    terminal_status_names: frozenset[str] = frozenset(),
) -> BackgroundScheduler:
    """
    Build (but do not start) the APScheduler instance.

    Uses a SQLite job store so missed runs survive process restarts.
    coalesce=True ensures that if multiple stale-detector runs were missed
    (e.g. during downtime), only one catch-up run executes.

    terminal_status_names is frozen at job-registration time (startup). If
    Proactivanet renames status values, a service restart is required to pick
    up the change — same behavior as action_type_map.

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
        kwargs={"action_type_map": action_type_map, "terminal_status_names": terminal_status_names},
    )
    scheduler.add_job(
        run_reminder_scheduler,
        "interval",
        hours=settings.reminder_poll_hours,
        id="reminder_scheduler",
        replace_existing=True,
        kwargs={"action_type_map": action_type_map, "terminal_status_names": terminal_status_names},
    )
    return scheduler
