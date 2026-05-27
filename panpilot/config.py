from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Proactivanet API
    proactivanet_api_url: str
    proactivanet_api_key: str
    # UUID of the Proactivanet technician account PanPilot posts annotations as.
    # Obtain from the Proactivanet admin interface (Técnicos list).
    proactivanet_author_id: str
    # Web-facing base URL for ticket deep-links in the admin UI (no /api suffix).
    # Ticket deep-link: {proactivanet_base_url}/servicedesk/incidents/formIncidents/formIncidents.paw?id={uuid}
    proactivanet_base_url: str

    # Anthropic
    anthropic_api_key: str

    # Admin interface — HTTP Basic Auth enforced in intake/webhook.py
    admin_username: str = "admin"
    admin_password: str

    # Confidence gate — applies to auto_respond action only
    confidence_threshold: float = 0.85

    # Clarification cap per ticket (T11)
    clarification_max: int = 2

    # Reminder caps (T16, T17)
    reminder_max_per_ticket: int = 2
    reminder_org_max: int = 3          # T17: max reminders to same requester across all tickets within window
    reminder_org_window_days: int = 3

    # Reminder scheduler thresholds (H18 Gap 2)
    reminder_poll_hours: int = 8          # how often the reminder scheduler runs
    reminder_threshold_hours: int = 24    # hours of WAITING silence before a reminder fires

    # Stale detection thresholds (T6)
    stale_threshold_p1_hours: int = 4
    stale_threshold_p2_hours: int = 24
    stale_threshold_p3_hours: int = 120
    stale_alert_poll_minutes: int = 10

    # Incoming webhook auth — shared secret sent in X-Webhook-Secret header.
    # Empty string disables the check (acceptable on an internal network).
    webhook_secret: str = ""

    # Webhook idempotency key source (T2).
    # Set to the JSON field name in the webhook payload that contains a unique delivery ID.
    # Empty string activates the fallback: sha256(ticket_id + event_type + DateLastModified).
    # NEVER point this at a receive-timestamp field — timestamps are not idempotent.
    webhook_idempotency_field: str = ""

    # UUID of the Proactivanet custom field used for manual exclusion (T13).
    # Empty string activates the text-marker fallback: "[panpilot-manual]" in Description.
    manual_exclusion_field_id: str = ""

    # Dry-run mode — decisions are logged but no Proactivanet write calls are made.
    # Ships as True. Set to False only after Week 2 dry-run validation.
    dry_run: bool = True

    # SQLite data directory — relative to the working directory at startup
    data_dir: Path = Path("data")

    # RAG — documentation retrieval (Phase 2)
    pandocs_dir: Path | None = None         # ~/pandocs or absolute path; None disables RAG
    chroma_dir: Path = Path("data/chroma")  # ChromaDB persistence directory
    rag_top_k: int = 5                       # number of chunks to retrieve per query

    @field_validator("proactivanet_author_id")
    @classmethod
    def _author_id_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "PROACTIVANET_AUTHOR_ID must be set to the PanPilot technician UUID. "
                "An empty value disables the annotation loop guard and causes infinite loops."
            )
        return v

    @field_validator("admin_password")
    @classmethod
    def _admin_password_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "ADMIN_PASSWORD must be a non-empty string. "
                "An empty value allows unauthenticated access to the admin panel."
            )
        return v

    @field_validator("confidence_threshold")
    @classmethod
    def _confidence_in_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("CONFIDENCE_THRESHOLD must be between 0.0 and 1.0")
        return v

    @field_validator("proactivanet_api_url", "proactivanet_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
