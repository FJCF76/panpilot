# Changelog

All notable changes to PanPilot are documented here.

---

## [0.2.3] - 2026-05-27

### Fixed

- **`_translate_to_spanish()` inverted language on Spanish input** — `_TRANSLATE_PROMPT` said "Translate the following text to Spanish"; when given already-Spanish reasoning (produced by the `_RAG_SYSTEM_PROMPT` fix in 0.2.2), Haiku interpreted the Spanish text as the source and returned an English translation, storing English in the audit log. Fixed: prompt now explicitly checks "if English, translate to Spanish; if already Spanish, return unchanged." Confirmed end-to-end: `auto_respond` audit entries now store Spanish reasoning correctly.
- **Admin dashboard reasoning column truncation** — the reasoning `<td>` used `-webkit-line-clamp:4` and `overflow:hidden`, cutting off the reasoning text after four lines. Replaced with `white-space:pre-wrap` so full reasoning is always visible.

### Tests

- 2 new tests: dashboard reasoning not truncated (no `line-clamp`/`overflow:hidden`), reasoning cell uses `white-space:pre-wrap`.
- Total: 464 tests

---

## [0.2.2] - 2026-05-27

### Fixed

- **RAG Pass 2 reasoning in Spanish** — `evaluate_with_context()` had no system prompt, so Claude wrote `reasoning` in English. If the `_translate_to_spanish()` call in `write_audit()` then failed silently (exception caught), English reasoning was stored in the audit log. Fixed on two fronts: (1) `evaluate_with_context()` now passes `_RAG_SYSTEM_PROMPT` as `system=`, requiring Spanish reasoning at the source; (2) `RAG_DECISION_TOOL` reasoning description updated to "in Spanish." Pass 1 `SYSTEM_PROMPT` and `DECISION_TOOL` reasoning descriptions updated to match, so both passes are consistent.

### Tests

- 3 new tests: `evaluate_with_context` system prompt wiring, system prompt Spanish requirement, runner integration verifying RAG `auto_respond` reasoning passes through `_translate_to_spanish` before audit DB write.
- Total: 462 tests

---

## [0.2.1] - 2026-05-26

Production bug fixes and defensive hardening surfaced during live-mode validation.

### Fixed

- **RAG initialization gate** — RAG never loaded because the gate checked `pandocs_dir is not None` (an env var that was absent from `.env`). Now gates on ChromaDB collection presence: PanPilot initializes RAG whenever the `pandocs` collection exists and is non-empty in `chroma_dir`, regardless of `PANDOCS_DIR`.
- **Internal reasoning leaked to customers** — when `response_draft` was absent, the router fell back to posting `decision.reasoning` as annotation text, exposing AI-internal reasoning to ticket requesters. Fixed: `alert` posts `reasoning` (internal note); `clarify`/`auto_respond`/`remind` require a non-empty `response_draft` and skip the API call silently if absent.
- **`AUTO_RESP` self-trigger loop (complete fix)** — `AutomaticResponse` annotations do not set `RequestedUserComments=True` in Proactivanet, so every subsequent Guardado arrives with `RequestedUserComments=False`; there is no reliable signal to distinguish the self-trigger from a customer update. Fixed: worker always skips when `_current_state == "AUTO_RESP"`. Confirmed end-to-end in production (INC 2026-000026: one annotation posted, self-trigger Guardado silently skipped). Stale detector also updated to never alert on `AUTO_RESP` tickets.
- **`CLR_REQ` self-trigger guard** — `CLR_REQ + RequestedUserComments=True` was not guarded, allowing re-evaluation on PanPilot's own clarification annotations. Fixed: split into a dedicated guard that skips when `CLR_REQ` and `RequestedUserComments=True`.

### Changed

- **Pass 1 `auto_respond` reframed as a classifier** — the prompt now instructs Claude to classify as `auto_respond` when the question is answerable from documentation; Claude no longer attempts to compose the answer in Pass 1 (Pass 2 RAG handles that). `no_doc_coverage` and `low_confidence` are noted as Pass 2 outcomes only.
- **Admin UI reasoning column** — the 200-character truncation on reasoning text in the audit table has been removed; full reasoning is shown with `white-space: pre-wrap` wrapping.
- **RAG gap fields capped at write time** — `gap_category` truncated to 100 chars, `gap_explanation` to 300 chars before DB insert (the JSON schema `maxLength` was advisory only).
- **`assert` replaced with `ValueError`** — `build_gap_analysis_message` now raises `ValueError` instead of `AssertionError` when `confidence` is None with chunks present; assertions can be stripped in optimized builds.

### Tests

- 9 new tests: router annotation text selection, `AUTO_RESP` self-trigger suppression (evaluate and route paths), `CLR_REQ` guard
- Total: 459 tests

---

## [0.2.0] - 2026-05-26

Phase 2 complete. RAG auto-response pipeline and cross-ticket org reminder cap.
Operating in DRY_RUN=true mode — no Proactivanet writes until sign-off.

### Added

**T17 — Cross-ticket org reminder cap**
- `enforce_org_reminder_cap()` in `caps.py` counts non-dry-run `remind` audit rows across all tickets sharing the same `requester_id` within a configurable rolling window (`REMINDER_ORG_WINDOW_DAYS`, default 30 days)
- Escalates to `needs_human` when the per-requester count reaches `REMINDER_ORG_MAX` (default 3), preventing spam escalation for persistent high-contact users
- `requester_id` extracted from `PanUsers_idSource` with fallback to `PadCustomers_id`; stored in `ticket_state.requester_id` for cross-ticket join
- `requester_id` capped at 128 chars on ingestion; `None` when both fields are absent or whitespace-only

**Feature 2 — RAG auto-response (Pass 2)**
- `panpilot/intelligence/rag.py` — new module implementing two-pass evaluation
  - `chunk_document()`: splits Markdown docs on `## ` headers + paragraph fallback with colon-guard (never splits an intro sentence from its bullet list)
  - `retrieve_relevant_chunks()`: embeds ticket title+description with `all-MiniLM-L6-v2` (384-dim, 256-token, CPU) and queries ChromaDB using `query_embeddings=` (never `query_texts=`)
  - `evaluate_with_context()`: calls Claude with ticket + top-k chunks via `record_rag_decision` tool; confidence clamped to [0.0, 1.0]
  - `rag_evaluate()`: full Pass 2 pipeline — retrieve → evaluate → confidence gate → `rag_misses` write on low confidence
  - Degrades gracefully: encode/query errors return `no_doc_coverage` instead of DLQ propagation
- `scripts/index_pandocs.py`: one-shot indexer for `~/pandocs` → ChromaDB `pandocs` collection; uses SHA-256 content hashing to skip unchanged chunks
- `scripts/rag_smoke_test.py`: end-to-end smoke test for Pass 1 + Pass 2 against real docs and real Claude call
- `build_rag_user_message()` and `RAG_DECISION_TOOL` added to `prompts.py`; doc chunks are `html.escape()`-d to harden against prompt injection in retrieved content
- `rag_misses` table added to schema — records ticket_id and question summary for admin review of documentation gaps
- `ticket_state.requester_id` column + partial index added to schema (nullable, no default)
- `RagDeps` dataclass with `.available` property wires model + collection into `process_event` at zero cost when RAG is not configured
- Webhook lifespan loads embedding model and ChromaDB collection at startup; degrades to `rag_deps=None` if pandocs are not configured; logs WARNING if collection is empty

**Test suite (Phase 2)**
- 83 new tests: T17 org cap (13), RAG engine (31), runner RAG wiring (5), runner requester_id extraction (7), caps alert passthrough, colon-guard regression, inside-window boundary, RAG decision substitution
- Total: 433 tests

### Changed

- `pyproject.toml`: `sentence-transformers>=5.5.1` and `chromadb>=1.5.9` promoted from dev to production dependencies
- `runner.process_event()`: RAG Pass 2 runs when `rag_deps.available` and Pass 1 returned `auto_respond`; the returned decision (which may be `low_confidence` or `no_doc_coverage`) replaces the original before routing
- `CHANGELOG.md [0.1.0] Known Limitations`: "Feature 2 (RAG auto-response) is not yet implemented" — now resolved

### Fixed

- Pass 2 confidence clamped to [0.0, 1.0] — prevents Claude returning `confidence: 1.2` from bypassing the threshold gate

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
