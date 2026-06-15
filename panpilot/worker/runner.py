"""
Main worker polling thread + T15 race-condition guard.

WorkerThread polls the events table for unprocessed events and runs each
one through the full evaluation pipeline:

  _try_mark_pending     (T15/T9) — atomically claim ticket before Claude call
  parse_ticket_context            — resolve UUIDs → readable labels
  evaluate_ticket           (T1) — single Claude call → Decision
  enforce_clarification_cap (T11)— override if clarify cap reached
  route                     (T3) — write audit + post annotation
  apply_transition          (T9) — update ticket_state to final state

T15 — Race-condition guard
--------------------------
The DLQ thread (T4) can retry events concurrently with the main worker.
_try_mark_pending() performs an atomic check-and-set at the SQLite level:

  UPDATE ticket_state SET state='PENDING_EVALUATION' WHERE state != 'PENDING_EVALUATION'
  + INSERT if no row exists yet

SQLite serializes all writes, so exactly one thread gets rowcount > 0 and
claims the ticket.  The other gets rowcount == 0 (or IntegrityError on the
INSERT path) → TicketBusy is raised:
  • Main worker loop: leaves the event unprocessed (retried next poll).
  • DLQ _retry_one: leaves the DLQ entry untouched (retried next poll).

Failure handling
----------------
Any exception other than TicketBusy:
  1. create_dlq_entry()  — schedule for retry at 30 s / 5 min / 30 min
  2. mark_event_processed() — prevents the main worker from re-picking it up
     (DLQ thread accesses events directly by ID, ignoring processed flag)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any

import anthropic

from panpilot.config import Settings
from panpilot.execution.proactivanet import ProactivanetClient
from panpilot.execution.router import route
from panpilot.intelligence.caps import enforce_clarification_cap, enforce_org_reminder_cap, enforce_reminder_cap
from panpilot.intelligence.engine import evaluate_ticket
from panpilot.intelligence.models import Decision, TicketContext
from panpilot.intelligence.rag import RagDeps, rag_evaluate
from panpilot.intelligence.state_machine import apply_transition
from panpilot.intake.event_store import claim_next_event, mark_event_processed
from panpilot.worker.dlq import create_dlq_entry
from panpilot.execution.router import TicketNotFound
from panpilot.worker.exceptions import TicketBusy

logger = logging.getLogger(__name__)


def _try_mark_pending(conn: sqlite3.Connection, ticket_id: str, priority: str) -> bool:
    """
    Atomically claim a ticket for PENDING_EVALUATION.

    Returns True if this call successfully set the ticket to PENDING_EVALUATION.
    Returns False if the ticket was already in PENDING_EVALUATION (another thread
    owns it — caller should raise TicketBusy).

    SQLite serializes all writes, so the UPDATE + INSERT sequence is safe:
    - UPDATE changes non-PENDING rows; rowcount > 0 → we claimed it.
    - If rowcount == 0 (no row, or row is already PENDING), try INSERT.
    - INSERT succeeds on a new ticket → we claimed it.
    - INSERT raises IntegrityError on a duplicate primary key, which means the row
      exists with state == PENDING_EVALUATION → another thread owns it.
    """
    cursor = conn.execute(
        "UPDATE ticket_state "
        "SET state='PENDING_EVALUATION', priority=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE ticket_id=? AND state!='PENDING_EVALUATION'",
        (priority, ticket_id),
    )
    if cursor.rowcount > 0:
        conn.commit()
        return True

    try:
        conn.execute(
            "INSERT INTO ticket_state "
            "(ticket_id, state, priority, updated_at, clarification_count, reminder_count) "
            "VALUES (?, 'PENDING_EVALUATION', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), 0, 0)",
            (ticket_id, priority),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Row exists with state == PENDING_EVALUATION — another thread claimed it.
        conn.rollback()
        return False


def parse_ticket_context(
    payload: dict[str, Any],
    ticket_id: str,
    priority_map: dict[str, str],
    status_map: dict[str, str],
) -> TicketContext:
    """
    Build a TicketContext from a raw Proactivanet event payload.

    UUID foreign keys (PadPriorities_id, PadStatus_id) are resolved to
    readable labels using the maps loaded at startup.  Unknown UUIDs fall
    back to safe defaults so the worker never crashes on unexpected data.
    """
    # PadPriorities_id can be null in some API responses; treat null as unknown → P3.
    priority_uuid = payload.get("PadPriorities_id") or ""
    priority = priority_map.get(priority_uuid, "P3")

    status_uuid = payload.get("PadStatus_id") or ""
    status = status_map.get(status_uuid, "Unknown")

    # T17: requester identity — prefer PanUsers_idSource, fall back to PadCustomers_id.
    requester_id = (
        (payload.get("PanUsers_idSource") or payload.get("PadCustomers_id") or "")
        .strip() or None
    )
    if requester_id:
        requester_id = requester_id[:128]

    return TicketContext(
        ticket_id=ticket_id,
        title=payload.get("Title", ""),
        description=payload.get("Description", ""),
        status=status,
        priority=priority,
        created_at=payload.get("DateCreated", ""),
        last_modified=payload.get("DateLastModified", ""),
        awaiting_client_reply=bool(payload.get("RequestedUserComments", False)),
        ticket_code=payload.get("Code"),
        requester_id=requester_id,
    )


def _check_manual_exclusion(ctx: TicketContext, settings: Settings) -> bool:
    """
    Return True if the ticket carries the manual-exclusion marker.

    When MANUAL_EXCLUSION_FIELD_ID is set, that custom Proactivanet field is
    authoritative (not yet implemented — requires the admin to create the field
    first).  When empty (the default), the text-marker fallback applies:
    any ticket whose Description contains "[panpilot-manual]" (case-insensitive)
    is excluded from automated evaluation.
    """
    if settings.manual_exclusion_field_id:
        # Custom field path: deferred to Phase 2 activation (T13).
        return False
    return "[panpilot-manual]" in (ctx.description or "").lower()


def process_event(
    event: dict[str, Any],
    settings: Settings,
    conn: sqlite3.Connection,
    priority_map: dict[str, str],
    status_map: dict[str, str],
    action_type_map: dict[str, str],
    *,
    terminal_status_names: frozenset[str] = frozenset(),
    proactivanet_client: ProactivanetClient | None = None,
    anthropic_client: anthropic.Anthropic | None = None,
    rag_deps: RagDeps | None = None,
) -> None:
    """
    Run one event through the full evaluation pipeline.

    Raises TicketBusy if the ticket is already being evaluated (T15).
    Raises any other exception on unrecoverable pipeline failure.

    The payload field in event must already be a dict (claim_next_event
    deserialises JSON; DLQ _retry_one passes the raw events row which has
    payload as a JSON string — callers must deserialise before calling).
    """
    ticket_id: str = event["ticket_id"]

    payload = event["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)

    # Skip tickets already in a terminal state (closed/resolved/rejected).
    # Returning normally here causes _handle() to mark the event processed,
    # so it is never re-evaluated.
    # Note: PadStatus_id is always null in Proactivanet Incidents API responses;
    # the Status string field is the only reliable terminal-state indicator.
    status_str = str(payload.get("Status") or "").lower()
    if status_str and status_str in terminal_status_names:
        logger.info(
            "Worker: skipping terminal-state ticket=%s (Status=%s)",
            ticket_id, payload.get("Status"),
        )
        return

    # Skip tickets that PanPilot has already handed off or escalated.
    # STALE_ALERT — alert was sent; if the ticket is still stale the scheduler
    #   will re-fire after the threshold window, not on every Guardado update.
    # NEEDS_HUMAN — PanPilot explicitly stepped back; re-evaluating on a
    #   Guardado caused by our own annotation would undo that decision.
    # This prevents the Guardado self-trigger loop: posting an annotation may
    # update DateLastModified → Proactivanet fires Guardado → unique key → stored.
    # Without this guard, the worker would call Claude again on that Guardado
    # and could post another annotation, repeating indefinitely.
    _state_row = conn.execute(
        "SELECT state FROM ticket_state WHERE ticket_id=?", (ticket_id,)
    ).fetchone()
    _current_state: str | None = _state_row["state"] if _state_row else None
    if _current_state in {"STALE_ALERT", "NEEDS_HUMAN", "CLOSED_EXTERNALLY"}:
        logger.info(
            "Worker: skipping ticket=%s in state=%s (not actionable)",
            ticket_id, _current_state,
        )
        return

    # Self-trigger guard for annotation-driven states.
    #
    # CLR_REQ: posting a UserTextQuestion annotation sets RequestedUserComments=True in
    # Proactivanet.  A Guardado arriving with RequestedUserComments=True is the self-trigger
    # (PanPilot's question reflected back) — skip it.  When the customer replies,
    # RequestedUserComments goes False and the Guardado is processed normally.
    if _current_state == "CLR_REQ" and bool(payload.get("RequestedUserComments")):
        logger.info(
            "Worker: skipping CLR_REQ self-trigger for ticket=%s (awaiting_client_reply=True)",
            ticket_id,
        )
        return

    # AUTO_RESP: posting an AutomaticResponse annotation does NOT set RequestedUserComments=True
    # in Proactivanet (it is an informational reply, not a question).  Every subsequent Guardado
    # therefore arrives with RequestedUserComments=False regardless of whether it is PanPilot's
    # own self-trigger or a customer update — there is no reliable signal to distinguish the two.
    # Always skip when in AUTO_RESP to prevent the infinite annotation loop observed in
    # production (REQ 2026-000016 received 5 identical auto_respond annotations).
    if _current_state == "AUTO_RESP":
        logger.info(
            "Worker: skipping AUTO_RESP self-trigger for ticket=%s",
            ticket_id,
        )
        return

    ctx = parse_ticket_context(payload, ticket_id, priority_map, status_map)

    # T15: atomic check-and-set — exactly one thread claims the ticket.
    if not _try_mark_pending(conn, ticket_id, ctx.priority):
        raise TicketBusy(f"ticket {ticket_id!r} is already in PENDING_EVALUATION")

    # T13: manual exclusion — skip Claude entirely, write audit, resolve state
    if _check_manual_exclusion(ctx, settings):
        logger.info("ticket=%s manually excluded ([panpilot-manual] marker)", ticket_id)
        _excl = Decision(
            action="none",
            reasoning="Ticket excluido manualmente de PanPilot.",
            none_reason="no_action_warranted",
        )
        route(_excl, ctx, settings, conn, action_type_map,
              proactivanet_client=proactivanet_client, anthropic_client=anthropic_client)
        apply_transition(conn, ticket_id, _excl, ctx.priority)
        return

    decision = evaluate_ticket(ctx, settings, client=anthropic_client)

    # RAG Pass 2: enrich auto_respond with retrieved documentation context
    if decision.action == "auto_respond" and rag_deps is not None and rag_deps.available:
        decision = rag_evaluate(ctx, rag_deps, settings, conn, anthropic_client)

    # T11: override clarify if cap reached
    decision = enforce_clarification_cap(conn, ticket_id, decision, settings)

    # T16: override remind if cap reached
    decision = enforce_reminder_cap(conn, ticket_id, decision, settings)

    # T17: override remind if org-level cap reached
    decision = enforce_org_reminder_cap(conn, ticket_id, ctx.requester_id, decision, settings)

    route(decision, ctx, settings, conn, action_type_map,
          proactivanet_client=proactivanet_client, anthropic_client=anthropic_client)

    apply_transition(conn, ticket_id, decision, ctx.priority, requester_id=ctx.requester_id)


class WorkerThread:
    """
    DB-backed event processing loop (Step 4 in the lifespan).

    Polls events WHERE processed=0, calls process_event for each, and sends
    failures to the DLQ.  Designed for a single-worker deployment — the
    APScheduler constraint already requires --workers 1.

    poll_interval: seconds to sleep when the queue is empty.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        priority_map: dict[str, str],
        status_map: dict[str, str],
        action_type_map: dict[str, str],
        *,
        terminal_status_names: frozenset[str] = frozenset(),
        anthropic_client: anthropic.Anthropic | None = None,
        rag_deps: RagDeps | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._priority_map = priority_map
        self._status_map = status_map
        self._action_type_map = action_type_map
        self._terminal_status_names = terminal_status_names
        self._anthropic_client = anthropic_client
        self._rag_deps = rag_deps
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="panpilot-worker", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("Worker thread started (poll_interval=%.0fs)", self._poll_interval)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)
        logger.info("Worker thread stopped")

    def _run(self) -> None:
        while True:
            try:
                self._drain()
            except Exception:
                logger.exception("Worker poll error — will retry next tick")
            if self._stop.wait(self._poll_interval):
                break

    def _drain(self) -> None:
        """Process events until the queue is empty."""
        while not self._stop.is_set():
            event = claim_next_event(self._conn)
            if event is None:
                return
            self._handle(event)

    def _handle(self, event: dict[str, Any]) -> None:
        ticket_id = event["ticket_id"]
        event_id = event["id"]
        try:
            process_event(
                event,
                self._settings,
                self._conn,
                self._priority_map,
                self._status_map,
                self._action_type_map,
                terminal_status_names=self._terminal_status_names,
                anthropic_client=self._anthropic_client,
                rag_deps=self._rag_deps,
            )
            mark_event_processed(self._conn, event_id)
            logger.info("Worker: processed event_id=%s ticket=%s", event_id, ticket_id)
        except TicketBusy:
            # T15: another thread is evaluating this ticket — leave event in queue.
            logger.debug(
                "Worker: ticket=%s busy, skipping event_id=%s (will retry)",
                ticket_id,
                event_id,
            )
        except TicketNotFound:
            # Ticket was deleted in Proactivanet; local state already CLOSED_EXTERNALLY.
            # Mark the event processed without creating a DLQ entry — retrying would
            # just hit the same 404 indefinitely.
            logger.warning(
                "Worker: ticket=%s deleted (TicketNotFound) for event_id=%s — marking processed",
                ticket_id,
                event_id,
            )
            mark_event_processed(self._conn, event_id)
        except Exception as exc:
            logger.exception(
                "Worker: event_id=%s ticket=%s failed — sending to DLQ", event_id, ticket_id
            )
            create_dlq_entry(self._conn, event_id, str(exc))
            mark_event_processed(self._conn, event_id)
