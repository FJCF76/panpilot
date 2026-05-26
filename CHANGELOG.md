# Changelog

All notable changes to PanPilot are documented here.

---

## [0.1.0] - 2026-05-26

Phase 1 complete. Full automation layer for Proactivanet ticket management,
operating in DRY_RUN=true mode pending Week 2 validation.

### Added

**Core intake pipeline**
- Webhook receiver (`POST /webhook/proactivanet`) with idempotency key deduplication — duplicate deliveries from Proactivanet are silently ignored (T2)
- SQLite event store with write-before-process guarantee — unprocessed events survive process restarts without loss (T2)
- Worker polling thread with per-event DLQ escalation after 3 retry attempts (T4)
- Dead-letter queue with exponential backoff (30 s → 5 min → 30 min) and admin retry button (T4, T19)
- Startup catch-up loader — queries Proactivanet for incidents modified since the last stored event so webhooks missed during downtime are recovered (T10)

**AI evaluation engine**
- Structured JSON decision output from Claude with tool_choice enforcement — model cannot skip the schema (T1)
- Four supported actions: `clarify`, `auto_respond`, `remind`, `alert` (T1, T3)
- Confidence threshold gate (default: 85 %) — auto-responses below threshold are downgraded to `none/low_confidence` (T1)
- Prompt injection hardening — ticket content sandboxed inside `<ticket>` XML delimiters with `html.escape()` on Title and Description (T7)
- Author-based annotation loop guard — PanPilot's own annotations are detected by `PROACTIVANET_AUTHOR_ID` and skipped (T3)
- Clarification cap: maximum 2 clarification requests per ticket (T11)

**State machine**
- Per-ticket state machine with 8 states: `PENDING_EVALUATION`, `WAITING`, `CLR_REQ`, `PENDING_AGENT_ACTION`, `STALE_ALERT`, `NEEDS_HUMAN`, `AWAITING_CLIENT_REPLY`, `AUTO_RESP` (T9)
- Ghost `PENDING_EVALUATION` recovery — crash-leftover states are reset at startup before threads start (T9)
- Race condition guard (`NEEDS_HUMAN` escalation on concurrent ticket updates) (T15)

**Stale ticket detector**
- APScheduler background job with SQLite job store and `coalesce=True` — missed runs during downtime result in one catch-up execution, not N (T6)
- Priority-based inactivity thresholds: P1 = 4 h, P2 = 24 h, P3 = 120 h (all configurable) (T6)
- Repeat-alert suppression via audit log — ticket not re-alerted until threshold window elapses again (T6)
- State transition to `STALE_ALERT` after alert fires — prevents duplicate alerts via state machine (T6)

**Reference data**
- Startup reference data loader — fetches priority and status maps from Proactivanet API at boot; service refuses to start if this fails (T18)
- Webhook payload dispatcher handling 4 confirmed payload shapes: flat, OldValue/NewValue diff, Annotation-wrapped, StatusChange-wrapped (T2)

**Admin interface**
- Audit log viewer at `GET /admin/audit` with filters by ticket, action, and dry-run flag (T5)
- DLQ viewer at `GET /admin/dlq` with exhausted/pending filter (T4)
- HTML dashboard at `GET /admin/` with audit log table, DLQ table, action filter, and Proactivanet deep-links (T5)
- Per-entry DLQ retry button (`POST /admin/dlq/{id}/retry`) — resets the DLQ entry and clears `events.processed` so the worker picks it up again (T19)
- HTTP Basic Auth on all `/admin/*` routes (T8)

**Infrastructure**
- SQLite WAL mode + `synchronous=NORMAL` on every connection for concurrent read/write performance
- systemd unit file with `--workers 1` enforcement (APScheduler requirement)
- nginx reverse proxy config with TLS termination and path allowlist (`/webhook`, `/admin` only)
- DRY_RUN mode — all decisions logged to audit, no Proactivanet write calls made (T8)
- pydantic-settings config with fail-fast validators for required credentials (T8)

**Test suite**
- 350 tests covering: webhook intake, event store idempotency, worker pipeline, DLQ retry, state machine transitions, stale detector logic, clarification caps, reference data loading, admin interface, audit log, prompt injection hardening, and scheduler lifecycle

### Fixed

- **XSS in admin dashboard** — `ticket_id` in Proactivanet deep-link `href` attribute was not HTML-escaped. Fixed with `_esc()` across all four `href` components.
- **Loop guard bypass with empty `PROACTIVANET_AUTHOR_ID`** — an empty string would match all annotations (every annotation has a blank author), causing an infinite annotation loop. Now a startup validator raises `ValueError` if this field is empty.
- **Empty `ADMIN_PASSWORD` accepted** — a blank password would leave the admin panel open to unauthenticated access. Now a startup validator raises `ValueError` if this field is empty or whitespace-only.
- **`catchup.py` integer `Status` field crash** — Proactivanet may return `Status` as an integer in some payload variants. Calling `.lower()` on an integer raises `AttributeError`. Fixed with `str(incident.get("Status") or "").lower()`, matching the pattern already in use in `runner.py`.
- **Stale detector not transitioning state** — `detect_stale_tickets()` called `route()` but not `apply_transition()`, so the `STALE_ALERT` state was never set. This made `STALE_ALERT` in `_SKIP_STATES` dead code and allowed unlimited repeat alerts. Fixed by adding `apply_transition()` after `route()` in the success path.

### Known Limitations

- `DRY_RUN=true` during Phase 1 — no Proactivanet write calls until Week 2 validation completes.
- No proactive alerting when a DLQ entry is exhausted. Operational workaround: monitor `journalctl -u panpilot.service -p err`. See `docs/ARCHITECTURE.md → Operational Notes`.
- Annotation text sent to Proactivanet is hard-truncated at 4 000 characters (`execution/router.py:_MAX_ANNOTATION_LEN`). The audit log stores the full text. Typical support responses are well under this limit.
- T13 manual exclusion field (`MANUAL_EXCLUSION_FIELD_ID`) is implemented as a config value but the Proactivanet custom field must be created by an admin before activation.
- Feature 2 (RAG auto-response) is not yet implemented. All `auto_respond` decisions in Phase 1 use Pass 1 (triage only) without document retrieval.

---

## Planned — Phase 2

- **Feature 2: RAG auto-response** — two-pass evaluation with Files API document retrieval for L1 knowledge questions.
- **T12: Files API quota rescue** — automatic re-upload of expired file IDs.
- **T13: Manual exclusion activation** — wire `MANUAL_EXCLUSION_FIELD_ID` to the Proactivanet custom field created by admin.
- **T14: Data governance sign-off** — formal sign-off before live mode.
- **T17: Cross-ticket org reminder cap** — limit total reminders sent across all tickets within a configurable rolling window.
- **DLQ exhaustion alerting** — proactive notification (email or webhook) when a DLQ entry reaches `exhausted=1`.
