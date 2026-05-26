"""Tests for T18: startup reference data loader (priorities, statuses, action types)."""
import pytest
import httpx

from panpilot.config import get_settings
from panpilot.intake.reference_data import REQUIRED_ACTION_TYPES, load_reference_data

BASE = "https://test.proactivanet.example/api"

_PRIORITIES_URL = f"{BASE}/Priorities"
_STATUS_URL = f"{BASE}/Status"
_ACTION_TYPES_URL = f"{BASE}/IncidentActionTypes"

_DEFAULT_STATUSES = [
    {"Id": "s-1", "PadStatus_id": None, "Name": "New", "SourceElementCode": "Incidents", "Inactive": False},
    {"Id": "s-2", "PadStatus_id": None, "Name": "Assigned", "SourceElementCode": "Incidents", "Inactive": False},
]

_DEFAULT_PRIORITIES = [
    {"Id": "p-1", "Name": "Critical", "Sort": 1, "Inactive": False},
    {"Id": "p-2", "Name": "High", "Sort": 2, "Inactive": False},
    {"Id": "p-3", "Name": "Medium", "Sort": 3, "Inactive": False},
]

_DEFAULT_ACTION_TYPES = [
    {"Id": "at-1", "Type": "Annotation", "Name": "Anotación", "Inactive": False},
    {"Id": "at-2", "Type": "UserTextQuestion", "Name": "Pregunta texto", "Inactive": False},
    {"Id": "at-3", "Type": "AutomaticResponse", "Name": "Respuesta automática", "Inactive": False},
    {"Id": "at-4", "Type": "PublishedAction", "Name": "Acción publicada", "Inactive": False},
]


@pytest.fixture
def mock_api(mock_proactivanet):
    """Register all three default happy-path routes."""
    mock_proactivanet.get(_PRIORITIES_URL).mock(
        return_value=httpx.Response(200, json=_DEFAULT_PRIORITIES)
    )
    mock_proactivanet.get(_STATUS_URL).mock(
        return_value=httpx.Response(200, json=_DEFAULT_STATUSES)
    )
    mock_proactivanet.get(_ACTION_TYPES_URL).mock(
        return_value=httpx.Response(200, json=_DEFAULT_ACTION_TYPES)
    )
    return mock_proactivanet


def _register_defaults(mock_proactivanet):
    """Helper: register all three default routes on a per-test mock."""
    mock_proactivanet.get(_PRIORITIES_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_PRIORITIES))
    mock_proactivanet.get(_STATUS_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_STATUSES))
    mock_proactivanet.get(_ACTION_TYPES_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_ACTION_TYPES))


# ---------------------------------------------------------------------------
# Priority mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_priority_sort_rank_maps_to_p1_p2_p3(mock_api):
    priority_map, _, _, _ = await load_reference_data(get_settings())
    assert priority_map["p-1"] == "P1"
    assert priority_map["p-2"] == "P2"
    assert priority_map["p-3"] == "P3"


@pytest.mark.asyncio
async def test_fourth_priority_also_maps_to_p3(mock_proactivanet):
    mock_proactivanet.get(_PRIORITIES_URL).mock(return_value=httpx.Response(200, json=[
        {"Id": "p-1", "Name": "Critical", "Sort": 1, "Inactive": False},
        {"Id": "p-2", "Name": "High", "Sort": 2, "Inactive": False},
        {"Id": "p-3", "Name": "Medium", "Sort": 3, "Inactive": False},
        {"Id": "p-4", "Name": "Low", "Sort": 4, "Inactive": False},
    ]))
    mock_proactivanet.get(_STATUS_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_STATUSES))
    mock_proactivanet.get(_ACTION_TYPES_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_ACTION_TYPES))
    priority_map, _, _, _ = await load_reference_data(get_settings())
    assert priority_map["p-3"] == "P3"
    assert priority_map["p-4"] == "P3"


@pytest.mark.asyncio
async def test_non_sequential_sort_values_use_rank_not_value(mock_proactivanet):
    mock_proactivanet.get(_PRIORITIES_URL).mock(return_value=httpx.Response(200, json=[
        {"Id": "p-a", "Name": "A", "Sort": 10, "Inactive": False},
        {"Id": "p-b", "Name": "B", "Sort": 30, "Inactive": False},
        {"Id": "p-c", "Name": "C", "Sort": 50, "Inactive": False},
    ]))
    mock_proactivanet.get(_STATUS_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_STATUSES))
    mock_proactivanet.get(_ACTION_TYPES_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_ACTION_TYPES))
    priority_map, _, _, _ = await load_reference_data(get_settings())
    assert priority_map["p-a"] == "P1"
    assert priority_map["p-b"] == "P2"
    assert priority_map["p-c"] == "P3"


@pytest.mark.asyncio
async def test_inactive_priorities_are_excluded(mock_proactivanet):
    mock_proactivanet.get(_PRIORITIES_URL).mock(return_value=httpx.Response(200, json=[
        {"Id": "p-active-1", "Name": "High", "Sort": 1, "Inactive": False},
        {"Id": "p-inactive", "Name": "Obsolete", "Sort": 2, "Inactive": True},
        {"Id": "p-active-2", "Name": "Low", "Sort": 3, "Inactive": False},
    ]))
    mock_proactivanet.get(_STATUS_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_STATUSES))
    mock_proactivanet.get(_ACTION_TYPES_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_ACTION_TYPES))
    priority_map, _, _, _ = await load_reference_data(get_settings())
    assert "p-inactive" not in priority_map
    assert priority_map["p-active-1"] == "P1"
    assert priority_map["p-active-2"] == "P2"


@pytest.mark.asyncio
async def test_all_inactive_priorities_raises(mock_proactivanet):
    mock_proactivanet.get(_PRIORITIES_URL).mock(return_value=httpx.Response(200, json=[
        {"Id": "p-1", "Name": "Old", "Sort": 1, "Inactive": True},
    ]))
    mock_proactivanet.get(_STATUS_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_STATUSES))
    with pytest.raises(ValueError, match="no active priorities"):
        await load_reference_data(get_settings())


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_map_uses_id_as_key(mock_api):
    _, status_map, _, _ = await load_reference_data(get_settings())
    assert status_map["s-1"] == "New"
    assert status_map["s-2"] == "Assigned"


@pytest.mark.asyncio
async def test_inactive_statuses_are_excluded(mock_proactivanet):
    mock_proactivanet.get(_PRIORITIES_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_PRIORITIES))
    mock_proactivanet.get(_STATUS_URL).mock(return_value=httpx.Response(200, json=[
        {"Id": "s-active", "PadStatus_id": None, "Name": "New", "SourceElementCode": "Incidents", "Inactive": False},
        {"Id": "s-old", "PadStatus_id": None, "Name": "Deprecated", "SourceElementCode": "Incidents", "Inactive": True},
    ]))
    mock_proactivanet.get(_ACTION_TYPES_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_ACTION_TYPES))
    _, status_map, _, _ = await load_reference_data(get_settings())
    assert "s-old" not in status_map
    assert status_map["s-active"] == "New"


@pytest.mark.asyncio
async def test_empty_statuses_raises(mock_proactivanet):
    mock_proactivanet.get(_PRIORITIES_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_PRIORITIES))
    mock_proactivanet.get(_STATUS_URL).mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(ValueError, match="no active statuses"):
        await load_reference_data(get_settings())


# ---------------------------------------------------------------------------
# Action type mapping (T3 dependency)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_action_type_map_keyed_by_type_name(mock_api):
    _, _, action_type_map, _ = await load_reference_data(get_settings())
    assert action_type_map["Annotation"] == "at-1"
    assert action_type_map["UserTextQuestion"] == "at-2"
    assert action_type_map["AutomaticResponse"] == "at-3"
    assert action_type_map["PublishedAction"] == "at-4"


@pytest.mark.asyncio
async def test_all_required_action_types_present(mock_api):
    _, _, action_type_map, _ = await load_reference_data(get_settings())
    assert REQUIRED_ACTION_TYPES.issubset(action_type_map.keys())


@pytest.mark.asyncio
async def test_inactive_action_types_excluded(mock_proactivanet):
    _register_defaults(mock_proactivanet)
    # Override action types with one inactive entry
    mock_proactivanet.get(_ACTION_TYPES_URL).mock(return_value=httpx.Response(200, json=[
        {"Id": "at-1", "Type": "Annotation", "Inactive": False},
        {"Id": "at-2", "Type": "UserTextQuestion", "Inactive": False},
        {"Id": "at-3", "Type": "AutomaticResponse", "Inactive": False},
        {"Id": "at-OLD", "Type": "PublishedAction", "Inactive": True},   # inactive
        {"Id": "at-4", "Type": "PublishedAction", "Inactive": False},    # active duplicate wins
    ]))
    _, _, action_type_map, _ = await load_reference_data(get_settings())
    assert action_type_map["PublishedAction"] == "at-4"
    assert "at-OLD" not in action_type_map.values()


@pytest.mark.asyncio
async def test_missing_required_action_type_raises(mock_proactivanet):
    mock_proactivanet.get(_PRIORITIES_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_PRIORITIES))
    mock_proactivanet.get(_STATUS_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_STATUSES))
    # Return action types missing AutomaticResponse
    mock_proactivanet.get(_ACTION_TYPES_URL).mock(return_value=httpx.Response(200, json=[
        {"Id": "at-1", "Type": "Annotation", "Inactive": False},
        {"Id": "at-2", "Type": "UserTextQuestion", "Inactive": False},
        {"Id": "at-4", "Type": "PublishedAction", "Inactive": False},
        # AutomaticResponse deliberately absent
    ]))
    with pytest.raises(ValueError, match="missing required types"):
        await load_reference_data(get_settings())


@pytest.mark.asyncio
async def test_action_types_http_error_raises(mock_proactivanet):
    mock_proactivanet.get(_PRIORITIES_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_PRIORITIES))
    mock_proactivanet.get(_STATUS_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_STATUSES))
    mock_proactivanet.get(_ACTION_TYPES_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await load_reference_data(get_settings())


# ---------------------------------------------------------------------------
# HTTP errors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_priorities_http_error_raises(mock_proactivanet):
    mock_proactivanet.get(_PRIORITIES_URL).mock(return_value=httpx.Response(401))
    mock_proactivanet.get(_STATUS_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_STATUSES))
    with pytest.raises(httpx.HTTPStatusError):
        await load_reference_data(get_settings())


@pytest.mark.asyncio
async def test_status_http_error_raises(mock_proactivanet):
    mock_proactivanet.get(_PRIORITIES_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_PRIORITIES))
    mock_proactivanet.get(_STATUS_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await load_reference_data(get_settings())


# ---------------------------------------------------------------------------
# Auth header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auth_header_is_sent(mock_proactivanet, monkeypatch):
    monkeypatch.setenv("PROACTIVANET_API_KEY", "my-secret-key")
    get_settings.cache_clear()

    captured_headers: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json=_DEFAULT_PRIORITIES)

    mock_proactivanet.get(_PRIORITIES_URL).mock(side_effect=capture)
    mock_proactivanet.get(_STATUS_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_STATUSES))
    mock_proactivanet.get(_ACTION_TYPES_URL).mock(return_value=httpx.Response(200, json=_DEFAULT_ACTION_TYPES))

    await load_reference_data(get_settings())
    assert captured_headers.get("authorization") == "my-secret-key"
