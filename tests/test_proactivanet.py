"""Tests for ProactivanetClient — including the new get_ticket() method."""
from __future__ import annotations

import httpx
import pytest
import respx

from panpilot.config import get_settings
from panpilot.execution.proactivanet import ProactivanetClient

BASE = "https://test.proactivanet.example/api"


# ---------------------------------------------------------------------------
# get_ticket()
# ---------------------------------------------------------------------------

def test_get_ticket_returns_dict_on_200(mock_proactivanet):
    mock_proactivanet.get(f"{BASE}/Incidents/T-001").mock(
        return_value=httpx.Response(200, json={"Id": "T-001", "Status": "Open"})
    )
    client = ProactivanetClient(get_settings())
    result = client.get_ticket("T-001")
    assert result == {"Id": "T-001", "Status": "Open"}


def test_get_ticket_returns_none_on_404(mock_proactivanet):
    mock_proactivanet.get(f"{BASE}/Incidents/T-404").mock(
        return_value=httpx.Response(404)
    )
    client = ProactivanetClient(get_settings())
    assert client.get_ticket("T-404") is None


def test_get_ticket_raises_on_5xx(mock_proactivanet):
    mock_proactivanet.get(f"{BASE}/Incidents/T-500").mock(
        return_value=httpx.Response(500)
    )
    client = ProactivanetClient(get_settings())
    with pytest.raises(httpx.HTTPStatusError):
        client.get_ticket("T-500")


def test_get_ticket_raises_on_401(mock_proactivanet):
    mock_proactivanet.get(f"{BASE}/Incidents/T-401").mock(
        return_value=httpx.Response(401)
    )
    client = ProactivanetClient(get_settings())
    with pytest.raises(httpx.HTTPStatusError):
        client.get_ticket("T-401")


def test_get_ticket_uses_auth_header(mock_proactivanet, monkeypatch):
    monkeypatch.setenv("PROACTIVANET_API_KEY", "Bearer my-token")
    get_settings.cache_clear()
    mock_proactivanet.get(f"{BASE}/Incidents/T-001").mock(
        return_value=httpx.Response(200, json={"Id": "T-001", "Status": "Open"})
    )
    ProactivanetClient(get_settings()).get_ticket("T-001")
    req = mock_proactivanet.calls.last.request
    assert req.headers["authorization"] == "Bearer my-token"
