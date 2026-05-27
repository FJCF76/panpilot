"""
T5 — Admin interface: audit log read side.
T19 — DLQ retry trigger.

All routes require HTTP Basic Auth. The admin interface is read-only with
the single exception of the DLQ retry action (T19), which requeues an
exhausted event by resetting its DLQ row and clearing events.processed.

The admin interface never reads, writes, or displays environment variables.
"""
from __future__ import annotations

import base64
import json
import pathlib
import secrets
import sqlite3
from datetime import date as _date, datetime as _datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from panpilot.config import Settings, get_settings
from panpilot.db.connection import get_connection, main_db_path

router = APIRouter(prefix="/admin")
_security = HTTPBasic()


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def _require_auth(
    credentials: HTTPBasicCredentials = Depends(_security),
    settings: Settings = Depends(get_settings),
) -> None:
    ok_user = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.admin_username.encode("utf-8"),
    )
    ok_pass = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.admin_password.encode("utf-8"),
    )
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )


# ---------------------------------------------------------------------------
# DB connection helper (per-request, closed after response)
# ---------------------------------------------------------------------------

def _conn(settings: Settings = Depends(get_settings)) -> sqlite3.Connection:
    conn = get_connection(main_db_path(settings))
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /admin/audit  — list audit log entries
# ---------------------------------------------------------------------------

@router.get("/audit", dependencies=[Depends(_require_auth)])
def list_audit(
    ticket_id: str | None = None,
    action: str | None = None,
    dry_run: int | None = None,
    limit: int = 100,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(_conn),
) -> JSONResponse:
    """
    Return recent audit log entries as JSON.

    Optional filters: ticket_id (exact), action (exact), dry_run (0 or 1).
    Ordered by evaluated_at DESC. Max limit=500 per page.
    """
    limit = min(limit, 500)
    clauses: list[str] = []
    params: list = []

    if ticket_id is not None:
        clauses.append("ticket_id = ?")
        params.append(ticket_id)
    if action is not None:
        clauses.append("action = ?")
        params.append(action)
    if dry_run is not None:
        clauses.append("dry_run = ?")
        params.append(dry_run)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT id, ticket_id, ticket_code, evaluated_at, action, none_reason, reasoning, "
        f"confidence, response_draft, dry_run, flagged_by, flag_reason "
        f"FROM audit_log {where} ORDER BY evaluated_at DESC, id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    return JSONResponse({"entries": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# GET /admin/dlq  — list DLQ entries
# ---------------------------------------------------------------------------

@router.get("/dlq", dependencies=[Depends(_require_auth)])
def list_dlq(
    exhausted: int | None = None,
    limit: int = 100,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(_conn),
) -> JSONResponse:
    """
    Return DLQ entries as JSON. Default returns all (exhausted and pending).
    Pass ?exhausted=1 for failed-and-done entries; ?exhausted=0 for pending retries.
    """
    limit = min(limit, 500)
    clauses: list[str] = []
    params: list = []

    if exhausted is not None:
        clauses.append("exhausted = ?")
        params.append(exhausted)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT id, event_id, error, attempts, next_retry, exhausted, created_at "
        f"FROM dlq {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    return JSONResponse({"entries": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# POST /admin/dlq/{entry_id}/retry  — T19: requeue an exhausted DLQ event
# ---------------------------------------------------------------------------

@router.post("/dlq/{entry_id}/retry", dependencies=[Depends(_require_auth)], status_code=204)
def retry_dlq(
    entry_id: int,
    conn: sqlite3.Connection = Depends(_conn),
) -> None:
    """
    Requeue an exhausted DLQ entry by resetting its state and clearing
    events.processed so the worker polling thread will pick it up again.

    If the event fails again the worker will create a new DLQ entry.
    On success returns 204. Returns 404 if entry_id is not found.
    """
    row = conn.execute(
        "SELECT id, event_id FROM dlq WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    conn.execute("UPDATE events SET processed = 0 WHERE id = ?", (row["event_id"],))
    conn.execute("DELETE FROM dlq WHERE id = ?", (entry_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# GET /admin/rag-gaps  — documentation gap data
# ---------------------------------------------------------------------------

@router.get("/rag-gaps", dependencies=[Depends(_require_auth)])
def list_rag_gaps(
    conn: sqlite3.Connection = Depends(_conn),
) -> JSONResponse:
    """
    Return rag_misses data in two structures:
    - summary: grouped by gap_category with COUNT and latest timestamp
    - recent: most recent 100 individual miss rows
    """
    summary = [
        dict(r) for r in conn.execute(
            "SELECT gap_category, COUNT(*) AS count, MAX(evaluated_at) AS latest, "
            "MAX(CASE WHEN gap_explanation != '—' THEN gap_explanation END) AS sample_explanation "
            "FROM rag_misses WHERE gap_category IS NOT NULL "
            "GROUP BY gap_category ORDER BY count DESC LIMIT 50"
        ).fetchall()
    ]
    recent = [
        dict(r) for r in conn.execute(
            "SELECT id, ticket_id, ticket_code, question_summary, confidence, none_reason, "
            "chunk_sources, gap_category, gap_explanation, evaluated_at "
            "FROM rag_misses ORDER BY evaluated_at DESC LIMIT 100"
        ).fetchall()
    ]
    return JSONResponse({"summary": summary, "recent": recent})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_logo() -> str:
    logo_path = pathlib.Path(__file__).parent / "Logo.png"
    try:
        return base64.b64encode(logo_path.read_bytes()).decode()
    except OSError:
        return ""


_LOGO_B64 = _load_logo()  # once at import; empty string if file absent


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        dt = _datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d/%m/%Y %H:%M")
    except (ValueError, AttributeError):
        return _esc(ts) if ts else "—"


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

_ACTIONS = ["clarify", "auto_respond", "remind", "alert", "none"]


def _render_dlq(rows: list[dict]) -> str:
    if not rows:
        return '<p style="color:#198754;font-weight:500;">No hay entradas en la cola de errores.</p>'

    parts = [
        '<div style="overflow-x: auto">',
        "<table>",
        "<thead><tr>"
        "<th>ID</th><th>Evento</th><th>Intentos</th>"
        "<th>Próximo reintento</th><th>Agotado</th><th>Error</th><th>Acción</th>"
        "</tr></thead><tbody>",
    ]
    for r in rows:
        badge = (
            '<span class="badge badge-agotado-si">Sí</span>'
            if r["exhausted"]
            else '<span class="badge badge-agotado-no">No</span>'
        )
        retry_btn = (
            f'<form method="post" action="/admin/dlq/{r["id"]}/retry" style="display:inline">'
            '<button class="btn-outline">Reintentar</button></form>'
        )
        parts.append(
            f'<tr><td>{r["id"]}</td><td><code>{_esc(str(r["event_id"]))}</code></td>'
            f'<td>{r["attempts"]}</td><td>{_fmt_ts(r["next_retry"])}</td>'
            f'<td>{badge}</td>'
            f'<td><small>{_esc(str(r["error"])[:120])}</small></td>'
            f'<td>{retry_btn}</td></tr>'
        )
    parts += ["</tbody></table></div>"]
    return "\n".join(parts)


def _render_audit(rows: list[dict], base_url: str, filtered: bool = False) -> str:
    if not rows:
        if filtered:
            return (
                '<p style="color:#6c757d;">'
                'No se encontraron evaluaciones para este filtro. '
                '<a href="/admin/">Limpiar filtros</a></p>'
            )
        return '<p style="color:#6c757d;">Sin entradas.</p>'

    parts = [
        '<div style="overflow-x: auto">',
        "<table>",
        '<thead><tr>'
        '<th>ID</th><th>Ticket</th><th>Evaluado</th><th>Acción</th>'
        '<th>Dry run</th><th>Razonamiento</th>'
        '</tr></thead><tbody id="audit-tbody">',
    ]
    for r in rows:
        action = r["action"] or "none"
        action_badge = f'<span class="badge badge-{_esc(action)}">{_esc(action)}</span>'
        dr_badge = (
            '<span class="badge badge-dr-si">Sí</span>'
            if r["dry_run"]
            else '<span class="badge badge-dr-no">No</span>'
        )
        label = _esc(r["ticket_code"] or r["ticket_id"])
        ticket_link = (
            f'<a href="{_esc(base_url)}/servicedesk/incidents/formIncidents/formIncidents.paw'
            f'?id={_esc(r["ticket_id"])}" target="_blank" rel="noopener">{label}</a>'
        )
        parts.append(
            f'<tr><td>{r["id"]}</td>'
            f'<td>{ticket_link}</td>'
            f'<td>{_fmt_ts(r["evaluated_at"])}</td>'
            f'<td>{action_badge}</td>'
            f'<td>{dr_badge}</td>'
            f'<td><small style="white-space:pre-wrap;word-break:break-word">'
            f'{_esc(str(r["reasoning"] or ""))}</small></td></tr>'
        )
    parts += ["</tbody></table></div>"]
    return "\n".join(parts)


def _render_rag_gaps(summary_rows: list[dict], recent_rows: list[dict], base_url: str) -> str:
    parts: list[str] = []

    parts.append(
        '<h3 style="font-size:0.8rem;text-transform:uppercase;letter-spacing:0.05em;'
        'color:#6c757d;margin-bottom:0.5rem;">Lagunas por categoría</h3>'
    )
    if not summary_rows:
        msg = (
            "No hay lagunas de documentación registradas aún."
            if not recent_rows
            else "Sin lagunas categorizadas todavía."
        )
        parts.append(f'<p style="color:#6c757d;">{msg}</p>')
    else:
        parts += [
            '<div style="overflow-x: auto">',
            "<table>",
            "<thead><tr>"
            "<th>Categoría</th><th>Tickets</th><th>Última vez</th><th>Explicación de muestra</th>"
            "</tr></thead><tbody>",
        ]
        for r in summary_rows:
            parts.append(
                f'<tr>'
                f'<td>{_esc(r["gap_category"] or "—")}</td>'
                f'<td>{r["count"]}</td>'
                f'<td>{_fmt_ts(r["latest"])}</td>'
                f'<td><small style="word-break:break-word">'
                f'{_esc(r["sample_explanation"] or "—")}'
                f'</small></td>'
                f'</tr>'
            )
        parts += ["</tbody></table></div>"]

    parts.append(
        '<h3 style="font-size:0.8rem;text-transform:uppercase;letter-spacing:0.05em;'
        'color:#6c757d;margin-top:1.5rem;margin-bottom:0.5rem;">'
        'Consultas sin respuesta automática</h3>'
    )
    if not recent_rows:
        parts.append('<p style="color:#6c757d;">No hay consultas sin respuesta registradas aún.</p>')
    else:
        parts += [
            '<div style="overflow-x: auto">',
            "<table>",
            "<thead><tr>"
            "<th>Ticket</th><th>Pregunta</th><th>Confianza</th><th>Motivo</th>"
            "<th>Fuentes recuperadas</th><th>Explicación</th><th>Evaluado</th>"
            "</tr></thead><tbody>",
        ]
        for r in recent_rows:
            label = _esc(r["ticket_code"] or r["ticket_id"])
            ticket_link = (
                f'<a href="{_esc(base_url)}/servicedesk/incidents/formIncidents'
                f'/formIncidents.paw?id={_esc(r["ticket_id"])}"'
                f' target="_blank" rel="noopener">{label}</a>'
            )
            conf_str = f'{r["confidence"]:.0%}' if r["confidence"] is not None else "—"
            reason_str = (
                f'<code>{_esc(r["none_reason"])}</code>' if r["none_reason"] else "—"
            )
            try:
                sources = json.loads(r["chunk_sources"] or "[]")
                titles = [
                    s.get("title", "") or s.get("filename", "")
                    for s in sources
                    if isinstance(s, dict)
                ]
                titles = [t for t in titles if t]
                sources_str = _esc(", ".join(titles)) if titles else "—"
            except (json.JSONDecodeError, TypeError):
                sources_str = "—"
            parts.append(
                f'<tr>'
                f'<td>{ticket_link}</td>'
                f'<td><small style="word-break:break-word">'
                f'{_esc(r["question_summary"] or "")}</small></td>'
                f'<td>{conf_str}</td>'
                f'<td>{reason_str}</td>'
                f'<td><small style="word-break:break-word">{sources_str}</small></td>'
                f'<td><small style="word-break:break-word">'
                f'{_esc(r["gap_explanation"] or "—")}</small></td>'
                f'<td>{_fmt_ts(r["evaluated_at"])}</td>'
                f'</tr>'
            )
        parts += ["</tbody></table></div>"]

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# HTML template
# All {{ and }} are escaped Python braces (literal in output).
# Single-brace tokens ({metric_total} etc.) are Python substitution points.
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PanPilot — Panel de control</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #f1f5f9; color: #1e293b; }}

    /* Layout */
    #app {{ display: flex; height: 100vh; overflow: hidden; }}
    #sidebar {{
      width: 240px; min-width: 180px; max-width: 400px;
      background: #1a2332; flex-shrink: 0; overflow-y: auto;
      display: flex; flex-direction: column;
    }}
    #resize-handle {{
      width: 4px; cursor: col-resize; background: transparent;
      flex-shrink: 0; position: relative;
    }}
    #resize-handle::before {{
      content: ''; position: absolute; top: 0; bottom: 0;
      left: -4px; right: -4px; cursor: col-resize;
    }}
    #resize-handle:hover {{ background: #3b82f6; }}
    #main {{ flex: 1; overflow-y: auto; display: flex; flex-direction: column; min-width: 0; }}

    /* Sidebar */
    #sidebar-brand {{
      display: flex; align-items: center; gap: 0.6rem;
      padding: 1rem 1rem 0.75rem;
      border-bottom: 1px solid rgba(255,255,255,0.06);
    }}
    #sidebar-brand span {{ color: #e5e7eb; font-weight: 600; font-size: 1rem; }}
    #sidebar-nav {{ padding: 0.5rem 0; flex: 1; }}
    .nav-item {{
      display: flex; align-items: center; gap: 0.5rem;
      padding: 0.65rem 1rem; cursor: pointer; color: #9ca3af;
      border-radius: 6px; margin: 2px 8px; font-size: 0.9rem;
      transition: background 0.15s; user-select: none;
    }}
    .nav-item:hover {{ background: rgba(255,255,255,0.07); color: #e5e7eb; }}
    .nav-item.active {{ background: #1e40af; color: #fff; }}
    .nav-item:focus-visible {{ outline: 2px solid #60a5fa; outline-offset: 2px; border-radius: 4px; }}
    .nav-icon {{ font-size: 1rem; flex-shrink: 0; }}
    #sidebar-footer {{
      padding: 0.75rem 1rem;
      border-top: 1px solid rgba(255,255,255,0.06);
      color: #4b5563; font-size: 0.75rem;
    }}
    #sidebar-footer .brand-name {{ color: #6b7280; }}

    /* Header */
    #header {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 0.875rem 1.5rem;
      background: #fff; border-bottom: 1px solid #e2e8f0;
      flex-shrink: 0;
    }}
    #header h1 {{ font-size: 1.1rem; font-weight: 600; color: #1e293b; }}
    .status-active {{ color: #198754; font-weight: 600; font-size: 0.9rem; }}
    .status-test   {{ color: #ffc107; font-weight: 600; font-size: 0.9rem; }}

    /* Metric cards */
    #metrics {{
      display: flex; gap: 1rem; padding: 1rem 1.5rem;
      background: #fff; border-bottom: 1px solid #e2e8f0; flex-shrink: 0;
    }}
    .metric-card {{
      flex: 1; background: #f8fafc; border: 1px solid #e2e8f0;
      border-radius: 8px; padding: 0.875rem 1rem;
      display: flex; flex-direction: column; gap: 0.25rem; min-width: 0;
    }}
    .metric-label {{ font-size: 0.78rem; color: #64748b; font-weight: 500; }}
    .metric-value {{ font-size: 2.25rem; font-weight: 700; line-height: 1; }}
    .metric-icon {{ font-size: 1.1rem; align-self: flex-end; margin-top: -1.5rem; }}
    .metric-blue   {{ color: #2563eb; }}
    .metric-green  {{ color: #16a34a; }}
    .metric-purple {{ color: #7c3aed; }}
    .metric-orange {{ color: #ea580c; }}

    /* Sections */
    .section {{ display: none; padding: 1.5rem; flex: 1; }}
    .section.active {{ display: block; }}
    .section-title {{
      font-size: 0.85rem; font-weight: 600; text-transform: uppercase;
      letter-spacing: 0.04em; color: #64748b; margin-bottom: 1rem;
    }}

    /* Tables */
    table {{ width: 100%; border-collapse: collapse; font-size: 0.875rem; }}
    th {{
      background: #f1f5f9; text-align: left;
      padding: 0.4rem 0.6rem; border-bottom: 2px solid #e2e8f0;
      font-weight: 600; color: #475569; font-size: 0.8rem;
    }}
    td {{ padding: 0.4rem 0.6rem; border-bottom: 1px solid #e2e8f0; vertical-align: top; }}
    tr:hover td {{ background: #f8fafc; }}
    code {{
      font-size: 0.85em; background: #f1f5f9; padding: 0.1em 0.3em;
      border-radius: 3px; color: #475569;
    }}

    /* Badges */
    .badge {{
      display: inline-block; padding: 0.2em 0.55em;
      border-radius: 0.25rem; font-size: 0.75em; font-weight: 600;
    }}
    .badge-auto_respond {{ background: #198754; color: #fff; }}
    .badge-clarify      {{ background: #0d6efd; color: #fff; }}
    .badge-remind       {{ background: #fd7e14; color: #fff; }}
    .badge-alert        {{ background: #ffc107; color: #000; }}
    .badge-none         {{ background: #6c757d; color: #fff; }}
    .badge-dr-si        {{ background: #ffc107; color: #000; }}
    .badge-dr-no        {{ background: #198754; color: #fff; }}
    .badge-agotado-si   {{ background: #dc3545; color: #fff; }}
    .badge-agotado-no   {{ background: #6c757d; color: #fff; }}

    /* Filter form */
    form.filter {{
      display: flex; gap: 0.5rem; align-items: center;
      margin-bottom: 1rem; flex-wrap: wrap;
    }}
    input[type=text], select {{
      padding: 0.35rem 0.6rem; border: 1px solid #cbd5e1;
      border-radius: 4px; font-size: 0.875rem; background: #fff;
    }}
    input[type=text]:focus-visible, select:focus-visible {{
      outline: 2px solid #0d6efd; outline-offset: 2px;
    }}
    button {{
      padding: 0.35rem 0.85rem; border: 1px solid #6c757d;
      border-radius: 4px; background: #6c757d; color: #fff;
      cursor: pointer; font-size: 0.875rem;
    }}
    button:hover {{ background: #5c636a; }}
    button.btn-primary {{ background: #0d6efd; border-color: #0d6efd; color: #fff; }}
    button.btn-primary:hover {{ background: #0b5ed7; border-color: #0a58ca; }}
    button.btn-outline {{
      background: transparent; color: #0d6efd; border-color: #0d6efd;
    }}
    button.btn-outline:hover {{ background: #0d6efd; color: #fff; }}
    button:focus-visible {{ outline: 2px solid #0d6efd; outline-offset: 2px; }}
    a {{ color: #0d6efd; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    a:visited {{ color: #6610f2; }}

    /* Pagination */
    #pagination-buttons {{ display: flex; gap: 0.3rem; margin-top: 0.5rem; flex-wrap: wrap; }}
    #pagination-buttons button {{
      padding: 0.25rem 0.6rem; border: 1px solid #dee2e6;
      border-radius: 4px; background: #fff; color: #0d6efd;
      cursor: pointer; font-size: 0.8rem;
    }}
    #pagination-buttons button.page-active {{
      background: #0d6efd; color: #fff; border-color: #0d6efd;
    }}
    #pagination-buttons button:disabled {{ color: #6c757d; cursor: default; background: #fff; }}
    #pagination-info {{ font-size: 0.8rem; color: #6c757d; margin-top: 0.25rem; }}
  </style>
</head>
<body>
<div id="app">

  <!-- Sidebar -->
  <aside id="sidebar" role="navigation" aria-label="Secciones del panel">
    <div id="sidebar-brand">
      <img src="data:image/png;base64,{logo_b64}" alt="PanPilot"
           style="width:28px;height:28px;flex-shrink:0;" aria-hidden="true">
      <span>PanPilot</span>
    </div>
    <nav id="sidebar-nav">
      <div class="nav-item active" data-tab="audit"
           onclick="showTab('audit')" role="button" tabindex="0"
           onkeydown="if(event.key==='Enter'||event.key===' '){{showTab('audit');event.preventDefault();}}">
        <span class="nav-icon" aria-hidden="true">📋</span>
        Registro de auditoría
      </div>
      <div class="nav-item" data-tab="lagunas"
           onclick="showTab('lagunas')" role="button" tabindex="0"
           onkeydown="if(event.key==='Enter'||event.key===' '){{showTab('lagunas');event.preventDefault();}}">
        <span class="nav-icon" aria-hidden="true">🔍</span>
        Lagunas de documentación
      </div>
      <div class="nav-item" data-tab="dlq"
           onclick="showTab('dlq')" role="button" tabindex="0"
           onkeydown="if(event.key==='Enter'||event.key===' '){{showTab('dlq');event.preventDefault();}}">
        <span class="nav-icon" aria-hidden="true">⚠️</span>
        Cola de errores (DLQ)
      </div>
    </nav>
    <div id="sidebar-footer">
      <span class="brand-name">Proactivanet · ITSM</span>
    </div>
  </aside>

  <!-- Resize handle -->
  <div id="resize-handle" aria-hidden="true"></div>

  <!-- Main -->
  <main id="main" role="main">

    <!-- Header -->
    <header id="header">
      <h1>Panel de control</h1>
      <span class="{dry_run_class}">{dry_run_label}</span>
    </header>

    <!-- Metric cards -->
    <div id="metrics" role="region" aria-label="Métricas de hoy">
      <div class="metric-card">
        <span class="metric-label">Evaluaciones hoy</span>
        <span class="metric-value metric-blue">{metric_total}</span>
        <span class="metric-icon" aria-hidden="true">📊</span>
      </div>
      <div class="metric-card">
        <span class="metric-label">Auto-respuestas enviadas</span>
        <span class="metric-value metric-green">{metric_auto}</span>
        <span class="metric-icon" aria-hidden="true">↑</span>
      </div>
      <div class="metric-card">
        <span class="metric-label">Aclaraciones solicitadas</span>
        <span class="metric-value metric-purple">{metric_clarify}</span>
        <span class="metric-icon" aria-hidden="true">💬</span>
      </div>
      <div class="metric-card">
        <span class="metric-label">Lagunas detectadas</span>
        <span class="metric-value metric-orange">{metric_gaps}</span>
        <span class="metric-icon" aria-hidden="true">⚡</span>
      </div>
    </div>

    <!-- Registro de auditoría -->
    <div id="content-audit" class="section active" role="region" aria-label="Registro de auditoría">
      <p class="section-title">Registro de auditoría</p>
      <form class="filter" method="get" action="/admin/">
        <input type="text" name="ticket_id" placeholder="Filtrar por ticket"
               value="{ticket_id_val}" aria-label="Filtrar por ticket">
        <select name="action" aria-label="Filtrar por acción">
          <option value="">Todas las acciones</option>
          {action_options}
        </select>
        <button type="submit" class="btn-primary">Filtrar</button>
      </form>
      {audit_section}
      <div id="pagination-buttons"></div>
      <p id="pagination-info"></p>
    </div>

    <!-- Lagunas de documentación -->
    <div id="content-lagunas" class="section" role="region" aria-label="Lagunas de documentación">
      <p class="section-title">Lagunas de documentación</p>
      {rag_gaps_section}
    </div>

    <!-- Cola de errores -->
    <div id="content-dlq" class="section" role="region" aria-label="Cola de errores">
      <p class="section-title">Cola de errores (DLQ)</p>
      {dlq_section}
    </div>

  </main>
</div>

<script>
// Tab switching
function showTab(name) {{
  document.querySelectorAll('.section').forEach(function(s) {{ s.classList.remove('active'); }});
  document.querySelectorAll('.nav-item').forEach(function(n) {{ n.classList.remove('active'); }});
  document.getElementById('content-' + name).classList.add('active');
  document.querySelector('[data-tab="' + name + '"]').classList.add('active');
}}

// Sidebar resize
(function() {{
  var SIDEBAR_KEY = 'pp-sidebar-w';
  var MIN_W = 180, MAX_W = 400;
  var sidebar = document.getElementById('sidebar');
  var handle = document.getElementById('resize-handle');
  var saved = parseInt(localStorage.getItem(SIDEBAR_KEY) || '240', 10);
  sidebar.style.width = saved + 'px';
  handle.addEventListener('mousedown', function(e) {{
    e.preventDefault();
    var startX = e.clientX, startW = sidebar.offsetWidth;
    function move(e2) {{
      var w = Math.min(MAX_W, Math.max(MIN_W, startW + e2.clientX - startX));
      sidebar.style.width = w + 'px';
    }}
    function up() {{
      localStorage.setItem(SIDEBAR_KEY, sidebar.offsetWidth);
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
    }}
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  }});
}})();

// Audit pagination
(function() {{
  var PAGE_SIZE = 25;
  var AUDIT_TOTAL = {audit_total};
  var currentPage = 1;
  var tbody = document.getElementById('audit-tbody');
  if (!tbody) return;
  var auditRows = Array.from(tbody.querySelectorAll('tr'));

  function renderAuditPage(page) {{
    currentPage = page;
    var total = auditRows.length;
    if (total === 0) {{
      var pb = document.getElementById('pagination-buttons');
      var pi = document.getElementById('pagination-info');
      if (pb) pb.style.display = 'none';
      if (pi) pi.textContent = '';
      return;
    }}
    auditRows.forEach(function(r, i) {{
      r.style.display = (i >= (page - 1) * PAGE_SIZE && i < page * PAGE_SIZE) ? '' : 'none';
    }});
    var from = Math.min((page - 1) * PAGE_SIZE + 1, total);
    var to = Math.min(page * PAGE_SIZE, total);
    var info = document.getElementById('pagination-info');
    if (info) {{
      info.textContent = 'Mostrando ' + from + ' a ' + to + ' de ' + AUDIT_TOTAL + ' evaluaciones';
      if (AUDIT_TOTAL > 500) {{
        info.textContent += ' (de más recientes)';
      }}
    }}
    renderPageButtons(page, Math.ceil(total / PAGE_SIZE));
  }}

  function renderPageButtons(current, totalPages) {{
    var container = document.getElementById('pagination-buttons');
    if (!container) return;
    while (container.firstChild) {{ container.removeChild(container.firstChild); }}

    var prev = document.createElement('button');
    prev.textContent = '←';
    prev.disabled = current <= 1;
    prev.onclick = function() {{ renderAuditPage(current - 1); }};
    container.appendChild(prev);

    var start = Math.max(1, current - 2), end = Math.min(totalPages, current + 2);
    for (var p = start; p <= end; p++) {{
      var btn = document.createElement('button');
      btn.textContent = p;
      if (p === current) btn.classList.add('page-active');
      (function(pp) {{
        btn.onclick = function() {{ renderAuditPage(pp); }};
      }})(p);
      container.appendChild(btn);
    }}

    var next = document.createElement('button');
    next.textContent = '→';
    next.disabled = current >= totalPages;
    next.onclick = function() {{ renderAuditPage(current + 1); }};
    container.appendChild(next);
  }}

  renderAuditPage(1);
}})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# GET /admin/  — HTML dashboard
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse, dependencies=[Depends(_require_auth)])
def admin_dashboard(
    request: Request,
    ticket_id: str | None = None,
    action: str | None = None,
    conn: sqlite3.Connection = Depends(_conn),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    today = _date.today().isoformat()

    # Metric counts
    metrics_row = conn.execute(
        "SELECT "
        "  SUM(CASE WHEN DATE(evaluated_at)=? THEN 1 ELSE 0 END) AS total_today, "
        "  SUM(CASE WHEN DATE(evaluated_at)=? AND action='auto_respond' THEN 1 ELSE 0 END) AS auto_today, "
        "  SUM(CASE WHEN DATE(evaluated_at)=? AND action='clarify' THEN 1 ELSE 0 END) AS clarify_today "
        "FROM audit_log",
        (today, today, today),
    ).fetchone()
    gaps_today = conn.execute(
        "SELECT COUNT(*) FROM rag_misses WHERE DATE(evaluated_at)=?", (today,)
    ).fetchone()[0] or 0

    metric_total   = metrics_row["total_today"]   or 0
    metric_auto    = metrics_row["auto_today"]    or 0
    metric_clarify = metrics_row["clarify_today"] or 0
    metric_gaps    = gaps_today

    # Audit section
    audit_clauses: list[str] = []
    audit_params: list = []
    if ticket_id:
        audit_clauses.append("ticket_id = ?")
        audit_params.append(ticket_id)
    if action:
        audit_clauses.append("action = ?")
        audit_params.append(action)

    where = ("WHERE " + " AND ".join(audit_clauses)) if audit_clauses else ""
    audit_total = conn.execute(
        f"SELECT COUNT(*) FROM audit_log {where}", audit_params
    ).fetchone()[0] or 0
    audit_rows = [
        dict(r)
        for r in conn.execute(
            f"SELECT id, ticket_id, ticket_code, evaluated_at, action, reasoning, dry_run "
            f"FROM audit_log {where} ORDER BY evaluated_at DESC, id DESC LIMIT 500",
            audit_params,
        ).fetchall()
    ]

    # DLQ section
    dlq_rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, event_id, error, attempts, next_retry, exhausted "
            "FROM dlq ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    ]

    # Lagunas section
    rag_summary_rows = [
        dict(r) for r in conn.execute(
            "SELECT gap_category, COUNT(*) AS count, MAX(evaluated_at) AS latest, "
            "MAX(CASE WHEN gap_explanation != '—' THEN gap_explanation END) AS sample_explanation "
            "FROM rag_misses WHERE gap_category IS NOT NULL "
            "GROUP BY gap_category ORDER BY count DESC LIMIT 50"
        ).fetchall()
    ]
    rag_recent_rows = [
        dict(r) for r in conn.execute(
            "SELECT id, ticket_id, ticket_code, question_summary, confidence, none_reason, "
            "chunk_sources, gap_category, gap_explanation, evaluated_at "
            "FROM rag_misses ORDER BY evaluated_at DESC LIMIT 100"
        ).fetchall()
    ]

    # DRY_RUN header indicator
    dry_run_label = "● Modo prueba" if settings.dry_run else "● Activo"
    dry_run_class = "status-test" if settings.dry_run else "status-active"

    # Filter form
    action_options = "\n".join(
        f'<option value="{a}"{"selected" if a == action else ""}>{a}</option>'
        for a in _ACTIONS
    )
    filtered = bool(ticket_id or action)

    html = _HTML_TEMPLATE.format(
        logo_b64=_LOGO_B64,
        dry_run_label=dry_run_label,
        dry_run_class=dry_run_class,
        metric_total=metric_total,
        metric_auto=metric_auto,
        metric_clarify=metric_clarify,
        metric_gaps=metric_gaps,
        audit_total=audit_total,
        ticket_id_val=_esc(ticket_id or ""),
        action_options=action_options,
        audit_section=_render_audit(audit_rows, settings.proactivanet_base_url, filtered=filtered),
        rag_gaps_section=_render_rag_gaps(rag_summary_rows, rag_recent_rows, settings.proactivanet_base_url),
        dlq_section=_render_dlq(dlq_rows),
    )
    return HTMLResponse(content=html)
