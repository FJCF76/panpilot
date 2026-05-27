"""
T5 — Admin interface: audit log read side.
T19 — DLQ retry trigger.

All routes require HTTP Basic Auth. The admin interface is read-only with
the single exception of the DLQ retry action (T19), which requeues an
exhausted event by resetting its DLQ row and clearing events.processed.

The admin interface never reads, writes, or displays environment variables.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path

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
# GET /admin/  — HTML dashboard
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PanPilot — Panel de administración</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background: #f8f9fa; margin: 0; padding: 0; }}
    .container {{ max-width: 1200px; margin: 0 auto; padding: 1.5rem 1rem; }}
    h1 {{ font-size: 1.75rem; margin-bottom: 1.5rem; }}
    h2 {{ font-size: 1.4rem; margin-top: 2rem; margin-bottom: 0.75rem; border-bottom: 1px solid #dee2e6; padding-bottom: 0.3rem; }}
    .text-muted {{ color: #6c757d; font-size: 0.9em; }}
    .text-success {{ color: #198754; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.875rem; }}
    th {{ background: #e9ecef; text-align: left; padding: 0.4rem 0.6rem; border-bottom: 2px solid #dee2e6; }}
    td {{ padding: 0.4rem 0.6rem; border-bottom: 1px solid #dee2e6; vertical-align: top; }}
    tr:hover td {{ background: #f1f3f5; }}
    code {{ font-size: 0.85em; background: #f1f3f5; padding: 0.1em 0.3em; border-radius: 3px; }}
    .badge {{ display: inline-block; padding: 0.2em 0.5em; border-radius: 0.25rem; font-size: 0.75em; font-weight: 600; }}
    .badge-danger {{ background: #dc3545; color: #fff; }}
    .badge-warning {{ background: #ffc107; color: #000; }}
    .badge-secondary {{ background: #6c757d; color: #fff; }}
    form.filter {{ display: flex; gap: 0.5rem; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; }}
    input[type=text], select {{ padding: 0.3rem 0.5rem; border: 1px solid #ced4da; border-radius: 4px; font-size: 0.875rem; }}
    button {{ padding: 0.3rem 0.8rem; border: 1px solid #6c757d; border-radius: 4px; background: #6c757d; color: #fff; cursor: pointer; font-size: 0.875rem; }}
    button:hover {{ background: #5c636a; }}
    .btn-outline {{ background: transparent; color: #0d6efd; border-color: #0d6efd; }}
    .btn-outline:hover {{ background: #0d6efd; color: #fff; }}
    a {{ color: #0d6efd; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    small {{ font-size: 0.8em; color: #555; }}
    td small, td code {{ font-size: inherit; }}
    button:focus-visible, input[type=text]:focus-visible, select:focus-visible {{ outline: 2px solid #0d6efd; outline-offset: 2px; }}
  </style>
</head>
<body>
<div class="container">

  <h1>PanPilot — Panel de administración</h1>

  <!-- DLQ section -->
  <h2>Cola de errores (DLQ)</h2>
  {dlq_section}

  <!-- Lagunas de documentación section -->
  <h2>Lagunas de documentación</h2>
  {rag_gaps_section}

  <!-- Audit section -->
  <h2>Registro de auditoría <span class="text-muted">(últimas {audit_limit} entradas)</span></h2>
  <form class="filter" method="get" action="/admin/">
    <input type="text" name="ticket_id" placeholder="Filtrar por ticket" value="{ticket_id_val}">
    <select name="action">
      <option value="">Todas las acciones</option>
      {action_options}
    </select>
    <button type="submit">Filtrar</button>
  </form>
  {audit_section}

</div>
</body>
</html>
"""

_ACTIONS = ["clarify", "auto_respond", "remind", "alert", "none"]


def _render_dlq(rows: list[dict]) -> str:
    if not rows:
        return '<p class="text-success">No hay entradas en la cola de errores.</p>'

    parts = [
        '<div style="overflow-x:auto">',
        "<table>",
        "<thead><tr>"
        "<th>ID</th><th>Evento</th><th>Intentos</th>"
        "<th>Próximo reintento</th><th>Agotado</th><th>Error</th><th>Acción</th>"
        "</tr></thead><tbody>",
    ]
    for r in rows:
        badge = '<span class="badge badge-danger">Sí</span>' if r["exhausted"] else '<span class="badge badge-warning">Pendiente</span>'
        retry_btn = (
            f'<form method="post" action="/admin/dlq/{r["id"]}/retry" style="display:inline">'
            '<button class="btn-outline">Reintentar</button></form>'
        )
        parts.append(
            f'<tr><td>{r["id"]}</td><td><code>{r["event_id"]}</code></td>'
            f'<td>{r["attempts"]}</td><td>{r["next_retry"] or "—"}</td>'
            f'<td>{badge}</td>'
            f'<td><small>{_esc(str(r["error"])[:120])}</small></td>'
            f'<td>{retry_btn}</td></tr>'
        )
    parts += ["</tbody></table></div>"]
    return "\n".join(parts)


def _render_audit(rows: list[dict], base_url: str) -> str:
    if not rows:
        return '<p class="text-muted">Sin entradas.</p>'

    parts = [
        '<div style="overflow-x:auto">',
        "<table>",
        "<thead><tr>"
        "<th>ID</th><th>Ticket</th><th>Evaluado</th><th>Acción</th>"
        "<th>Dry run</th><th>Razonamiento</th>"
        "</tr></thead><tbody>",
    ]
    for r in rows:
        dr = '<span class="badge badge-secondary">Sí</span>' if r["dry_run"] else ""
        label = _esc(r["ticket_code"] or r["ticket_id"])
        ticket_link = (
            f'<a href="{_esc(base_url)}/servicedesk/incidents/formIncidents/formIncidents.paw'
            f'?id={_esc(r["ticket_id"])}" target="_blank" rel="noopener">{label}</a>'
        )
        parts.append(
            f'<tr><td>{r["id"]}</td>'
            f'<td>{ticket_link}</td>'
            f'<td>{r["evaluated_at"]}</td>'
            f'<td><code>{r["action"]}</code></td>'
            f'<td>{dr}</td>'
            f'<td><small style="white-space:pre-wrap;word-break:break-word">'
            f'{_esc(str(r["reasoning"] or ""))}</small></td></tr>'
        )
    parts += ["</tbody></table></div>"]
    return "\n".join(parts)


def _render_rag_gaps(summary_rows: list[dict], recent_rows: list[dict], base_url: str) -> str:
    parts: list[str] = []

    parts.append(
        '<p><strong>Lagunas por categoría</strong>'
        ' <span class="text-muted">(agrupadas)</span></p>'
    )
    if not summary_rows:
        msg = (
            'No hay lagunas de documentación registradas aún.'
            if not recent_rows
            else 'Sin lagunas categorizadas todavía.'
        )
        parts.append(f'<p class="text-muted">{msg}</p>')
    else:
        parts += [
            '<div style="overflow-x:auto">',
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
                f'<td>{r["latest"] or "—"}</td>'
                f'<td><small style="word-break:break-word">'
                f'{_esc(r["sample_explanation"] or "—")}'
                f'</small></td>'
                f'</tr>'
            )
        parts += ["</tbody></table></div>"]

    parts.append(
        '<p><strong>Consultas sin respuesta automática</strong>'
        ' <span class="text-muted">(últimas 100)</span></p>'
    )
    if not recent_rows:
        parts.append('<p class="text-muted">Sin entradas.</p>')
    else:
        parts += [
            '<div style="overflow-x:auto">',
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
                f'<td>{r["evaluated_at"] or "—"}</td>'
                f'</tr>'
            )
        parts += ["</tbody></table></div>"]

    return "\n".join(parts)


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
    )


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(_require_auth)])
def admin_dashboard(
    request: Request,
    ticket_id: str | None = None,
    action: str | None = None,
    conn: sqlite3.Connection = Depends(_conn),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    audit_limit = 100

    audit_clauses: list[str] = []
    audit_params: list = []
    if ticket_id:
        audit_clauses.append("ticket_id = ?")
        audit_params.append(ticket_id)
    if action:
        audit_clauses.append("action = ?")
        audit_params.append(action)

    where = ("WHERE " + " AND ".join(audit_clauses)) if audit_clauses else ""
    audit_rows = [
        dict(r)
        for r in conn.execute(
            f"SELECT id, ticket_id, ticket_code, evaluated_at, action, reasoning, dry_run "
            f"FROM audit_log {where} ORDER BY evaluated_at DESC LIMIT ?",
            audit_params + [audit_limit],
        ).fetchall()
    ]

    dlq_rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, event_id, error, attempts, next_retry, exhausted "
            "FROM dlq ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    ]

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

    action_options = "\n".join(
        f'<option value="{a}"{"selected" if a == action else ""}>{a}</option>'
        for a in _ACTIONS
    )

    html = _HTML_TEMPLATE.format(
        dlq_section=_render_dlq(dlq_rows),
        rag_gaps_section=_render_rag_gaps(
            rag_summary_rows, rag_recent_rows, settings.proactivanet_base_url
        ),
        audit_section=_render_audit(audit_rows, settings.proactivanet_base_url),
        audit_limit=audit_limit,
        ticket_id_val=_esc(ticket_id or ""),
        action_options=action_options,
    )
    return HTMLResponse(content=html)
