"""Tests for the main worker — T15 race guard, parse_ticket_context, process_event, WorkerThread."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from panpilot.config import get_settings
from panpilot.intelligence.models import Decision
from panpilot.intelligence.rag import RagDeps
from panpilot.worker.exceptions import TicketBusy
from panpilot.worker.runner import WorkerThread, parse_ticket_context, process_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    schema = (Path(__file__).parent.parent / "panpilot" / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    return conn


_PRIORITY_MAP = {"uuid-p1": "P1", "uuid-p2": "P2", "uuid-p3": "P3"}
_STATUS_MAP = {"uuid-s1": "New", "uuid-s2": "Assigned"}
_ACTION_TYPE_MAP = {
    "Annotation": "uuid-annotation",
    "UserTextQuestion": "uuid-clarify",
    "AutomaticResponse": "uuid-auto",
    "PublishedAction": "uuid-remind",
}


def _payload(
    priority_uuid: str = "uuid-p2",
    status_uuid: str = "uuid-s2",
    **kwargs,
) -> dict:
    return {
        "Title": "Test ticket",
        "Description": "Something broken.",
        "PadPriorities_id": priority_uuid,
        "PadStatus_id": status_uuid,
        "DateCreated": "2026-05-25T08:00:00Z",
        "DateLastModified": "2026-05-25T09:00:00Z",
        "RequestedUserComments": False,
        **kwargs,
    }


def _event(ticket_id: str = "TKT-001", payload: dict | None = None) -> dict:
    return {
        "id": f"evt-{ticket_id}",
        "ticket_id": ticket_id,
        "event_type": "Guardado",
        "payload": payload or _payload(),
        "received_at": "2026-05-25T09:00:00Z",
    }


def _insert_event(conn: sqlite3.Connection, ticket_id: str = "TKT-001") -> None:
    p = json.dumps(_payload())
    conn.execute(
        "INSERT INTO events (id, ticket_id, event_type, payload, processed) "
        "VALUES (?, ?, 'Guardado', ?, 0)",
        (f"evt-{ticket_id}", ticket_id, p),
    )
    conn.commit()


def _mock_decision(action: str = "none") -> Decision:
    kwargs: dict = {"action": action, "reasoning": "test"}
    if action in {"auto_respond", "remind"}:
        kwargs["response_draft"] = "resp"
    if action == "none":
        kwargs["none_reason"] = "no_action_warranted"
    return Decision(**kwargs)


# ---------------------------------------------------------------------------
# parse_ticket_context
# ---------------------------------------------------------------------------

class TestParseTicketContext:

    def test_resolves_priority_from_map(self):
        ctx = parse_ticket_context(_payload("uuid-p1"), "TKT-1", _PRIORITY_MAP, _STATUS_MAP)
        assert ctx.priority == "P1"

    def test_resolves_status_from_map(self):
        ctx = parse_ticket_context(_payload(status_uuid="uuid-s1"), "TKT-1", _PRIORITY_MAP, _STATUS_MAP)
        assert ctx.status == "New"

    def test_unknown_priority_uuid_defaults_to_p3(self):
        ctx = parse_ticket_context(_payload("unknown-uuid"), "TKT-1", _PRIORITY_MAP, _STATUS_MAP)
        assert ctx.priority == "P3"

    def test_unknown_status_uuid_defaults_to_unknown(self):
        ctx = parse_ticket_context(_payload(status_uuid="unknown"), "TKT-1", _PRIORITY_MAP, _STATUS_MAP)
        assert ctx.status == "Unknown"

    def test_ticket_id_set_correctly(self):
        ctx = parse_ticket_context(_payload(), "TKT-XYZ", _PRIORITY_MAP, _STATUS_MAP)
        assert ctx.ticket_id == "TKT-XYZ"

    def test_title_extracted(self):
        ctx = parse_ticket_context(_payload(), "TKT-1", _PRIORITY_MAP, _STATUS_MAP)
        assert ctx.title == "Test ticket"

    def test_description_extracted(self):
        ctx = parse_ticket_context(_payload(), "TKT-1", _PRIORITY_MAP, _STATUS_MAP)
        assert ctx.description == "Something broken."

    def test_awaiting_client_reply_false(self):
        ctx = parse_ticket_context(_payload(RequestedUserComments=False), "TKT-1", _PRIORITY_MAP, _STATUS_MAP)
        assert ctx.awaiting_client_reply is False

    def test_awaiting_client_reply_true(self):
        ctx = parse_ticket_context(_payload(RequestedUserComments=True), "TKT-1", _PRIORITY_MAP, _STATUS_MAP)
        assert ctx.awaiting_client_reply is True

    def test_missing_fields_use_empty_defaults(self):
        ctx = parse_ticket_context({}, "TKT-1", _PRIORITY_MAP, _STATUS_MAP)
        assert ctx.title == ""
        assert ctx.description == ""
        assert ctx.created_at == ""
        assert ctx.last_modified == ""


# ---------------------------------------------------------------------------
# T17 — requester_id extraction in parse_ticket_context
# ---------------------------------------------------------------------------

class TestRequesterIdExtraction:

    def test_pan_users_id_source_extracted(self):
        ctx = parse_ticket_context(
            _payload(PanUsers_idSource="usr-abc"), "TKT-1", _PRIORITY_MAP, _STATUS_MAP
        )
        assert ctx.requester_id == "usr-abc"

    def test_pad_customers_id_fallback(self):
        ctx = parse_ticket_context(
            _payload(PadCustomers_id="cust-xyz"), "TKT-1", _PRIORITY_MAP, _STATUS_MAP
        )
        assert ctx.requester_id == "cust-xyz"

    def test_pan_users_preferred_over_pad_customers(self):
        ctx = parse_ticket_context(
            _payload(PanUsers_idSource="usr-abc", PadCustomers_id="cust-xyz"),
            "TKT-1", _PRIORITY_MAP, _STATUS_MAP,
        )
        assert ctx.requester_id == "usr-abc"

    def test_both_absent_gives_none(self):
        ctx = parse_ticket_context(_payload(), "TKT-1", _PRIORITY_MAP, _STATUS_MAP)
        assert ctx.requester_id is None

    def test_empty_string_treated_as_none(self):
        ctx = parse_ticket_context(
            _payload(PanUsers_idSource="", PadCustomers_id=""),
            "TKT-1", _PRIORITY_MAP, _STATUS_MAP,
        )
        assert ctx.requester_id is None

    def test_whitespace_only_treated_as_none(self):
        ctx = parse_ticket_context(
            _payload(PanUsers_idSource="   "),
            "TKT-1", _PRIORITY_MAP, _STATUS_MAP,
        )
        assert ctx.requester_id is None

    def test_requester_id_stripped_of_whitespace(self):
        ctx = parse_ticket_context(
            _payload(PanUsers_idSource="  usr-abc  "),
            "TKT-1", _PRIORITY_MAP, _STATUS_MAP,
        )
        assert ctx.requester_id == "usr-abc"


# ---------------------------------------------------------------------------
# process_event — T15 race condition guard
# ---------------------------------------------------------------------------

class TestT15RaceGuard:

    def test_raises_ticket_busy_when_pending(self):
        conn = _conn()
        # Force ticket into PENDING_EVALUATION
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority) "
            "VALUES ('TKT-001', 'PENDING_EVALUATION', 'P2')"
        )
        conn.commit()
        with pytest.raises(TicketBusy):
            process_event(
                _event(), get_settings(), conn,
                _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
            )

    def test_does_not_raise_when_not_pending(self):
        conn = _conn()
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority) "
            "VALUES ('TKT-001', 'WAITING', 'P2')"
        )
        conn.commit()
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=_mock_decision("none")):
            with patch("panpilot.worker.runner.route"):
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )

    def test_no_prior_state_processes_normally(self):
        conn = _conn()
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=_mock_decision("none")):
            with patch("panpilot.worker.runner.route"):
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )


# ---------------------------------------------------------------------------
# process_event — pipeline execution
# ---------------------------------------------------------------------------

class TestProcessEvent:

    def _run(self, conn, decision=None, action="none"):
        d = decision or _mock_decision(action)
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=d) as mock_eval:
            with patch("panpilot.worker.runner.route") as mock_route:
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        return mock_eval, mock_route

    def test_evaluate_ticket_called_once(self):
        conn = _conn()
        mock_eval, _ = self._run(conn)
        mock_eval.assert_called_once()

    def test_route_called_once(self):
        conn = _conn()
        _, mock_route = self._run(conn)
        mock_route.assert_called_once()

    def test_ticket_state_set_after_processing(self):
        conn = _conn()
        self._run(conn, action="clarify")
        row = conn.execute(
            "SELECT state FROM ticket_state WHERE ticket_id='TKT-001'"
        ).fetchone()
        assert row is not None
        assert row["state"] == "CLR_REQ"

    def test_payload_as_string_is_deserialized(self):
        conn = _conn()
        evt = _event()
        evt["payload"] = json.dumps(evt["payload"])  # simulate DLQ row format
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=_mock_decision()) as m:
            with patch("panpilot.worker.runner.route"):
                process_event(evt, get_settings(), conn, _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP)
        ctx = m.call_args[0][0]
        assert ctx.title == "Test ticket"

    def test_clarification_cap_applied(self):
        conn = _conn()
        # Set clarification_count to max (2)
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority, clarification_count) "
            "VALUES ('TKT-001', 'CLR_REQ', 'P2', 2)"
        )
        conn.commit()
        clarify = _mock_decision("clarify")
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=clarify):
            with patch("panpilot.worker.runner.route") as mock_route:
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        _, kwargs = mock_route.call_args
        # Route receives decision; positional arg 0 is the (possibly overridden) decision
        routed_decision = mock_route.call_args[0][0]
        assert routed_decision.action == "none"
        assert routed_decision.none_reason == "needs_human"


# ---------------------------------------------------------------------------
# WorkerThread — lifecycle
# ---------------------------------------------------------------------------

class TestWorkerThread:

    def test_starts_and_stops(self):
        conn = _conn()
        wt = WorkerThread(conn, get_settings(), _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP, poll_interval=0.05)
        wt.start()
        assert wt._thread.is_alive()
        wt.stop(timeout=1.0)
        assert not wt._thread.is_alive()

    def test_processes_queued_event(self):
        conn = _conn()
        _insert_event(conn, "TKT-001")
        processed = threading.Event()

        def _fake_process(event, settings, c, pm, sm, am, **kw):
            processed.set()

        with patch("panpilot.worker.runner.process_event", side_effect=_fake_process):
            wt = WorkerThread(conn, get_settings(), _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP, poll_interval=0.05)
            wt.start()
            assert processed.wait(timeout=2.0), "Worker did not process event within 2s"
            wt.stop(timeout=1.0)

    def test_failed_event_goes_to_dlq(self):
        conn = _conn()
        _insert_event(conn, "TKT-001")

        def _fail(event, *a, **kw):
            raise RuntimeError("evaluation failed")

        with patch("panpilot.worker.runner.process_event", side_effect=_fail):
            wt = WorkerThread(conn, get_settings(), _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP, poll_interval=0.05)
            wt.start()
            # Give the worker time to process and DLQ it
            import time; time.sleep(0.3)
            wt.stop(timeout=1.0)

        dlq_row = conn.execute("SELECT * FROM dlq").fetchone()
        assert dlq_row is not None
        assert dlq_row["event_id"] == "evt-TKT-001"

    def test_failed_event_is_marked_processed(self):
        conn = _conn()
        _insert_event(conn, "TKT-001")

        def _fail(event, *a, **kw):
            raise RuntimeError("fail")

        with patch("panpilot.worker.runner.process_event", side_effect=_fail):
            wt = WorkerThread(conn, get_settings(), _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP, poll_interval=0.05)
            wt.start()
            import time; time.sleep(0.3)
            wt.stop(timeout=1.0)

        row = conn.execute("SELECT processed FROM events WHERE id='evt-TKT-001'").fetchone()
        assert row["processed"] == 1

    def test_ticket_busy_leaves_event_unprocessed(self):
        conn = _conn()
        _insert_event(conn, "TKT-001")

        call_count = [0]

        def _busy(event, *a, **kw):
            call_count[0] += 1
            if call_count[0] < 3:
                raise TicketBusy("busy")

        with patch("panpilot.worker.runner.process_event", side_effect=_busy):
            wt = WorkerThread(conn, get_settings(), _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP, poll_interval=0.05)
            wt.start()
            import time; time.sleep(0.5)
            wt.stop(timeout=1.0)

        # Called multiple times (retried each poll)
        assert call_count[0] >= 2
        # No DLQ entry for TicketBusy
        dlq_count = conn.execute("SELECT COUNT(*) FROM dlq").fetchone()[0]
        assert dlq_count == 0


# ---------------------------------------------------------------------------
# process_event — terminal status filtering
# ---------------------------------------------------------------------------

_TERMINAL_STATUS_NAMES: frozenset[str] = frozenset({"closed"})


class TestTerminalStatusFilter:

    def _terminal_payload(self) -> dict:
        return _payload(Status="Closed")

    def test_evaluate_not_called_for_terminal_ticket(self):
        conn = _conn()
        evt = _event(payload=self._terminal_payload())
        with patch("panpilot.worker.runner.evaluate_ticket") as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    evt, get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                    terminal_status_names=_TERMINAL_STATUS_NAMES,
                )
        mock_eval.assert_not_called()

    def test_route_not_called_for_terminal_ticket(self):
        conn = _conn()
        evt = _event(payload=self._terminal_payload())
        with patch("panpilot.worker.runner.evaluate_ticket"):
            with patch("panpilot.worker.runner.route") as mock_route:
                process_event(
                    evt, get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                    terminal_status_names=_TERMINAL_STATUS_NAMES,
                )
        mock_route.assert_not_called()

    def test_no_ticket_state_set_for_terminal_ticket(self):
        conn = _conn()
        evt = _event(payload=self._terminal_payload())
        with patch("panpilot.worker.runner.evaluate_ticket"):
            with patch("panpilot.worker.runner.route"):
                process_event(
                    evt, get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                    terminal_status_names=_TERMINAL_STATUS_NAMES,
                )
        row = conn.execute("SELECT state FROM ticket_state WHERE ticket_id='TKT-001'").fetchone()
        assert row is None

    def test_active_status_is_not_skipped(self):
        conn = _conn()
        evt = _event(payload=_payload(Status="New"))  # non-terminal Status
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=_mock_decision("none")) as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    evt, get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                    terminal_status_names=_TERMINAL_STATUS_NAMES,
                )
        mock_eval.assert_called_once()

    def test_empty_terminal_set_processes_everything(self):
        conn = _conn()
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=_mock_decision("none")) as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                    terminal_status_names=frozenset(),
                )
        mock_eval.assert_called_once()

    def test_missing_status_field_in_payload_is_not_skipped(self):
        # Payload with no Status field should still be evaluated.
        conn = _conn()
        payload_no_status = {
            "Title": "Test", "Description": "Test.",
            "DateCreated": "2026-05-25T08:00:00Z",
            "DateLastModified": "2026-05-25T09:00:00Z",
            "RequestedUserComments": False,
        }
        evt = _event(payload=payload_no_status)
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=_mock_decision("none")) as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    evt, get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                    terminal_status_names=_TERMINAL_STATUS_NAMES,
                )
        mock_eval.assert_called_once()

    def test_worker_thread_marks_terminal_event_processed(self):
        # WorkerThread calls mark_event_processed after process_event returns normally.
        # Terminal events return normally → they should be marked processed.
        conn = _conn()
        terminal_payload = json.dumps(self._terminal_payload())
        conn.execute(
            "INSERT INTO events (id, ticket_id, event_type, payload, processed) "
            "VALUES ('evt-term', 'TKT-001', 'Guardado', ?, 0)",
            (terminal_payload,),
        )
        conn.commit()

        import time
        wt = WorkerThread(
            conn, get_settings(), _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
            terminal_status_names=_TERMINAL_STATUS_NAMES,
            poll_interval=0.05,
        )
        wt.start()
        time.sleep(0.3)
        wt.stop(timeout=1.0)

        row = conn.execute("SELECT processed FROM events WHERE id='evt-term'").fetchone()
        assert row["processed"] == 1

    def test_worker_thread_no_dlq_entry_for_terminal(self):
        conn = _conn()
        terminal_payload = json.dumps(self._terminal_payload())
        conn.execute(
            "INSERT INTO events (id, ticket_id, event_type, payload, processed) "
            "VALUES ('evt-term', 'TKT-001', 'Guardado', ?, 0)",
            (terminal_payload,),
        )
        conn.commit()

        import time
        wt = WorkerThread(
            conn, get_settings(), _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
            terminal_status_names=_TERMINAL_STATUS_NAMES,
            poll_interval=0.05,
        )
        wt.start()
        time.sleep(0.3)
        wt.stop(timeout=1.0)

        dlq_count = conn.execute("SELECT COUNT(*) FROM dlq").fetchone()[0]
        assert dlq_count == 0


# ---------------------------------------------------------------------------
# _compute_terminal_ids — unit tests
# ---------------------------------------------------------------------------

def test_compute_terminal_ids_marks_resolved_as_terminal():
    from panpilot.intake.reference_data import _compute_terminal_ids
    statuses = [
        {"Id": "id-new",      "Code": 0,    "PadStatus_id": None,     "IncreaseSLA": False},
        {"Id": "id-assigned", "Code": 2,    "PadStatus_id": None,     "IncreaseSLA": False},
        {"Id": "id-resolved", "Code": 3,    "PadStatus_id": None,     "IncreaseSLA": False},
        {"Id": "id-closed",   "Code": 4,    "PadStatus_id": None,     "IncreaseSLA": False},
    ]
    result = _compute_terminal_ids(statuses)
    assert "id-resolved" in result
    assert "id-closed" in result


def test_compute_terminal_ids_excludes_new_and_assigned():
    from panpilot.intake.reference_data import _compute_terminal_ids
    statuses = [
        {"Id": "id-new",      "Code": 0, "PadStatus_id": None, "IncreaseSLA": False},
        {"Id": "id-assigned", "Code": 2, "PadStatus_id": None, "IncreaseSLA": False},
        {"Id": "id-resolved", "Code": 3, "PadStatus_id": None, "IncreaseSLA": False},
    ]
    result = _compute_terminal_ids(statuses)
    assert "id-new" not in result
    assert "id-assigned" not in result


def test_compute_terminal_ids_excludes_sub_statuses_of_assigned():
    from panpilot.intake.reference_data import _compute_terminal_ids
    statuses = [
        {"Id": "id-assigned", "Code": 2,    "PadStatus_id": None,          "IncreaseSLA": False},
        {"Id": "id-sub-work", "Code": None, "PadStatus_id": "id-assigned", "IncreaseSLA": False},
        {"Id": "id-resolved", "Code": 3,    "PadStatus_id": None,           "IncreaseSLA": False},
    ]
    result = _compute_terminal_ids(statuses)
    assert "id-sub-work" not in result  # child of Assigned → not terminal
    assert "id-resolved" in result


def test_compute_terminal_ids_increase_sla_true_is_never_terminal():
    from panpilot.intake.reference_data import _compute_terminal_ids
    statuses = [
        {"Id": "id-waiting", "Code": None, "PadStatus_id": None, "IncreaseSLA": True},
    ]
    result = _compute_terminal_ids(statuses)
    assert "id-waiting" not in result


def test_compute_terminal_ids_sub_status_of_resolved_is_terminal():
    from panpilot.intake.reference_data import _compute_terminal_ids
    statuses = [
        {"Id": "id-resolved",  "Code": 3,    "PadStatus_id": None,          "IncreaseSLA": False},
        {"Id": "id-confirm",   "Code": None, "PadStatus_id": "id-resolved", "IncreaseSLA": False},
    ]
    result = _compute_terminal_ids(statuses)
    assert "id-confirm" in result  # child of Resolved → terminal


# ---------------------------------------------------------------------------
# Guardado self-trigger loop guard — STALE_ALERT / NEEDS_HUMAN state check
# ---------------------------------------------------------------------------

class TestSelfTriggerLoopGuard:
    """
    When PanPilot posts an annotation, Proactivanet may fire a Guardado
    webhook because DateLastModified changed.  If the ticket is in STALE_ALERT
    or NEEDS_HUMAN, we must skip evaluation — otherwise we'd call Claude again
    and potentially post another annotation, creating an infinite loop.
    """

    def _run_process(self, conn, state: str):
        """Insert a ticket_state row with the given state, then run process_event."""
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority) VALUES ('TKT-001', ?, 'P2')",
            (state,),
        )
        conn.commit()
        with patch("panpilot.worker.runner.evaluate_ticket") as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        return mock_eval

    def test_stale_alert_state_skips_evaluate(self):
        conn = _conn()
        mock_eval = self._run_process(conn, "STALE_ALERT")
        mock_eval.assert_not_called()

    def test_needs_human_state_skips_evaluate(self):
        conn = _conn()
        mock_eval = self._run_process(conn, "NEEDS_HUMAN")
        mock_eval.assert_not_called()

    def test_stale_alert_state_skips_route(self):
        conn = _conn()
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority) VALUES ('TKT-001', 'STALE_ALERT', 'P2')",
        )
        conn.commit()
        with patch("panpilot.worker.runner.evaluate_ticket"):
            with patch("panpilot.worker.runner.route") as mock_route:
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        mock_route.assert_not_called()

    def test_waiting_state_still_evaluates(self):
        """WAITING is not in the skip set — re-evaluation is allowed."""
        conn = _conn()
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority) VALUES ('TKT-001', 'WAITING', 'P2')",
        )
        conn.commit()
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=_mock_decision("none")) as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        mock_eval.assert_called_once()

    def test_clr_req_state_still_evaluates(self):
        """CLR_REQ + RequestedUserComments=False (customer replied) → re-evaluate."""
        conn = _conn()
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority, clarification_count) "
            "VALUES ('TKT-001', 'CLR_REQ', 'P2', 1)",
        )
        conn.commit()
        # _payload() defaults RequestedUserComments=False — customer has replied
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=_mock_decision("none")) as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        mock_eval.assert_called_once()

    def test_clr_req_awaiting_self_trigger_skips_evaluate(self):
        """CLR_REQ + RequestedUserComments=True → PanPilot's annotation self-trigger → skip."""
        conn = _conn()
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority, clarification_count) "
            "VALUES ('TKT-001', 'CLR_REQ', 'P2', 1)",
        )
        conn.commit()
        evt = _event(payload=_payload(RequestedUserComments=True))
        with patch("panpilot.worker.runner.evaluate_ticket") as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    evt, get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        mock_eval.assert_not_called()

    def test_clr_req_awaiting_self_trigger_skips_route(self):
        """CLR_REQ + RequestedUserComments=True → route() never called."""
        conn = _conn()
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority, clarification_count) "
            "VALUES ('TKT-001', 'CLR_REQ', 'P2', 1)",
        )
        conn.commit()
        evt = _event(payload=_payload(RequestedUserComments=True))
        with patch("panpilot.worker.runner.evaluate_ticket"):
            with patch("panpilot.worker.runner.route") as mock_route:
                process_event(
                    evt, get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        mock_route.assert_not_called()

    def test_auto_resp_awaiting_self_trigger_skips_evaluate(self):
        """AUTO_RESP + RequestedUserComments=True → PanPilot's auto_respond annotation self-trigger → skip."""
        conn = _conn()
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority, clarification_count) "
            "VALUES ('TKT-001', 'AUTO_RESP', 'P2', 0)",
        )
        conn.commit()
        evt = _event(payload=_payload(RequestedUserComments=True))
        with patch("panpilot.worker.runner.evaluate_ticket") as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    evt, get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        mock_eval.assert_not_called()

    def test_auto_resp_awaiting_self_trigger_skips_route(self):
        """AUTO_RESP + RequestedUserComments=True → route() never called."""
        conn = _conn()
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority, clarification_count) "
            "VALUES ('TKT-001', 'AUTO_RESP', 'P2', 0)",
        )
        conn.commit()
        evt = _event(payload=_payload(RequestedUserComments=True))
        with patch("panpilot.worker.runner.evaluate_ticket"):
            with patch("panpilot.worker.runner.route") as mock_route:
                process_event(
                    evt, get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        mock_route.assert_not_called()

    def test_auto_resp_not_awaiting_skips_evaluate(self):
        """AUTO_RESP + RequestedUserComments=False → still skipped.

        AutomaticResponse annotations do not set RequestedUserComments=True, so
        every subsequent Guardado arrives with RequestedUserComments=False — we
        cannot distinguish the self-trigger Guardado from a customer update.
        Always skip to prevent the infinite annotation loop (REQ 2026-000016).
        """
        conn = _conn()
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority, clarification_count) "
            "VALUES ('TKT-001', 'AUTO_RESP', 'P2', 0)",
        )
        conn.commit()
        # _payload() defaults RequestedUserComments=False
        with patch("panpilot.worker.runner.evaluate_ticket") as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        mock_eval.assert_not_called()

    def test_auto_resp_not_awaiting_skips_route(self):
        """AUTO_RESP + RequestedUserComments=False → route() never called."""
        conn = _conn()
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority, clarification_count) "
            "VALUES ('TKT-001', 'AUTO_RESP', 'P2', 0)",
        )
        conn.commit()
        with patch("panpilot.worker.runner.evaluate_ticket"):
            with patch("panpilot.worker.runner.route") as mock_route:
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        mock_route.assert_not_called()

    def test_no_prior_state_still_evaluates(self):
        """No existing ticket_state row → new ticket, should be evaluated."""
        conn = _conn()
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=_mock_decision("none")) as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        mock_eval.assert_called_once()


# ---------------------------------------------------------------------------
# DLQ TicketBusy handling (regression: must not count against attempts)
# ---------------------------------------------------------------------------

def test_dlq_ticket_busy_does_not_increment_attempts():
    from panpilot.worker.dlq import DLQThread
    conn = _conn()
    conn.execute(
        "INSERT INTO events (id, ticket_id, event_type, payload, processed) "
        "VALUES ('evt-1', 'TKT-001', 'Guardado', '{}', 1)"
    )
    conn.execute(
        "INSERT INTO dlq (event_id, error, attempts, next_retry, exhausted) "
        "VALUES ('evt-1', 'first fail', 1, '2020-01-01T00:00:00Z', 0)"
    )
    conn.commit()

    def _busy(event):
        raise TicketBusy("busy")

    thread = DLQThread(conn, _busy, poll_interval=60.0)
    thread._process_due()  # synchronous call, no thread needed

    row = conn.execute("SELECT attempts, exhausted FROM dlq WHERE event_id='evt-1'").fetchone()
    assert row["attempts"] == 1    # unchanged — TicketBusy doesn't count
    assert row["exhausted"] == 0


# ---------------------------------------------------------------------------
# T13 — manual exclusion ([panpilot-manual] text marker)
# ---------------------------------------------------------------------------

class TestManualExclusion:

    def _excluded_payload(self) -> dict:
        return _payload(Description="Fix this. [panpilot-manual]")

    def _excluded_event(self) -> dict:
        return _event(payload=self._excluded_payload())

    def test_evaluate_not_called_for_excluded_ticket(self):
        conn = _conn()
        with patch("panpilot.worker.runner.evaluate_ticket") as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    self._excluded_event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        mock_eval.assert_not_called()

    def test_route_called_with_none_no_action_warranted(self):
        conn = _conn()
        with patch("panpilot.worker.runner.evaluate_ticket"):
            with patch("panpilot.worker.runner.route") as mock_route:
                process_event(
                    self._excluded_event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        routed = mock_route.call_args[0][0]
        assert routed.action == "none"
        assert routed.none_reason == "no_action_warranted"

    def test_ticket_state_set_after_exclusion(self):
        conn = _conn()
        with patch("panpilot.worker.runner.evaluate_ticket"):
            with patch("panpilot.worker.runner.route"):
                process_event(
                    self._excluded_event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        row = conn.execute(
            "SELECT state FROM ticket_state WHERE ticket_id='TKT-001'"
        ).fetchone()
        assert row is not None
        assert row["state"] == "WAITING"

    def test_marker_is_case_insensitive(self):
        conn = _conn()
        payload = _payload(Description="Fix this. [PANPILOT-MANUAL]")
        with patch("panpilot.worker.runner.evaluate_ticket") as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    _event(payload=payload), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        mock_eval.assert_not_called()

    def test_normal_ticket_not_excluded(self):
        conn = _conn()
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=_mock_decision("none")) as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        mock_eval.assert_called_once()

    def test_custom_field_set_disables_text_marker(self, monkeypatch):
        # When MANUAL_EXCLUSION_FIELD_ID is set, text-marker path is bypassed.
        monkeypatch.setenv("MANUAL_EXCLUSION_FIELD_ID", "some-uuid")
        get_settings.cache_clear()
        conn = _conn()
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=_mock_decision("none")) as mock_eval:
            with patch("panpilot.worker.runner.route"):
                process_event(
                    self._excluded_event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        # Text marker ignored when custom field is configured
        mock_eval.assert_called_once()


# ---------------------------------------------------------------------------
# T16 — reminder cap applied in process_event
# ---------------------------------------------------------------------------

class TestReminderCapInProcessEvent:

    def test_reminder_cap_applied(self):
        conn = _conn()
        # Set reminder_count to max (2)
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority, reminder_count) "
            "VALUES ('TKT-001', 'WAITING', 'P2', 2)"
        )
        conn.commit()
        remind = _mock_decision("remind")
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=remind):
            with patch("panpilot.worker.runner.route") as mock_route:
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        routed = mock_route.call_args[0][0]
        assert routed.action == "none"
        assert routed.none_reason == "needs_human"

    def test_remind_under_cap_passes_through(self):
        conn = _conn()
        conn.execute(
            "INSERT INTO ticket_state (ticket_id, state, priority, reminder_count) "
            "VALUES ('TKT-001', 'WAITING', 'P2', 1)"
        )
        conn.commit()
        remind = _mock_decision("remind")
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=remind):
            with patch("panpilot.worker.runner.route") as mock_route:
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        routed = mock_route.call_args[0][0]
        assert routed.action == "remind"


# ---------------------------------------------------------------------------
# T17 — org reminder cap wiring in process_event
# ---------------------------------------------------------------------------

class TestOrgCapInProcessEvent:

    def _seed_org_reminders(
        self, conn: sqlite3.Connection, requester_id: str, ticket_ids: list[str], count: int
    ) -> None:
        for tid in ticket_ids:
            conn.execute(
                "INSERT INTO ticket_state (ticket_id, state, priority, requester_id) "
                "VALUES (?, 'WAITING', 'P2', ?) "
                "ON CONFLICT(ticket_id) DO UPDATE SET requester_id=excluded.requester_id",
                (tid, requester_id),
            )
            for _ in range(count):
                conn.execute(
                    "INSERT INTO audit_log (ticket_id, action, reasoning, dry_run) "
                    "VALUES (?, 'remind', 'test', 0)",
                    (tid,),
                )
        conn.commit()

    def test_org_cap_reached_escalates_to_needs_human(self):
        conn = _conn()
        # 3 existing reminders for same requester = at default cap
        self._seed_org_reminders(conn, "REQ-1", ["TKT-002", "TKT-003", "TKT-004"], 1)
        remind = _mock_decision("remind")
        # TKT-001 event with same requester_id
        payload = _payload(PanUsers_idSource="REQ-1")
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=remind):
            with patch("panpilot.worker.runner.route") as mock_route:
                process_event(
                    _event(payload=payload), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        routed = mock_route.call_args[0][0]
        assert routed.action == "none"
        assert routed.none_reason == "needs_human"

    def test_org_cap_not_triggered_when_requester_id_absent(self):
        conn = _conn()
        # No PanUsers_idSource — requester_id will be None → org cap skipped
        remind = _mock_decision("remind")
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=remind):
            with patch("panpilot.worker.runner.route") as mock_route:
                process_event(
                    _event(), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        routed = mock_route.call_args[0][0]
        assert routed.action == "remind"

    def test_requester_id_persisted_to_ticket_state(self):
        conn = _conn()
        none_d = _mock_decision("none")
        payload = _payload(PanUsers_idSource="REQ-42")
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=none_d):
            with patch("panpilot.worker.runner.route"):
                process_event(
                    _event(payload=payload), get_settings(), conn,
                    _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                )
        row = conn.execute(
            "SELECT requester_id FROM ticket_state WHERE ticket_id='TKT-001'"
        ).fetchone()
        assert row["requester_id"] == "REQ-42"


# ---------------------------------------------------------------------------
# RAG wiring in process_event
# ---------------------------------------------------------------------------

def _mock_rag_deps(available: bool = True) -> RagDeps:
    model = MagicMock()
    model.encode.return_value = MagicMock()
    model.encode.return_value.tolist.return_value = [0.1] * 384

    collection = MagicMock()
    collection.count.return_value = 5 if available else 0
    collection.query.return_value = {
        "documents": [["Chunk de documentación."]],
        "metadatas": [[{"title": "Doc"}]],
        "distances": [[0.1]],
    }
    return RagDeps(model=model, collection=collection)


class TestRagWiringInProcessEvent:

    def test_auto_respond_with_rag_deps_calls_rag_evaluate(self):
        conn = _conn()
        auto_d = _mock_decision("auto_respond")
        rag_d = _mock_decision("auto_respond")
        deps = _mock_rag_deps(available=True)
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=auto_d):
            with patch("panpilot.worker.runner.rag_evaluate", return_value=rag_d) as mock_rag:
                with patch("panpilot.worker.runner.route"):
                    process_event(
                        _event(), get_settings(), conn,
                        _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                        rag_deps=deps,
                    )
        mock_rag.assert_called_once()

    def test_non_auto_respond_does_not_call_rag_evaluate(self):
        conn = _conn()
        clarify_d = _mock_decision("clarify")
        deps = _mock_rag_deps(available=True)
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=clarify_d):
            with patch("panpilot.worker.runner.rag_evaluate") as mock_rag:
                with patch("panpilot.worker.runner.route"):
                    process_event(
                        _event(), get_settings(), conn,
                        _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                        rag_deps=deps,
                    )
        mock_rag.assert_not_called()

    def test_auto_respond_without_rag_deps_skips_rag(self):
        conn = _conn()
        auto_d = _mock_decision("auto_respond")
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=auto_d):
            with patch("panpilot.worker.runner.rag_evaluate") as mock_rag:
                with patch("panpilot.worker.runner.route"):
                    process_event(
                        _event(), get_settings(), conn,
                        _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                        rag_deps=None,
                    )
        mock_rag.assert_not_called()

    def test_auto_respond_with_unavailable_rag_deps_skips_rag(self):
        conn = _conn()
        auto_d = _mock_decision("auto_respond")
        deps = _mock_rag_deps(available=False)
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=auto_d):
            with patch("panpilot.worker.runner.rag_evaluate") as mock_rag:
                with patch("panpilot.worker.runner.route"):
                    process_event(
                        _event(), get_settings(), conn,
                        _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                        rag_deps=deps,
                    )
        mock_rag.assert_not_called()

    def test_rag_decision_substitutes_original(self):
        conn = _conn()
        auto_d = _mock_decision("auto_respond")
        rag_d = Decision(action="none", none_reason="low_confidence", reasoning="Baja confianza.")
        deps = _mock_rag_deps(available=True)
        with patch("panpilot.worker.runner.evaluate_ticket", return_value=auto_d):
            with patch("panpilot.worker.runner.rag_evaluate", return_value=rag_d):
                with patch("panpilot.worker.runner.route") as mock_route:
                    process_event(
                        _event(), get_settings(), conn,
                        _PRIORITY_MAP, _STATUS_MAP, _ACTION_TYPE_MAP,
                        rag_deps=deps,
                    )
        routed = mock_route.call_args[0][0]
        assert routed.action == "none"
        assert routed.none_reason == "low_confidence"
