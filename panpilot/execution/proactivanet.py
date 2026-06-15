from __future__ import annotations

import logging

import httpx

from panpilot.config import Settings

logger = logging.getLogger(__name__)

# ActionTypeId names → what they do (confirmed against live instance, OQ5 resolved)
# Annotation       → internal only; never visible to the customer
# UserTextQuestion → customer-visible; sets RequestedUserComments=true on the ticket
# AutomaticResponse → customer-visible
# PublishedAction  → customer-visible
ANNOTATION_TYPE_INTERNAL = "Annotation"
ANNOTATION_TYPE_CLARIFY = "UserTextQuestion"
ANNOTATION_TYPE_AUTO_RESPOND = "AutomaticResponse"
ANNOTATION_TYPE_REMIND = "PublishedAction"


class ProactivanetClient:
    """
    Thin HTTP client for Proactivanet write operations.

    Write scope is intentionally narrow: post_annotation() is the only method.
    PanPilot never closes tickets, changes priority, or modifies assignment.
    A persistent httpx.Client is used for connection reuse across annotations.
    """

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.proactivanet_api_url
        self._author_id = settings.proactivanet_author_id
        self._client = httpx.Client(
            headers={"Authorization": settings.proactivanet_api_key},
            timeout=15.0,
        )

    def post_annotation(
        self,
        ticket_id: str,
        text: str,
        action_type_id: str,
    ) -> dict:
        """
        Post an annotation to a ticket.

        action_type_id must be the UUID of the desired IncidentActionType, resolved
        from app.state.action_type_map at startup. Do not pass type name strings here.

        Raises httpx.HTTPStatusError on 4xx/5xx responses.
        Returns the AnnotationModel response dict (includes HasSentMail).
        """
        url = f"{self._base_url}/Incidents/{ticket_id}/annotations"
        payload = {
            "Type": "Technician",
            "Author_id": self._author_id,
            "Text": text,
            "ActionTypeId": action_type_id,
        }
        logger.debug(
            "POST %s (action_type_id=%s, text_len=%d)",
            url,
            action_type_id,
            len(text),
        )
        response = self._client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        logger.debug("Annotation posted, HasSentMail=%s", data.get("HasSentMail"))
        return data

    def get_incidents_modified_since(self, since: str) -> list[dict]:
        """
        Return all incidents whose DateLastModified is >= since (ISO-8601).

        Used by the startup catch-up (T10) to recover events missed during
        downtime.  The since value is the max received_at from the events
        table; on first run it defaults to 24 hours ago.

        Paginates using $top/$skip until the server returns a partial page,
        so large backlogs are fully fetched rather than silently truncated.
        A safety cap of 100 pages (20 000 incidents) prevents infinite loops
        if the server ignores $top.

        Raises httpx.HTTPStatusError on 4xx/5xx.
        """
        _PAGE_SIZE = 200
        _MAX_PAGES = 100

        url = f"{self._base_url}/Incidents"
        result: list[dict] = []
        offset = 0

        for page_num in range(_MAX_PAGES):
            response = self._client.get(url, params={
                "DateLastModified_from": since,
                "$top": _PAGE_SIZE,
                "$skip": offset,
            })
            response.raise_for_status()
            page: list[dict] = response.json()
            result.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
        else:
            logger.warning(
                "get_incidents_modified_since: safety page cap (%d) reached — "
                "%d incidents fetched; result may be incomplete",
                _MAX_PAGES,
                len(result),
            )

        return result

    def get_ticket(self, ticket_id: str) -> dict | None:
        """
        Fetch a single ticket from Proactivanet.

        Returns the ticket dict on success.
        Returns None on 404 (ticket deleted or not found).
        Raises httpx.HTTPStatusError on other 4xx/5xx errors.

        Note: a 404 can also mean a misconfigured API URL or a temporary
        permissions change — not always a deleted ticket. CLOSED_EXTERNALLY
        is permanent, so callers log at WARNING before transitioning.
        """
        url = f"{self._base_url}/Incidents/{ticket_id}"
        response = self._client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()
