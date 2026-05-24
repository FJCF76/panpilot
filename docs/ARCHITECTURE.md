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

## Directory Map

```
panpilot/                          ← repo root
├── pyproject.toml                 ← uv-managed dependencies
├── .env.example                   ← required env vars (no defaults for secrets)
│
├── panpilot/                      ← main package
│   ├── config.py                  ← env var loading + validation (fail fast on missing)
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
│   └── panpilot.service           ← systemd unit file (see Deployment section)
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
ExecStart=/home/wfroot-n5i9y/panpilot/.venv/bin/uvicorn panpilot.intake.webhook:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

SQLite data directory: `~/panpilot/data/` — persists across restarts automatically (no volume mounts needed).

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

**Build order for P1 tasks:**
```
T8 (config) → T2 (event store) → T1 (models + engine) → T7 (prompts)
→ T3 (router) → T5 (audit) → T4 (DLQ) → T6 (scheduler) → T15 (race condition)
```
Config and event store first — everything else depends on them.

---

## Open Questions (must resolve before writing the named code)

1. **Proactivanet webhook payload schema** — what fields does the webhook include?
   Determines field-level validation in `intake/webhook.py` and whether ticket
   priority is available without a second API call (required for T6/scheduler + E2 thresholds).

2. **Proactivanet REST `updated_since` filter** — does `GET /tickets` support it?
   If not, `scripts/startup_catchup.py` (T10) must paginate all open tickets on every
   restart. Scope and performance risk changes significantly.

3. **Doc corpus size** — total tokens across all official Proactivanet `.md` files?
   If > ~200K tokens, the two-pass Files API approach for Feature 2 needs a vector
   retrieval layer added before `intelligence/rag.py` can be designed.

4. **Admin interface access pattern** — is the VPS directly accessible on port 8001
   (admin runs on a separate port), or does it need a reverse proxy (nginx) in front?
   Determines whether `deploy/panpilot.service` needs a companion nginx config.
