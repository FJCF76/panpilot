"""Tests for T3: action router, audit write, PolicyViolation, and DRY_RUN enforcement."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, call

import httpx
import pytest

from panpilot.config import get_settings
from panpilot.execution.proactivanet import (
    ANNOTATION_TYPE_AUTO_RESPOND,
    ANNOTATION_TYPE_CLARIFY,
    ANNOTATION_TYPE_INTERNAL,
    ANNOTATION_TYPE_REMIND,
    ProactivanetClient,
)
from panpilot.execution.router import PolicyViolation, _ANNOTATION_TYPE_NAME, route
from panpilot.intelligence.models import Decision, TicketContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema = (Path(__file__).parent.parent / "panpilot" / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    return conn


def _make_ctx(**kwargs) -> TicketContext:
    defaults = dict(
        ticket_id="TKT-001",
        title="Cannot export",
        description="Export button crashes.",
        status="Assigned",
        priority="P2",
        created_at="2026-05-25T08:00:00Z",
        last_modified="2026-05-25T09:00:00Z",
        awaiting_client_reply=False,
    )
    return TicketContext(**{**defaults, **kwargs})


def _make_decision(action: str, **kwargs) -> Decision:
    defaults = dict(reasoning="Test reasoning.", response_draft=None, none_reason=None)
    if action == "none" and "none_reason" not in kwargs:
        defaults["none_reason"] = "no_action_warranted"
    # clarify, auto_respond, and remind are customer-facing and require response_draft.
    # alert is an internal note and uses reasoning instead.
    if action in {"clarify", "auto_respond", "remind"} and "response_draft" not in kwargs:
        defaults["response_draft"] = "La solución es..."
    return Decision(action=action, **{**defaults, **kwargs})


def _mock_client() -> MagicMock:
    client = MagicMock(spec=ProactivanetClient)
    client.post_annotation.return_value = {"HasSentMail": True, "Id": "ann-uuid"}
    return client


_ACTION_TYPE_MAP = {
    "Annotation": "uuid-annotation",
    "UserTextQuestion": "uuid-clarify",
    "AutomaticResponse": "uuid-auto",
    "PublishedAction": "uuid-remind",
}


# ---------------------------------------------------------------------------
# DRY_RUN mode — no API calls, audit written with dry_run=1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["clarify", "auto_respond", "remind", "alert"])
def test_dry_run_makes_no_api_call(action, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()
    conn = _in_memory_conn()
    mock_client = _mock_client()
    decision = _make_decision(action)
    route(_make_decision(action), _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP, proactivanet_client=mock_client)
    mock_client.post_annotation.assert_not_called()


@pytest.mark.parametrize("action", ["clarify", "auto_respond", "remind", "alert"])
def test_dry_run_writes_audit_with_dry_run_flag(action, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()
    conn = _in_memory_conn()
    route(_make_decision(action), _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP, proactivanet_client=_mock_client())
    row = conn.execute("SELECT dry_run FROM audit_log WHERE ticket_id='TKT-001'").fetchone()
    assert row["dry_run"] == 1


# ---------------------------------------------------------------------------
# Live mode — API call made, audit written with dry_run=0
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["clarify", "auto_respond", "remind", "alert"])
def test_live_mode_calls_post_annotation(action, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    conn = _in_memory_conn()
    mock_client = _mock_client()
    route(_make_decision(action), _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP, proactivanet_client=mock_client)
    mock_client.post_annotation.assert_called_once()


@pytest.mark.parametrize("action", ["clarify", "auto_respond", "remind", "alert"])
def test_live_mode_writes_audit_with_dry_run_0(action, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    conn = _in_memory_conn()
    route(_make_decision(action), _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP, proactivanet_client=_mock_client())
    row = conn.execute("SELECT dry_run FROM audit_log WHERE ticket_id='TKT-001'").fetchone()
    assert row["dry_run"] == 0


# ---------------------------------------------------------------------------
# Correct action type UUID is passed per action
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action,expected_type_name", [
    ("clarify", ANNOTATION_TYPE_CLARIFY),
    ("auto_respond", ANNOTATION_TYPE_AUTO_RESPOND),
    ("remind", ANNOTATION_TYPE_REMIND),
    ("alert", ANNOTATION_TYPE_INTERNAL),
])
def test_correct_action_type_uuid_used(action, expected_type_name, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    conn = _in_memory_conn()
    mock_client = _mock_client()
    route(_make_decision(action), _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP, proactivanet_client=mock_client)
    _, kwargs = mock_client.post_annotation.call_args
    expected_uuid = _ACTION_TYPE_MAP[expected_type_name]
    assert kwargs["action_type_id"] == expected_uuid


# ---------------------------------------------------------------------------
# Annotation text: customer-facing actions use response_draft; alert uses reasoning
# ---------------------------------------------------------------------------

def test_annotation_uses_response_draft_when_present(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    decision = _make_decision("auto_respond", response_draft="La respuesta en español.")
    conn = _in_memory_conn()
    mock_client = _mock_client()
    route(decision, _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP, proactivanet_client=mock_client)
    _, kwargs = mock_client.post_annotation.call_args
    assert kwargs["text"] == "La respuesta en español."


def test_alert_uses_reasoning_as_annotation_text(monkeypatch):
    """alert is an internal note — posts reasoning, never response_draft."""
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    decision = _make_decision("alert", response_draft=None, reasoning="Ticket stale for 6h.")
    conn = _in_memory_conn()
    mock_client = _mock_client()
    route(decision, _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP, proactivanet_client=mock_client)
    _, kwargs = mock_client.post_annotation.call_args
    assert kwargs["text"] == "Ticket stale for 6h."


def test_auto_respond_skips_post_when_response_draft_absent(monkeypatch):
    """auto_respond with no response_draft must NOT post — reasoning must never reach customers."""
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    decision = _make_decision("auto_respond", response_draft=None)
    conn = _in_memory_conn()
    mock_client = _mock_client()
    route(decision, _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP, proactivanet_client=mock_client)
    mock_client.post_annotation.assert_not_called()


def test_clarify_skips_post_when_response_draft_absent(monkeypatch):
    """clarify with no response_draft must NOT post — reasoning must never reach customers."""
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    decision = _make_decision("clarify", response_draft=None)
    conn = _in_memory_conn()
    mock_client = _mock_client()
    route(decision, _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP, proactivanet_client=mock_client)
    mock_client.post_annotation.assert_not_called()


# ---------------------------------------------------------------------------
# action=none — no API call, audit always written
# ---------------------------------------------------------------------------

def test_none_action_makes_no_api_call(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    conn = _in_memory_conn()
    mock_client = _mock_client()
    route(_make_decision("none"), _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP, proactivanet_client=mock_client)
    mock_client.post_annotation.assert_not_called()


def test_none_action_writes_audit(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    conn = _in_memory_conn()
    route(_make_decision("none"), _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP)
    row = conn.execute("SELECT action, none_reason FROM audit_log").fetchone()
    assert row["action"] == "none"
    assert row["none_reason"] == "no_action_warranted"


# ---------------------------------------------------------------------------
# PolicyViolation — unknown action
# ---------------------------------------------------------------------------

def test_unknown_action_raises_policy_violation():
    conn = _in_memory_conn()
    bad_decision = Decision(action="delete_ticket", reasoning="oops")  # type: ignore[arg-type]
    with pytest.raises(PolicyViolation, match="delete_ticket"):
        route(bad_decision, _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP)


def test_unknown_action_still_writes_audit():
    """Audit entry is written before PolicyViolation is raised."""
    conn = _in_memory_conn()
    bad_decision = Decision(action="delete_ticket", reasoning="oops")  # type: ignore[arg-type]
    with pytest.raises(PolicyViolation):
        route(bad_decision, _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP)
    row = conn.execute("SELECT action FROM audit_log").fetchone()
    assert row is not None
    assert row["action"] == "delete_ticket"


def test_missing_action_type_uuid_raises_policy_violation(monkeypatch):
    """Router raises PolicyViolation if the required UUID is absent from action_type_map."""
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    conn = _in_memory_conn()
    incomplete_map = {"Annotation": "uuid-ann"}  # missing UserTextQuestion etc.
    with pytest.raises(PolicyViolation, match="UserTextQuestion"):
        route(_make_decision("clarify"), _make_ctx(), get_settings(), conn, incomplete_map, proactivanet_client=_mock_client())


# ---------------------------------------------------------------------------
# Audit completeness — every route call writes exactly one audit entry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["clarify", "auto_respond", "remind", "alert", "none"])
def test_always_writes_exactly_one_audit_entry(action, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()
    conn = _in_memory_conn()
    route(_make_decision(action), _make_ctx(), get_settings(), conn, _ACTION_TYPE_MAP, proactivanet_client=_mock_client())
    count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# ProactivanetClient — request shape (respx intercepting real httpx)
# ---------------------------------------------------------------------------

BASE = "https://test.proactivanet.example/api"


def test_post_annotation_sends_correct_payload(mock_proactivanet):
    mock_proactivanet.post(f"{BASE}/Incidents/TKT-001/annotations").mock(
        return_value=httpx.Response(200, json={"HasSentMail": True, "Id": "ann-1"})
    )
    client = ProactivanetClient(get_settings())
    result = client.post_annotation("TKT-001", text="Test text", action_type_id="uuid-clarify")
    assert result["HasSentMail"] is True

    req = mock_proactivanet.calls.last.request
    import json
    body = json.loads(req.content)
    assert body["Type"] == "Technician"
    assert body["Text"] == "Test text"
    assert body["ActionTypeId"] == "uuid-clarify"
    assert body["Author_id"] == get_settings().proactivanet_author_id


def test_post_annotation_raises_on_4xx(mock_proactivanet):
    mock_proactivanet.post(f"{BASE}/Incidents/TKT-001/annotations").mock(
        return_value=httpx.Response(401)
    )
    client = ProactivanetClient(get_settings())
    with pytest.raises(httpx.HTTPStatusError):
        client.post_annotation("TKT-001", text="x", action_type_id="uuid")


def test_post_annotation_uses_auth_header(mock_proactivanet, monkeypatch):
    monkeypatch.setenv("PROACTIVANET_API_KEY", "bearer-token-123")
    get_settings.cache_clear()
    mock_proactivanet.post(f"{BASE}/Incidents/TKT-002/annotations").mock(
        return_value=httpx.Response(200, json={"HasSentMail": False, "Id": "ann-2"})
    )
    ProactivanetClient(get_settings()).post_annotation("TKT-002", text="x", action_type_id="uuid")
    req = mock_proactivanet.calls.last.request
    assert req.headers["authorization"] == "bearer-token-123"
