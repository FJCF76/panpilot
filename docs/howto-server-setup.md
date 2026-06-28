# How to Deploy PanPilot from Scratch

This guide walks through deploying a production PanPilot instance on a fresh Ubuntu/Debian VPS,
from zero to receiving and processing live Proactivanet tickets.

**Time estimate:** 45–90 minutes on a clean server.

**End result:** PanPilot running under systemd, fronted by nginx with TLS, processing
webhooks from Proactivanet in dry-run mode ready for Week 1 validation.

---

## Prerequisites

Before starting, you need:

- A VPS running Ubuntu 22.04+ or Debian 12+ with at least 1 GB RAM and 10 GB disk.
- A domain name (or subdomain) pointing to the VPS — e.g. `panpilot.yourcompany.com`.
- Access to your Proactivanet instance as an administrator (to create a technician account and configure the webhook).
- An Anthropic API key with credits.
- Git access to the PanPilot repository.

---

## Step 1: Install system dependencies

```bash
sudo apt-get update && sudo apt-get install -y \
    python3.12 python3.12-venv python3.12-dev \
    nginx certbot python3-certbot-nginx \
    git curl build-essential
```

Install `uv` (Python package manager):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc   # or open a new shell
uv --version       # should print uv 0.x.x
```

---

## Step 2: Clone the repository

```bash
git clone <your-panpilot-repo-url> ~/panpilot
cd ~/panpilot
```

Install all Python dependencies into a local virtual environment:

```bash
uv sync
```

This creates `.venv/` and installs FastAPI, uvicorn, anthropic, chromadb,
sentence-transformers, APScheduler, and all other dependencies. First run
downloads the `all-MiniLM-L6-v2` embedding model (~90 MB) — subsequent runs
are instant.

Verify the install:

```bash
uv run python -c "import panpilot; print('OK')"
```

---

## Step 3: Create the Proactivanet technician account

PanPilot posts all annotations to Proactivanet as a dedicated technician account.
You need to create this account before configuring the service.

1. Log in to Proactivanet as an administrator.
2. Go to **Administración → Técnicos → Nuevo**.
3. Create a technician named `PanPilot` (or similar) with an email address you control.
4. After saving, open the technician's detail view and copy the **Id** field (a UUID like
   `a1b2c3d4-e5f6-...`). This is `PROACTIVANET_AUTHOR_ID`.

> **Why this matters:** PanPilot uses `PROACTIVANET_AUTHOR_ID` to exclude its own
> annotations from re-evaluation. An empty value causes an infinite self-trigger loop —
> PanPilot posts an annotation, Proactivanet fires a webhook, PanPilot evaluates it again,
> and so on. The application refuses to start with an empty value.

---

## Step 4: Configure environment variables

```bash
cp .env.example .env
chmod 600 .env   # secrets live here; restrict to owner only
```

Open `.env` and fill in every required value:

```env
# Proactivanet API
PROACTIVANET_API_URL=https://your-instance.proactivanet.com/panet/api
PROACTIVANET_BASE_URL=https://your-instance.proactivanet.com/panet
PROACTIVANET_API_KEY=<your-api-key>
PROACTIVANET_AUTHOR_ID=<uuid-from-step-3>

# Anthropic
ANTHROPIC_API_KEY=<your-anthropic-key>

# Admin interface
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<choose-a-strong-password>

# DRY_RUN must stay true until Week 2 validation is complete
DRY_RUN=true
```

The remaining variables have safe defaults. See
[`docs/reference-configuration.md`](reference-configuration.md) for the complete list.

Validate that the application reads the configuration without errors:

```bash
uv run python -c "from panpilot.config import get_settings; s = get_settings(); print('Config OK:', s.proactivanet_api_url)"
```

If this prints `Config OK: ...`, the required variables are set correctly.

---

## Step 5: Initialize the database

The database schema is created automatically on first startup. To initialize it
before the service starts (and verify there are no errors):

```bash
uv run python -c "
from panpilot.db.connection import get_connection
from pathlib import Path
Path('data').mkdir(exist_ok=True)
conn = get_connection('data/panpilot.db')
conn.close()
print('Schema initialized')
"
```

This creates `data/panpilot.db` with WAL mode enabled and all five tables
(`events`, `ticket_state`, `audit_log`, `dlq`, `rag_misses`).

---

## Step 6: Set up the RAG documentation corpus

PanPilot uses a local vector database (ChromaDB) to retrieve relevant documentation
before auto-responding to tickets. You need to populate this with your Proactivanet
documentation before enabling automatic responses.

### 6a. Prepare documentation files

Collect your Proactivanet documentation as Markdown files. Place them in a directory —
by convention, `~/pandocs`:

```bash
mkdir -p ~/pandocs
# Copy or create your .md documentation files here
# Example structure:
# ~/pandocs/webhooks.md
# ~/pandocs/ad-agent.md
# ~/pandocs/roles-permisos.md
```

Each file can optionally include YAML frontmatter for better search results:

```markdown
---
title: Configuración de webhooks
article_id: WBK-001
source_url: https://docs.yourcompany.com/webhooks
---

# Configuración de webhooks

...
```

### 6b. Index the documentation

```bash
uv run python scripts/index_pandocs.py --pandocs ~/pandocs --chroma data/chroma
```

Expected output:

```
Loading embedding model 'all-MiniLM-L6-v2' …
Done. Indexed 47 chunks from 8 docs (0 unchanged chunks skipped).
Collection 'pandocs' now contains 47 total chunks.
```

Re-run this command whenever you add or update documentation files. Unchanged
chunks are skipped (incremental indexing), so re-runs are fast.

### 6c. Enable RAG in .env

Add the pandocs path to `.env`:

```env
PANDOCS_DIR=~/pandocs
CHROMA_DIR=data/chroma
```

### 6d. Verify the RAG pipeline

```bash
uv run python scripts/rag_smoke_test.py
```

This sends a test query through the full retrieval pipeline and prints the top
matching chunks. If it returns results, RAG is working correctly.

---

## Step 7: Deploy the systemd service

### 7a. Customize the service file

Open `deploy/panpilot.service` and update the `User` and `WorkingDirectory` fields
to match your actual username and install path:

```ini
[Service]
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/panpilot
EnvironmentFile=/home/YOUR_USERNAME/panpilot/.env
ExecStart=/home/YOUR_USERNAME/panpilot/.venv/bin/uvicorn panpilot.intake.webhook:app \
    --host 127.0.0.1 --port 8000 --workers 1
```

> **`--workers 1` is required and must not be changed.** PanPilot's background
> scheduler (APScheduler) uses a SQLite job store. Multiple uvicorn workers would
> each create a separate scheduler instance, causing duplicate stale ticket alerts
> to fire for every ticket. Scale by adding a separate `panpilot-worker.service`,
> not by increasing `--workers`.

### 7b. Install and enable the service

```bash
sudo cp deploy/panpilot.service /etc/systemd/system/panpilot.service
sudo systemctl daemon-reload
sudo systemctl enable panpilot
sudo systemctl start panpilot
```

Check that it started cleanly:

```bash
sudo systemctl status panpilot
journalctl -u panpilot.service -n 50
```

Look for log lines like:
```
INFO     panpilot.intake.webhook:webhook.py:XX  PanPilot startup complete
INFO     apscheduler.schedulers.background:...  Scheduler started
```

If you see `ValueError: PROACTIVANET_AUTHOR_ID must be set`, go back to Step 4.

---

## Step 8: Configure nginx and TLS

### 8a. Customize the nginx config

Open `deploy/panpilot-nginx.conf` and replace the placeholder domain in both
`server_name` directives with your actual domain name.

### 8b. Install the nginx config

```bash
sudo cp deploy/panpilot-nginx.conf /etc/nginx/sites-available/panpilot
sudo ln -s /etc/nginx/sites-available/panpilot /etc/nginx/sites-enabled/panpilot
sudo nginx -t   # verify config syntax
sudo systemctl reload nginx
```

### 8c. Obtain a TLS certificate

```bash
sudo certbot --nginx -d your-domain.com
```

Certbot modifies the nginx config in place to add the certificate paths and
redirect HTTP → HTTPS. After this, reload nginx:

```bash
sudo systemctl reload nginx
```

Verify the service is reachable:

```bash
curl -s -o /dev/null -w "%{http_code}" https://your-domain.com/webhook/proactivanet
# Expected: 405 (POST-only endpoint, GET returns Method Not Allowed)
```

---

## Step 9: Configure the Proactivanet webhook

In your Proactivanet instance, configure a webhook to notify PanPilot on ticket events:

1. Go to **Administración → Webhooks → Nuevo**.
2. Set the URL to: `https://your-domain.com/webhook/proactivanet`
3. Enable events: **Creación**, **Guardado**, **En anotación**, **Cambio de estado**.
4. If your Proactivanet instance supports a shared secret header, set it and add
   the same value to `.env` as `WEBHOOK_SECRET=<value>`. Leave empty if not available.
5. Save and send a test event.

Verify the webhook is received:

```bash
journalctl -u panpilot.service -f
# You should see: INFO ... event_store ... stored event <id>
```

---

## Step 10: Week 1–2 dry-run validation

PanPilot starts with `DRY_RUN=true`. In this mode, it evaluates every ticket
and logs every decision to the audit log, but **does not write anything to
Proactivanet**. This is the correct way to validate behavior before going live.

### What to check during dry-run

Open the admin panel at `https://your-domain.com/admin/` (username/password
from `.env`). Verify:

1. **Auditoría tab:** Every incoming ticket appears with an action and reasoning.
   Check that `clarify` decisions ask sensible questions, `auto_respond` decisions
   use correct documentation, and `none` decisions have valid reasons.

2. **Lagunas de documentación tab:** Review the list of documentation gaps. These
   are tickets where PanPilot wanted to auto-respond but couldn't because the
   documentation coverage was insufficient or confidence was below 85%. Add the
   missing documentation to `~/pandocs` and re-run `index_pandocs.py`.

3. **Cola de errores tab:** Any events that failed processing appear here. If the
   DLQ has entries, check `journalctl -u panpilot.service -p err` for the underlying
   error and retry from the admin panel.

Run the dry-run for at least one week, covering enough ticket volume to see all
four automation behaviors (auto_respond, clarify, remind, alert).

---

## Step 11: Go live

After validating dry-run behavior:

1. Edit `.env` and set `DRY_RUN=false`.
2. Restart the service:

```bash
sudo systemctl restart panpilot
```

3. Monitor the first live annotations in Proactivanet and cross-check against the
   audit log in the admin panel.

> **Rollback:** To revert to dry-run mode at any time, set `DRY_RUN=true` in `.env`
> and restart. No data is lost — the audit log retains the `dry_run` flag on every
> entry so you can distinguish simulated from real decisions.

---

## Ongoing operations

### Monitor logs

```bash
journalctl -u panpilot.service -f           # live log stream
journalctl -u panpilot.service -p err       # errors only
journalctl -u panpilot.service --since "1h ago"  # last hour
```

### Update documentation and re-index

Whenever you add or update files in `~/pandocs`:

```bash
uv run python scripts/index_pandocs.py --pandocs ~/pandocs --chroma data/chroma
sudo systemctl restart panpilot   # not strictly required, but clears the RAG cache
```

### Check for exhausted DLQ entries

Events that fail three retry attempts are marked exhausted and require manual
attention. Check the admin panel's **Cola de errores** tab, or:

```bash
sqlite3 data/panpilot.db "SELECT event_id, error, attempts FROM dlq WHERE exhausted=1;"
```

Fix the underlying error, then click **Reintentar** in the admin panel or:

```bash
sqlite3 data/panpilot.db "UPDATE dlq SET exhausted=0, attempts=0, next_retry=datetime('now') WHERE exhausted=1;"
```

### Backup

The entire runtime state lives in `data/panpilot.db` and `data/scheduler.db`.
Back up the `data/` directory regularly:

```bash
sqlite3 data/panpilot.db ".backup data/panpilot.db.bak"
```

### Update PanPilot

```bash
cd ~/panpilot
git pull
uv sync
sudo systemctl restart panpilot
```

---

## Troubleshooting

### Service fails to start: `ValueError: PROACTIVANET_AUTHOR_ID must be set`

The `PROACTIVANET_AUTHOR_ID` is empty in `.env`. Follow Step 3 to create the
PanPilot technician account and copy its UUID.

### Service fails to start: reference data fetch failed

PanPilot fetches priority and status maps from the Proactivanet API at startup.
If the API is unreachable or the key is invalid, startup aborts. Check:

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" https://your-instance.proactivanet.com/panet/api/Priorities
```

### Webhooks arrive but no audit entries appear

The event is stored but the worker has not processed it yet (or is failing silently).
Check:

```bash
sqlite3 data/panpilot.db "SELECT id, ticket_id, processed FROM events ORDER BY received_at DESC LIMIT 10;"
journalctl -u panpilot.service -p err
```

If `processed=0` entries accumulate, the worker thread has likely crashed. A
`sudo systemctl restart panpilot` recovers it; unprocessed events are picked up
automatically on restart.

### Auto-responses not triggering even with documentation indexed

1. Check `CONFIDENCE_THRESHOLD` in `.env` (default 0.85). If the documentation
   is thin, confidence may be below threshold — check the **Lagunas** tab.
2. Verify RAG is enabled: `PANDOCS_DIR` must be set and non-empty.
3. Run `uv run python scripts/rag_smoke_test.py` to confirm the RAG pipeline works.

### TLS certificate renewal

Certbot installs a systemd timer that auto-renews. Verify it is active:

```bash
sudo systemctl status certbot.timer
sudo certbot renew --dry-run   # test renewal without committing
```

---

## Related documentation

- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — three-layer architecture, state machine, schema
- [`docs/reference-configuration.md`](reference-configuration.md) — complete environment variable reference
- [`CHANGELOG.md`](../CHANGELOG.md) — version history
