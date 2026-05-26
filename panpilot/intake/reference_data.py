from __future__ import annotations

import logging
from typing import TypeAlias

import httpx

from panpilot.config import Settings

logger = logging.getLogger(__name__)

PriorityMap: TypeAlias = dict[str, str]    # uuid → "P1" | "P2" | "P3"
StatusMap: TypeAlias = dict[str, str]      # uuid → status_name
ActionTypeMap: TypeAlias = dict[str, str]  # type_name → uuid (e.g. "UserTextQuestion" → uuid)
TerminalStatusIds: TypeAlias = frozenset[str]   # UUIDs of terminal statuses (from Status API)
TerminalStatusNames: TypeAlias = frozenset[str] # lowercase status name strings for filtering

# Fallback set of canonical English terminal-status strings returned by the
# Proactivanet Incidents API in the Status field.  Used as a safety net when the
# Status API Name field does not match the Incidents API Status field (e.g. the
# instance uses localised Spanish Names but the Incidents endpoint returns English).
# Note: PadStatus_id is always null in Incidents API responses — UUID-based filtering
# never fires. Use string matching instead.
_TERMINAL_STATUS_STRINGS_FALLBACK: TerminalStatusNames = frozenset({
    "closed",
    "resolved",
    "rejected",
    "cancelled",
})

# The four action types PanPilot uses for annotation posting (T3).
# Names are the Type field values from GET /api/IncidentActionTypes.
REQUIRED_ACTION_TYPES = frozenset({
    "Annotation",          # internal only  (alert)
    "UserTextQuestion",    # customer-visible + sets RequestedUserComments=true (clarify)
    "AutomaticResponse",   # customer-visible (auto_respond)
    "PublishedAction",     # customer-visible (remind)
})


def _compute_terminal_ids(active: list[dict]) -> TerminalStatusIds:
    """
    Derive the set of status UUIDs that represent terminal ticket states.

    Terminal means: IncreaseSLA=false AND the status is not part of the "Nueva"
    (Code=0) or "Asignada a un grupo" (Code=2) active categories.  These two
    categories — and all their sub-statuses — have IncreaseSLA=false at the
    record level but are live, actionable work states.

    Codes 0 and 2 are Proactivanet system constants (not localised names): they
    identify the two categories that contain actively-worked tickets across every
    Proactivanet instance.  Any status whose own Code or whose parent's UUID is
    Code-0 or Code-2 is non-terminal.
    """
    _ACTIVE_CODES: frozenset[int] = frozenset({0, 2})
    active_ids = frozenset(s["Id"] for s in active if s.get("Code") in _ACTIVE_CODES)
    return frozenset(
        s["Id"]
        for s in active
        if not s.get("IncreaseSLA", True)
        and s["Id"] not in active_ids
        and s.get("PadStatus_id") not in active_ids
    )


def _compute_terminal_names(active: list[dict]) -> TerminalStatusNames:
    """
    Derive the set of lowercase terminal status Name strings from the Status API.

    Uses the same terminal-detection logic as _compute_terminal_ids() but returns
    the lowercase Name field so they can be matched against the Incidents API
    Status string field.  Unioned with _TERMINAL_STATUS_STRINGS_FALLBACK so that
    the standard English names are always caught even if the instance returns
    Spanish Names.
    """
    terminal_ids = _compute_terminal_ids(active)
    dynamic = frozenset(
        s["Name"].lower()
        for s in active
        if s.get("Id") in terminal_ids and s.get("Name")
    )
    return dynamic | _TERMINAL_STATUS_STRINGS_FALLBACK


async def load_reference_data(
    settings: Settings,
) -> tuple[PriorityMap, StatusMap, ActionTypeMap, TerminalStatusNames]:
    """
    Fetch priority, status, and annotation action-type maps from Proactivanet at startup.

    All three maps are required. Raises httpx.HTTPStatusError or ValueError on any
    failure so the service won't start with missing reference data.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        priority_map = await _load_priorities(client, settings)
        status_map, terminal_status_names = await _load_statuses(client, settings)
        action_type_map = await _load_action_types(client, settings)

    logger.info(
        "Reference data loaded: %d priorities, %d statuses, %d action types, %d terminal status names",
        len(priority_map),
        len(status_map),
        len(action_type_map),
        len(terminal_status_names),
    )
    return priority_map, status_map, action_type_map, terminal_status_names


def _auth_headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": settings.proactivanet_api_key}


async def _load_priorities(
    client: httpx.AsyncClient,
    settings: Settings,
) -> PriorityMap:
    response = await client.get(
        f"{settings.proactivanet_api_url}/Priorities",
        headers=_auth_headers(settings),
    )
    response.raise_for_status()

    active = [p for p in response.json() if not p.get("Inactive", False)]
    if not active:
        raise ValueError("GET /api/Priorities returned no active priorities — cannot map stale thresholds")

    # Sort ascending: lowest Sort value = highest priority = P1
    active.sort(key=lambda p: p["Sort"])

    mapping: PriorityMap = {}
    for rank, priority in enumerate(active, start=1):
        label = "P1" if rank == 1 else "P2" if rank == 2 else "P3"
        mapping[priority["Id"]] = label
        logger.debug(
            "Priority %s (%s) → %s (Sort=%s)",
            priority["Id"],
            priority.get("Name", "?"),
            label,
            priority["Sort"],
        )

    return mapping


async def _load_statuses(
    client: httpx.AsyncClient,
    settings: Settings,
) -> tuple[StatusMap, TerminalStatusNames]:
    response = await client.get(
        f"{settings.proactivanet_api_url}/Status",
        headers=_auth_headers(settings),
        params={"SourceElementCode": "Incidents"},
    )
    response.raise_for_status()

    active = [s for s in response.json() if not s.get("Inactive", False)]
    if not active:
        raise ValueError("GET /api/Status returned no active statuses for Incidents — cannot run state machine")

    terminal_names = _compute_terminal_names(active)

    # Key by s["Id"] (the status's own UUID). A ticket's PadStatus_id field
    # contains the status's Id, not the status's PadStatus_id (parent UUID).
    status_map: StatusMap = {s["Id"]: s["Name"] for s in active if s.get("Id")}
    return status_map, terminal_names


async def _load_action_types(
    client: httpx.AsyncClient,
    settings: Settings,
) -> ActionTypeMap:
    """
    Fetch the UUID for each annotation action type PanPilot uses.

    ActionTypeId in POST /api/Incidents/{id}/annotations is a UUID, not a string.
    This resolves type names (e.g. "UserTextQuestion") to their UUIDs at startup
    so the router can look them up without a per-annotation API round-trip.
    """
    response = await client.get(
        f"{settings.proactivanet_api_url}/IncidentActionTypes",
        headers=_auth_headers(settings),
        params={"Type": ",".join(sorted(REQUIRED_ACTION_TYPES))},
    )
    response.raise_for_status()

    mapping: ActionTypeMap = {}
    for item in response.json():
        type_name = item.get("Type", "")
        if not item.get("Inactive", False) and type_name in REQUIRED_ACTION_TYPES:
            mapping[type_name] = item["Id"]
            logger.debug("ActionType %s → %s", type_name, item["Id"])

    missing = REQUIRED_ACTION_TYPES - mapping.keys()
    if missing:
        raise ValueError(
            f"GET /api/IncidentActionTypes missing required types: {sorted(missing)}. "
            "Confirm these action types exist and are active in the Proactivanet instance."
        )

    return mapping
