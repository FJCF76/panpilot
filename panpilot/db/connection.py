from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from panpilot.config import Settings

logger = logging.getLogger(__name__)


def get_connection(db_path: Path) -> sqlite3.Connection:
    """
    Open a SQLite connection with WAL mode and synchronous=NORMAL applied.

    Both pragmas are set on every connection — not just at schema creation time —
    so no module can accidentally open a connection without them.
    WAL allows concurrent readers without blocking writers.
    synchronous=NORMAL is safe with WAL and avoids fsync on every write.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def main_db_path(settings: Settings) -> Path:
    return settings.data_dir / "panpilot.db"


def init_db(settings: Settings) -> None:
    """
    Create the data directory and initialize all tables if they don't exist.
    Safe to call on every startup — all CREATE statements use IF NOT EXISTS.
    Column additions are applied via ALTER TABLE when missing (forward migrations).
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    schema_sql = (Path(__file__).parent / "schema.sql").read_text()
    conn = get_connection(main_db_path(settings))
    try:
        conn.executescript(schema_sql)
        _migrate(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply forward-only column additions to existing tables."""
    _add_column_if_missing(conn, "audit_log", "ticket_code", "TEXT")
    _add_column_if_missing(conn, "ticket_state", "requester_id", "TEXT")


def reset_stale_pending(conn: sqlite3.Connection) -> int:
    """
    Reset any PENDING_EVALUATION rows in ticket_state back to WAITING.

    Call this at startup before any worker or DLQ threads start.  In a
    single-process deployment (uvicorn --workers 1) any PENDING_EVALUATION
    row at startup is stale — it was left behind by a crash or SIGKILL
    mid-evaluation.  Resetting it allows the next event for that ticket to
    be claimed normally instead of looping forever on TicketBusy.

    Returns the number of rows reset (0 on a clean startup).
    """
    cursor = conn.execute(
        "UPDATE ticket_state "
        "SET state = 'WAITING', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
        "WHERE state = 'PENDING_EVALUATION'"
    )
    conn.commit()
    count = cursor.rowcount
    if count:
        logger.warning(
            "Startup: reset %d stale PENDING_EVALUATION ticket(s) to WAITING", count
        )
    else:
        logger.debug("Startup: no stale PENDING_EVALUATION rows found")
    return count


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, col_type: str
) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
