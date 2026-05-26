# PanPilot — Architecture Reference

Internal AI complement for Proactivanet S.A.'s support team.
Automates the administrative layer of ticket management so agents spend time solving, not managing.

**Product language:** Spanish. **Planning language:** English.

---

## Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Language | Python 3.12 | VPS already has it |
| Package manager | uv | Faster than pip, lockfile included |
| Webhook receiver | FastAPI | Native async required for non-blocking 200 response |
| Admin interface | FastAPI + Jinja2 | Same process, no extra framework |
| Background scheduler | APScheduler 3.x | SQLite job store, coalesce=True for stale detector |
| Database | SQLite via `sqlite3` | Single-server internal tool; adequate at this volume |
| AI | Anthropic Python SDK | Claude evaluation engine + Files API for RAG |
| Process manager | systemd | VPS-native, no Docker needed |
| Testing | pytest | Standard; fixtures for mocked Claude + Proactivanet |
| Package tool | uv | `uv run`, `uv sync`, `uv add` |

---

## Three-Layer Architecture

```
Layer 1: Intake        panpilot/intake/
  Webhook receiver (FastAPI), event store write, idempotency check,
  APScheduler stale detector. Returns HTTP 200 BEFORE evaluation starts.
  Never calls Claude. Never calls Proactivanet write endpoints.
  Hosts a DB-backed worker polling thread that picks up unprocessed events
  and dispatches to Layer 2. Crash-safe: unprocessed events survive restart.

Layer 2: Intelligence  panpilot/intelligence/
  evaluate_ticket() → Decision. Single entry point. Single Claude call
  (two calls for Feature 2 / auto_respond path). No external side effects.
  Never calls Proactivanet. Never writes to SQLite directly.

Layer 3: Execution     panpilot/execution/
  Action router (allowlist: post_comment only). Proactivanet API client.
  Audit log writer (append-only). Never reads raw ticket data.
  Never calls Claude directly.
```

No layer calls into a non-adjacent layer.

---

## FastAPI Lifespan Contract (intake/webhook.py)

The `@asynccontextmanager` lifespan function in `intake/webhook.py` owns all
startup and shutdown sequencing. Order is strictly defined:

```
STARTUP (in order):
  0. Configure logging
       logging.basicConfig(level=logging.INFO,
           format="%(asctime)s %(name)s %(levelname)s %(message)s")
       All layers use logging.getLogger(__name__). Captured by journald via stdout.

  1. Load reference data (T18)
       GET /api/Priorities → cache {uuid: "P1"|"P2"|"P3"} by Sort rank
       GET /api/Status     → cache {uuid: status_name} for state machine
       Stored in app.state.priority_map and app.state.status_map
       Raises on failure — service must not start without reference data

  2. Start APScheduler with SQLite job store (T6)
       coalesce=True, misfire_grace_time=60s
       Stale detector job registered here

  3. Start DLQ background thread (T4)
       daemon thread, exception-safe polling loop
       Reads dlq WHERE exhausted=0 AND next_retry <= NOW()

  4. Start DB-backed worker polling thread (worker)
       daemon thread, polls events WHERE processed=0
       Dispatches each event to evaluate_ticket() → router → audit

SHUTDOWN (reverse order on SIGTERM):
  4. Signal worker thread to stop, join with timeout
  3. Signal DLQ thread to stop, join with timeout
  2. Shut down APScheduler (scheduler.shutdown(wait=False))
  1. Reference data maps are in-memory; no cleanup needed

# single worker required — APScheduler uses SQLite job store;
# multiple workers would each create a scheduler instance.
# Scale by adding a separate panpilot-worker.service, not --workers N.
```

---

## Directory Map

```
panpilot/                          ← repo root
├── pyproject.toml                 ← uv-managed dependencies
├── .env.example                   ← required env vars (no defaults for secrets)
│
├── panpilot/                      ← main package
│   ├── config.py                  ← env var loading + validation (fail fast on missing)
│   │
│   ├── db/
│   │   ├── schema.sql             ← All table definitions + PRAGMA journal_mode=WAL
│   │   └── connection.py          ← get_connection(db_path) helper; applies WAL +
│   │                                 synchronous=NORMAL pragmas on every connect.
│   │                                 All modules use this — never sqlite3.connect() directly.
│   │
│   ├── intake/                    ← Layer 1
│   │   ├── webhook.py             ← FastAPI app: POST /webhook/proactivanet
│   │   │                             writes event_store, returns 200, enqueues to worker
│   │   ├── event_store.py         ← SQLite: events table, idempotency key check
│   │   └── scheduler.py           ← APScheduler: stale ticket poll job (Feature 4)
│   │
│   ├── intelligence/              ← Layer 2
│   │   ├── models.py              ← Decision dataclass, none_reason enum, TicketState enum
│   │   ├── engine.py              ← evaluate_ticket(ticket, config) → Decision
│   │   │                             Pass 1: triage without docs
│   │   │                             Pass 2 (auto_respond only): with Files API doc IDs
│   │   ├── prompts.py             ← All Claude prompt templates
│   │   │                             ticket content always inside <ticket_content> tags
│   │   └── rag.py                 ← Files API client: upload, list, call with file_ids
│   │                                 confidence threshold enforcement
│   │
│   ├── execution/                 ← Layer 3
│   │   ├── router.py              ← Action router: maps Decision.action → executor
│   │   │                             raises PolicyViolation if action ∉ allowlist
│   │   ├── proactivanet.py        ← Proactivanet REST client
│   │   │                             read: GET /tickets, GET /ticket/{id}
│   │   │                             write: POST /ticket/{id}/comment ONLY
│   │   └── audit.py               ← Audit log: INSERT only, never UPDATE/DELETE
│   │                                 writes Decision + none_reason + ticket_id + ts
│   │
│   ├── db/
│   │   └── schema.sql             ← All table definitions (see Schema section below)
│   │
│   ├── admin/                     ← Admin web interface (FastAPI router)
│   │   ├── app.py                 ← FastAPI router mounted at /admin
│   │   │                             HTTP Basic Auth middleware
│   │   ├── dashboard.py           ← Stats aggregation: counts by action_type, week-on-week
│   │   ├── audit_log.py           ← Audit log read + filter (action_type, ticket_id, date)
│   │   ├── docs_mgmt.py           ← .md file upload → Files API, list indexed docs
│   │   ├── settings.py            ← Read + display masked env var values (read-only)
│   │   └── templates/             ← Jinja2 HTML templates (Spanish UI)
│   │       ├── base.html
│   │       ├── dashboard.html
│   │       ├── audit_log.html
│   │       ├── docs.html
│   │       └── settings.html
│   │
│   └── worker/
│       └── dlq.py                 ← DLQ processor: SELECT exhausted=0, retry with backoff
│                                     attempts 1/2/3 at 30s / 5min / 30min
│                                     after 3rd failure: exhausted=1, alert admin
│
├── tests/
│   ├── conftest.py                ← Fixtures: mock Claude client, mock Proactivanet API,
│   │                                 in-memory SQLite, sample ticket payloads
│   ├── unit/
│   │   ├── test_engine.py         ← evaluate_ticket() with mocked Claude responses
│   │   ├── test_router.py         ← PolicyViolation on non-allowlist actions
│   │   ├── test_audit.py          ← AUDIT_FAILED flag + halt behavior
│   │   └── test_prompts.py        ← Prompt injection: ticket inside XML tags, schema validation
│   └── integration/
│       ├── test_webhook_flow.py   ← Full: webhook → event store → evaluation → audit log
│       ├── test_idempotency.py    ← Duplicate webhook → only one audit entry
│       └── test_rag_flow.py       ← Two-pass evaluation: triage → RAG → confidence gate
│
├── scripts/
│   ├── dry_run.py                 ← Week 1-2: process all webhooks, log decisions, no API writes
│   └── startup_catchup.py         ← T10: on start, query PNet for tickets since last_processed_at
│
├── deploy/
│   ├── panpilot.service           ← systemd unit file (see Deployment section)
│   └── panpilot-nginx.conf        ← nginx TLS reverse proxy (panpilot.owncompute.com)
│
├── docs/
│   ├── ARCHITECTURE.md            ← this file
│   └── proactivanet_docs/         ← official .md docs for RAG (committed to repo)
│       └── .gitkeep
│
└── data/                          ← runtime SQLite files — gitignored
    ├── panpilot.db                ← events, audit_log, ticket_state, dlq, rag_misses
    └── scheduler.db               ← APScheduler job store
```

---

## SQLite Schema

```sql
-- event_store: Layer 1 write-before-process
CREATE TABLE events (
    id          TEXT PRIMARY KEY,          -- idempotency key from webhook
    ticket_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL,             -- JSON blob
    received_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    processed   INTEGER DEFAULT 0
);

-- ticket_state: per-ticket state machine
CREATE TABLE ticket_state (
    ticket_id   TEXT PRIMARY KEY,
    state       TEXT NOT NULL,             -- PENDING_EVALUATION | AUTO_RESP | CLR_REQ |
                                           -- WAITING | STALE_ALERT | PENDING_AGENT_ACTION |
                                           -- NEEDS_HUMAN | AWAITING_CLIENT_REPLY
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    clarification_count INTEGER DEFAULT 0,
    reminder_count      INTEGER DEFAULT 0
);

-- audit_log: append-only, no UPDATE/DELETE permitted to app user
CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       TEXT NOT NULL,
    evaluated_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    action          TEXT NOT NULL,         -- clarify|auto_respond|remind|alert|none
    none_reason     TEXT,                  -- no_doc_coverage|low_confidence|etc.
    reasoning       TEXT NOT NULL,         -- Claude's reasoning, verbatim
    confidence      REAL,                  -- 0.0-1.0, Feature 2 only
    response_draft  TEXT,
    dry_run         INTEGER DEFAULT 0,     -- 1 = dry-run mode, no actual API write
    flagged_by      TEXT,                  -- Phase 2: agent who flagged this decision
    flag_reason     TEXT                   -- Phase 2: why they flagged it
);

-- dead_letter_queue
CREATE TABLE dlq (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    error       TEXT NOT NULL,
    attempts    INTEGER DEFAULT 0,
    next_retry  DATETIME,
    exhausted   INTEGER DEFAULT 0,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- rag_misses: E3 documentation gap report
CREATE TABLE rag_misses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id        TEXT NOT NULL,
    question_summary TEXT NOT NULL,        -- 1-sentence summary of unanswered question
    evaluated_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## Per-Ticket State Machine

```
TICKET CREATED
      │
      ▼
PENDING_EVALUATION ──(webhook stored, worker picks up)──▶ EVALUATED
                                                               │
                          ┌────────────────────────────────────┤
                          ▼          ▼         ▼          ▼    ▼
                     AUTO_RESP   CLR_REQ   WAITING   STALE  NEEDS_HUMAN
                          │          │    (remind)   ALERT       │
                          │          │                 │         │
                          └─────┬────┘    PENDING_AGENT_ACTION   │
                                ▼               │           no further
                      AWAITING_CLIENT_REPLY  new event      auto actions;
                      (new client event      resets to      flagged in
                       resets to             PENDING_       audit log;
                       PENDING_EVALUATION)   EVALUATION     admin alerted
```

**NEEDS_HUMAN is entered when:**
- Race condition handler detects two consecutive concurrent ticket updates (T15)
- Clarification cap reached (T11)
- Reminder cap reached (T16)
- Action router receives an unrecognized Decision action type

No automatic actions taken on NEEDS_HUMAN tickets. Subsequent webhooks are logged as
`none / no_action_warranted` until an agent changes the ticket state, which resets to
PENDING_EVALUATION.

---

## Feature 2: Two-Pass Evaluation (RAG)

```
evaluate_ticket() called
        │
        ▼
   Pass 1: Claude call with ticket content only
   → Decision with preliminary action
        │
        ├── action != "auto_respond" → finalize Decision, done (1 Claude call total)
        │
        └── action == "auto_respond"
                │
                ▼
           Pass 2: Claude call with ticket content + Files API doc file_ids
           → Final Decision with confidence score
                │
                ├── confidence >= threshold → post auto-response
                └── confidence < threshold → none / low_confidence (no API write)
```

**Token cost:** Features 1/3/4 use 1 Claude call. Feature 2 uses 2 calls (triage + RAG).
Doc files are never loaded for non-knowledge-question evaluations.
**Corpus limit:** All doc files passed in-context. Feasible up to ~200K tokens (~400 pages).
Confirm corpus size before writing any Feature 2 code.
**Files API caching:** `rag.py` must cache the uploaded file IDs at startup (or on first upload)
and reuse them across evaluations. Do NOT re-upload docs on every Pass 2 call.
If a file ID returns 404 (TTL expired), re-upload once and update the cached ID.

---

## Deployment (systemd)

```ini
# deploy/panpilot.service
[Unit]
Description=PanPilot AI Complement for Proactivanet
After=network.target

[Service]
Type=simple
User=wfroot-n5i9y
WorkingDirectory=/home/wfroot-n5i9y/panpilot
EnvironmentFile=/home/wfroot-n5i9y/panpilot/.env
ExecStart=/home/wfroot-n5i9y/panpilot/.venv/bin/uvicorn panpilot.intake.webhook:app --host 127.0.0.1 --port 8000 --workers 1
# --workers 1 is required: APScheduler uses a SQLite job store;
# multiple workers each create a scheduler instance causing duplicate stale alerts.
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

SQLite data directory: `~/panpilot/data/` — persists across restarts automatically (no volume mounts needed).

---

## Operational Notes

### DLQ exhaustion — no proactive alerting in Phase 1

When all three DLQ retry attempts fail, the entry is marked `exhausted=1` and no
further action is taken automatically.  Monitor with:

```
journalctl -u panpilot.service -p err
```

Look for log lines containing `"exhausted"` or `"DLQ"`.  The admin UI at `/admin/dlq`
also shows exhausted entries with a per-entry retry button.  Automated alerting
(e.g. email/webhook on exhaustion) is deferred to Phase 2.

### Annotation length cap

`execution/router.py` hard-truncates the annotation text sent to Proactivanet to
4000 characters (`_MAX_ANNOTATION_LEN`).  The full `response_draft` is stored in the
audit log before truncation — only the outbound annotation is capped.  Typical
support responses are well under this limit.  Revisit in Phase 2 if longer
auto-responses are needed.

---

## Required Environment Variables

```bash
# .env.example
PROACTIVANET_API_URL=https://your-instance.proactivanet.com/api
PROACTIVANET_API_KEY=             # write scope: POST /ticket/{id}/comment only
ANTHROPIC_API_KEY=                # Claude API + Files API
ADMIN_USERNAME=                   # HTTP Basic Auth for /admin
ADMIN_PASSWORD=                   # HTTP Basic Auth for /admin

# Thresholds (defaults shown)
CONFIDENCE_THRESHOLD=0.85
CLARIFICATION_MAX=2
REMINDER_MAX_PER_TICKET=2
REMINDER_ORG_WINDOW_DAYS=3
STALE_THRESHOLD_P1_HOURS=4
STALE_THRESHOLD_P2_HOURS=24
STALE_THRESHOLD_P3_HOURS=120

# Idempotency key for webhook deduplication.
# Set to the JSON field name in the webhook payload that contains a
# unique delivery ID (confirm against live Proactivanet instance).
# If no delivery ID exists, leave unset — fallback uses
# sha256(ticket_id + event_type + DateLastModified).
# NEVER use receive timestamp alone — that is NOT idempotent.
WEBHOOK_IDEMPOTENCY_FIELD=        # e.g. "DeliveryId" — confirm with live instance

# UUID of the custom field configured in Proactivanet for manual exclusion (T13).
# A Proactivanet admin must create a custom field on the Incident entity
# (type: text or boolean, name: e.g. "panpilot_exclude") and provide its UUID here.
# If unset, T13 falls back to checking for "[panpilot-manual]" in Description.
MANUAL_EXCLUSION_FIELD_ID=        # confirm with Proactivanet admin before T13

# Mode
DRY_RUN=true                      # set false only after Week 2 validation
```

---

## Implementation Tasks → File Map

| Task | File(s) | Priority |
|------|---------|----------|
| T1: Structured JSON + schema validation | `intelligence/engine.py`, `intelligence/models.py` | P1 |
| T2: Write-before-process + idempotency | `intake/webhook.py`, `intake/event_store.py`, `db/schema.sql` | P1 |
| T3: Action router allowlist | `execution/router.py` | P1 |
| T4: Dead-letter queue | `worker/dlq.py`, `db/schema.sql` | P1 |
| T5: Audit log halt (AUDIT_FAILED flag) | `execution/audit.py`, `config.py` | P1 |
| T6: APScheduler job lock | `intake/scheduler.py` | P1 |
| T7: Prompt injection hardening | `intelligence/prompts.py` | P1 |
| T8: Credential storage (env vars only) | `config.py`, `admin/settings.py` | P1 |
| T9: Per-ticket state machine | `db/schema.sql`, new `intake/state.py` | P2 |
| T10: Startup catch-up | `scripts/startup_catchup.py`, `intake/webhook.py` | P2 |
| T11: Clarification cap | `execution/router.py`, `config.py` | P2 |
| T12: Files API quota rescue | `intelligence/rag.py` | P2 |
| T13: Manual exclusion tag | `intelligence/engine.py`, `config.py` | P2 |
| T14: Data governance sign-off | (ops — no code) | P2 |
| T15: Race condition handler | `execution/router.py`, `execution/proactivanet.py` | P1 |
| T16: Per-ticket reminder cap | `execution/router.py`, `db/schema.sql` (reminder_count) | P2 |
| T17: Cross-ticket org reminder cap | `execution/router.py`, `execution/audit.py` | P2 |
| T18: Startup reference data loader | `intake/reference_data.py`, `intake/webhook.py`, `config.py` | P1 |
| T19: DLQ retry button in admin UI | `admin/app.py`, `worker/dlq.py` | P2 |

T19: Retry button on exhausted DLQ entries. Resets `exhausted=0, attempts=0, next_retry=NOW()`.
This is the only write operation in the admin UI. It only touches PanPilot's internal queue —
no Proactivanet write calls — so it does not violate the "write scope = comments/annotations only" constraint.

**Build order for P1 tasks (updated):**
```
T8 (config) → T18 (reference data) → T2 (event store) → T1 (models + engine) → T7 (prompts)
→ T3 (router) → T5 (audit) → T4 (DLQ) → T6 (scheduler) → T15 (race condition)
```
T18 comes after T8 (config provides the API client) and before T2/T1 (stale detector needs the priority map).

**Phase 1 tasks (deploy dry-run):** T8, T18, T2, T1, T7, T3, T5, T4, T6, T15, T9, T10, T11
**Phase 2 tasks (before feature activation):** T12, T13, T16, T17, T19
**Ops task (before live mode):** T14 (data governance sign-off)

---

## Open Questions (must resolve before writing the named code)

1. **Proactivanet webhook payload schema** — what fields does the webhook include?
   Determines field-level validation in `intake/webhook.py`. Design receiver to be
   schema-flexible with runtime validation until confirmed against live instance.
   Also: does the webhook include a unique delivery ID field? If yes, set as
   `WEBHOOK_IDEMPOTENCY_FIELD`; if no, use sha256(ticket_id+event_type+DateLastModified).

   **[RESOLVED — PARTIAL]**: Webhook event types confirmed: Creación, Guardado,
   En anotación, Cambio de estado. Exact JSON payload schema not yet documented.
   Custom header (e.g. X-APIKey) available for authentication.

2. **Proactivanet REST `updated_since` filter** — RESOLVED: `GET /api/Incidents`
   supports `DateLastModified` (date-time) query parameter. T10 startup catch-up
   uses `?DateLastModified={last_processed_at}&Status=New,Assigned`.
   No full ticket pagination required on restart.

3. **Doc corpus size** — total tokens across all official Proactivanet `.md` files?
   If > ~200K tokens, the two-pass Files API approach for Feature 2 needs a vector
   retrieval layer. Validate during Week 1 dry-run BEFORE building Feature 2.

4. **Admin interface access pattern** — RESOLVED:
   nginx terminates TLS at panpilot.owncompute.com. Both /webhook and /admin proxy to
   127.0.0.1:8000. HTTP Basic Auth (ADMIN_USERNAME/ADMIN_PASSWORD) enforced by FastAPI;
   credentials are safe because they only cross the wire over TLS. See deploy/panpilot-nginx.conf.
   Deploy: copy to /etc/nginx/sites-available/panpilot, run certbot --nginx -d panpilot.owncompute.com.

5. **Comment `Type` field values** — RESOLVED (confirmed against live instance):
   - Correct endpoint: POST /api/Incidents/{id}/annotations (not /comments)
   - Type field: "Technician" or "User"
   - Visibility is controlled by ActionTypeId, not Type:
     - Customer-visible: UserTextQuestion (also sets RequestedUserComments=true on ticket),
       PublishedAction, AutomaticResponse
     - Internal only: Annotation
   - HasSentMail in response confirms whether client was notified
   - Feature mapping:
     - Feature 1 clarification  → ActionTypeId=UserTextQuestion
     - Feature 2 auto-response  → ActionTypeId=AutomaticResponse
     - Feature 3 reminder       → ActionTypeId=PublishedAction
     - Feature 4 stale alert    → ActionTypeId=Annotation (internal)
   - post_annotation(ticket_id, text, action_type) in execution/proactivanet.py
     maps action_type enum → confirmed ActionTypeId values.
   - NOTE: UserTextQuestion automatically sets RequestedUserComments=true on the ticket.
     Feature 3 (reminder) can therefore detect "waiting for client reply" state via
     GET /api/Incidents?RequestedUserComments=true without needing separate state tracking.

6. **Priority name convention** — do Proactivanet priority names follow a P1/P2/P3
   convention or something else? T18 maps by Sort rank (rank 1 = P1) as default;
   confirm this matches the instance's configuration.

7. **T13 custom field config** — Proactivanet admin must create a custom field
   (e.g. "panpilot_exclude", type boolean) on the Incident entity and provide its UUID
   as `MANUAL_EXCLUSION_FIELD_ID`. Fallback: "[panpilot-manual]" in Description field.
   Confirm approach with Proactivanet admin before T13 implementation.
