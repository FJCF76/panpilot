-- PanPilot SQLite schema
-- Applied by db/connection.py:init_db() at startup via executescript().
-- All CREATE statements use IF NOT EXISTS — init_db() is idempotent.

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- event_store: Layer 1 write-before-process work queue.
-- id is the idempotency key — INSERT OR IGNORE silently drops duplicates.
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,          -- idempotency key (delivery ID or sha256 fallback)
    ticket_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL,             -- Creación | Guardado | En anotación | Cambio de estado
    payload     TEXT NOT NULL,             -- JSON blob, full webhook payload
    received_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    processed   INTEGER DEFAULT 0         -- 0 = pending, 1 = done
);

CREATE INDEX IF NOT EXISTS idx_events_pending ON events (processed, received_at)
    WHERE processed = 0;

-- ticket_state: per-ticket state machine (T9).
-- clarification_count and reminder_count enforced by T11/T16.
-- priority written by T9 when first processing a ticket; read by T6 stale detector.
-- requester_id stores PanUsers_idSource (fallback PadCustomers_id) for T17 org-level cap.
CREATE TABLE IF NOT EXISTS ticket_state (
    ticket_id            TEXT PRIMARY KEY,
    state                TEXT NOT NULL,    -- PENDING_EVALUATION | AUTO_RESP | CLR_REQ |
                                           -- WAITING | STALE_ALERT | PENDING_AGENT_ACTION |
                                           -- NEEDS_HUMAN | AWAITING_CLIENT_REPLY
    priority             TEXT,             -- P1 | P2 | P3; NULL until T9 sets it
    updated_at           TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    clarification_count  INTEGER DEFAULT 0,
    reminder_count       INTEGER DEFAULT 0,
    requester_id         TEXT              -- T17: PanUsers_idSource or PadCustomers_id
);

-- audit_log: append-only record of every Decision.
-- No UPDATE or DELETE is ever issued against this table by application code.
-- flagged_by / flag_reason reserved for Phase 2 agent feedback UI.
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       TEXT NOT NULL,
    ticket_code     TEXT,                  -- human-readable code, e.g. "INC 2026-000001"
    evaluated_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    action          TEXT NOT NULL,         -- clarify | auto_respond | remind | alert | none
    none_reason     TEXT,                  -- no_doc_coverage | low_confidence | needs_human | etc.
    reasoning       TEXT NOT NULL,         -- Claude's reasoning, translated to Spanish
    confidence      REAL,                  -- 0.0–1.0, populated on auto_respond path only
    response_draft  TEXT,                  -- populated on auto_respond path only
    dry_run         INTEGER DEFAULT 0,     -- 1 when DRY_RUN=true; no Proactivanet write was made
    flagged_by      TEXT,                  -- Phase 2
    flag_reason     TEXT                   -- Phase 2
);

CREATE INDEX IF NOT EXISTS idx_audit_ticket ON audit_log (ticket_id, evaluated_at);

-- dead_letter_queue: events that failed processing after all retries.
-- Retry schedule: 30s → 5min → 30min. After 3rd failure: exhausted=1, admin alerted.
CREATE TABLE IF NOT EXISTS dlq (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    error       TEXT NOT NULL,
    attempts    INTEGER DEFAULT 0,
    next_retry  TEXT,
    exhausted   INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_dlq_pending ON dlq (exhausted, next_retry)
    WHERE exhausted = 0;

-- rag_misses: documentation gap report (E3).
-- Written when auto_respond confidence < threshold and no doc coverage found.
CREATE TABLE IF NOT EXISTS rag_misses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id        TEXT NOT NULL,
    question_summary TEXT NOT NULL,
    evaluated_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
