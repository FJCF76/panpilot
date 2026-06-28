# Configuration Reference

Complete reference for all PanPilot environment variables. These are loaded from `.env`
at startup via `panpilot/config.py` using pydantic-settings. Variables marked **required**
have no default and cause startup failure if unset.

---

## Proactivanet API

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PROACTIVANET_API_URL` | Yes | — | Base URL of the Proactivanet REST API, without trailing slash. Example: `https://your-instance.proactivanet.com/panet/api` |
| `PROACTIVANET_BASE_URL` | Yes | — | Web-facing URL of Proactivanet (no `/api` suffix), used for ticket deep-links in the admin panel. Example: `https://your-instance.proactivanet.com/panet` |
| `PROACTIVANET_API_KEY` | Yes | — | API key with write scope limited to `POST /api/Incidents/{id}/annotations`. |
| `PROACTIVANET_AUTHOR_ID` | Yes | — | UUID of the Proactivanet technician account PanPilot posts annotations as. Find it in Proactivanet under Administración → Técnicos → select the PanPilot account → copy the Id field. **Must not be empty** — an empty value disables the annotation self-trigger guard, causing an infinite loop where PanPilot evaluates its own annotations. |

---

## Anthropic

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes | — | Anthropic API key. Used for both Pass 1 (ticket triage, model: claude-sonnet-4-6) and Pass 2 (RAG auto-response). |

---

## Admin interface

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ADMIN_USERNAME` | No | `admin` | Username for the `/admin/` panel (HTTP Basic Auth). |
| `ADMIN_PASSWORD` | Yes | — | Password for the `/admin/` panel. Must be non-empty — an empty value allows unauthenticated access. |

---

## Automation thresholds

These control when PanPilot takes automated actions. Defaults are conservative and suitable
for most deployments. Adjust after reviewing audit log data from the dry-run period.

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIDENCE_THRESHOLD` | `0.85` | Minimum confidence score (0.0–1.0) required for an auto-response to be sent. Below this threshold, the ticket is logged as a documentation gap and escalated. A value of 0.85 means PanPilot is right ~85% of the time before auto-responding. |
| `CLARIFICATION_MAX` | `2` | Maximum clarification questions PanPilot asks per ticket before escalating to `NEEDS_HUMAN`. Prevents interrogating customers with repeated questions. |
| `REMINDER_MAX_PER_TICKET` | `2` | Maximum reminders PanPilot sends per ticket when the customer is silent after an agent contact. |
| `REMINDER_ORG_MAX` | `3` | Maximum reminders PanPilot sends to the same requester across all tickets within the `REMINDER_ORG_WINDOW_DAYS` window. Prevents spam to customers with multiple open tickets. |
| `REMINDER_ORG_WINDOW_DAYS` | `3` | Rolling window in days for the per-requester reminder cap. |
| `REMINDER_POLL_HOURS` | `8` | How often the reminder scheduler runs to check for tickets in `WAITING` state that need a follow-up. |
| `REMINDER_THRESHOLD_HOURS` | `24` | Hours a ticket must be in `WAITING` state (no customer response after an agent contact) before a reminder is sent. |
| `STALE_THRESHOLD_P1_HOURS` | `4` | Inactivity threshold in hours for P1 (critical) tickets before a stale alert fires. |
| `STALE_THRESHOLD_P2_HOURS` | `24` | Inactivity threshold in hours for P2 (high) tickets. |
| `STALE_THRESHOLD_P3_HOURS` | `120` | Inactivity threshold in hours for P3 (normal) tickets (5 days). |
| `STALE_ALERT_POLL_MINUTES` | `10` | How often the stale detector checks for inactive tickets. |

---

## Webhook

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBHOOK_SECRET` | `""` (disabled) | Shared secret expected in the `X-Webhook-Secret` header of incoming webhooks from Proactivanet. Leave empty to disable authentication — acceptable on a private internal network, not recommended for internet-facing deployments. |
| `WEBHOOK_IDEMPOTENCY_FIELD` | `""` (fallback) | JSON field name in the webhook payload containing a unique delivery ID (e.g. `"DeliveryId"`). When set, PanPilot uses this field as the idempotency key to detect and silently drop duplicate webhook deliveries. When empty, falls back to `sha256(ticket_id + event_type + DateLastModified)`. **Never point this at a timestamp field** — timestamps are not idempotent. Confirm the field name against your live Proactivanet webhook payloads before setting. |

---

## Manual exclusion

| Variable | Default | Description |
|----------|---------|-------------|
| `MANUAL_EXCLUSION_FIELD_ID` | `""` (text fallback) | UUID of the Proactivanet custom field used to exclude specific tickets from automation. A Proactivanet admin must create a custom field (type: boolean or text) on the Incident entity and provide its UUID here. When empty, PanPilot falls back to checking for the string `[panpilot-manual]` anywhere in the ticket Description field. |

---

## Operational mode

| Variable | Default | Description |
|----------|---------|-------------|
| `DRY_RUN` | `true` | When `true`, PanPilot evaluates every ticket and logs decisions to the audit log, but does not post any annotations to Proactivanet. Set to `false` only after completing the Week 1–2 dry-run validation. See the `dry_run` column in `audit_log` to distinguish simulated from real decisions. |

---

## Data storage

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_DIR` | `data` | Path to the directory where PanPilot stores its SQLite files (`panpilot.db`, `scheduler.db`). Relative to the working directory at startup (i.e., the repo root when running under the provided systemd unit). The directory is created automatically if it does not exist. |
| `PANDOCS_DIR` | `None` (RAG disabled) | Path to the directory containing Markdown documentation files for the RAG corpus. When unset or empty, the auto-respond path is disabled — all `auto_respond` decisions fall through to `none/no_doc_coverage`. Set to the absolute path of your pandocs directory (e.g. `/home/youruser/pandocs`). |
| `CHROMA_DIR` | `data/chroma` | Path to the ChromaDB persistence directory. Must match the `--chroma` argument used when running `scripts/index_pandocs.py`. |
| `RAG_TOP_K` | `5` | Number of documentation chunks retrieved per RAG query. Higher values give Claude more context but increase token cost. 5 is appropriate for most documentation sets. |

---

## Variable validation at startup

The following validations are enforced at startup by `panpilot/config.py`. If any fails,
the process exits with a descriptive error message rather than starting in a broken state:

- `PROACTIVANET_AUTHOR_ID` must not be empty.
- `ADMIN_PASSWORD` must not be empty.
- `CONFIDENCE_THRESHOLD` must be between 0.0 and 1.0.
- `PROACTIVANET_API_URL` and `PROACTIVANET_BASE_URL` have trailing slashes stripped automatically.

---

## Example `.env`

```env
# Proactivanet
PROACTIVANET_API_URL=https://your-instance.proactivanet.com/panet/api
PROACTIVANET_BASE_URL=https://your-instance.proactivanet.com/panet
PROACTIVANET_API_KEY=your-api-key-here
PROACTIVANET_AUTHOR_ID=a1b2c3d4-e5f6-7890-abcd-ef1234567890

# Anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Admin panel
ADMIN_USERNAME=admin
ADMIN_PASSWORD=choose-a-strong-password

# RAG corpus
PANDOCS_DIR=/home/youruser/pandocs
CHROMA_DIR=data/chroma

# Start in dry-run mode — switch to false after Week 2 validation
DRY_RUN=true

# All threshold variables below are optional (defaults shown)
# CONFIDENCE_THRESHOLD=0.85
# CLARIFICATION_MAX=2
# REMINDER_MAX_PER_TICKET=2
# REMINDER_ORG_MAX=3
# REMINDER_ORG_WINDOW_DAYS=3
# STALE_THRESHOLD_P1_HOURS=4
# STALE_THRESHOLD_P2_HOURS=24
# STALE_THRESHOLD_P3_HOURS=120
```

---

## Related documentation

- [`docs/howto-server-setup.md`](howto-server-setup.md) — step-by-step server setup guide
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — architecture, state machine, schema
