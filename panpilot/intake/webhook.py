from __future__ import annotations

import asyncio
import logging
import logging.config
from contextlib import asynccontextmanager
from typing import AsyncIterator

import anthropic
from fastapi import FastAPI

from panpilot.admin.router import router as admin_router
from panpilot.intake.router import router as intake_router
from panpilot.config import get_settings
from panpilot.db.connection import get_connection, init_db, main_db_path, reset_stale_pending
from panpilot.intake.catchup import get_last_received_at, run_startup_catchup
from panpilot.intake.reference_data import load_reference_data
from panpilot.intake.scheduler import build_scheduler
from panpilot.intelligence.rag import RagDeps, _load_model
from panpilot.worker.dlq import DLQThread
from panpilot.worker.runner import WorkerThread, process_event

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Step 0: logging — must come first so all startup messages are captured
    _configure_logging()

    settings = get_settings()

    # Step 0b (T2): initialize SQLite schema — idempotent, safe on every restart
    init_db(settings)

    # Step 0c (Bug 1 fix): reset any PENDING_EVALUATION rows left by a prior crash.
    # Must run before Step 2 (scheduler) and Step 3 (DLQ thread) so no thread
    # can claim a ticket between the reset and the first worker poll.
    _startup_conn = get_connection(main_db_path(settings))
    try:
        reset_stale_pending(_startup_conn)
    finally:
        _startup_conn.close()

    # Step 0d: single Anthropic client shared across all evaluations and audit writes.
    # Created once at startup so connection pooling is reused across tickets.
    anthropic_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    # Step 0e (RAG): load sentence-transformers model and connect ChromaDB collection.
    # Both are optional — if pandocs_dir is not configured, RAG is disabled (degraded mode).
    rag_deps: RagDeps = RagDeps(model=None, collection=None)
    if settings.pandocs_dir is not None:
        try:
            import chromadb  # noqa: PLC0415
            rag_model = _load_model()
            chroma_client = chromadb.PersistentClient(path=str(settings.chroma_dir))
            try:
                rag_collection = chroma_client.get_collection("pandocs")
                if rag_collection.count() == 0:
                    logger.warning(
                        "RAG: pandocs_dir is set but the 'pandocs' collection is empty — "
                        "run 'uv run scripts/index_pandocs.py' to index documentation."
                    )
                rag_deps = RagDeps(model=rag_model, collection=rag_collection)
                logger.info("RAG: loaded model and collection (%d chunks)", rag_collection.count())
            except Exception:
                logger.warning(
                    "RAG: 'pandocs' collection not found — "
                    "run 'uv run scripts/index_pandocs.py' to index documentation."
                )
        except Exception:
            logger.exception("RAG: failed to initialize — running in degraded mode (no RAG)")

    # Step 1 (T18): load reference data — raises on failure, won't start blind
    priority_map, status_map, action_type_map, terminal_status_names = await load_reference_data(settings)
    app.state.priority_map = priority_map
    app.state.status_map = status_map
    app.state.action_type_map = action_type_map
    app.state.terminal_status_names = terminal_status_names

    # Step 1b (Bug 2 fix): snapshot watermark BEFORE yield so a webhook arriving
    # during startup can't advance the timestamp before catchup reads it.
    # The catchup itself runs as a background task after yield (below).
    catchup_conn = get_connection(main_db_path(settings))
    catchup_since = get_last_received_at(catchup_conn)

    # Step 2 (T6): start APScheduler stale detector
    scheduler = build_scheduler(settings, action_type_map)
    scheduler.start()

    # Step 3 (T4): start DLQ background thread
    dlq_conn = get_connection(main_db_path(settings))

    def _dlq_process_fn(event: dict) -> None:
        process_event(
            event, settings, dlq_conn,
            priority_map, status_map, action_type_map,
            terminal_status_names=terminal_status_names,
            anthropic_client=anthropic_client,
            rag_deps=rag_deps,
        )

    dlq_thread = DLQThread(dlq_conn, _dlq_process_fn)
    dlq_thread.start()

    # Step 4 (worker): start DB-backed worker polling thread
    worker_conn = get_connection(main_db_path(settings))
    worker_thread = WorkerThread(
        worker_conn, settings, priority_map, status_map, action_type_map,
        terminal_status_names=terminal_status_names,
        anthropic_client=anthropic_client,
        rag_deps=rag_deps,
    )
    worker_thread.start()

    logger.info(
        "PanPilot started (dry_run=%s, priorities=%d, statuses=%d, action_types=%d)",
        settings.dry_run,
        len(priority_map),
        len(status_map),
        len(action_type_map),
    )

    # Step 5 (T10 / Bug 2 fix): run catch-up as a background task so the server
    # accepts webhook deliveries immediately. Uses the watermark snapshotted above.
    catchup_task: asyncio.Task[int] = asyncio.create_task(
        asyncio.to_thread(
            run_startup_catchup, settings, catchup_conn,
            since=catchup_since,
            terminal_status_names=terminal_status_names,
        ),
        name="startup-catchup",
    )

    yield

    # Shutdown in reverse order
    # Step 5: wait for background catchup to finish (or time out)
    catchup_task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(catchup_task), timeout=30.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    catchup_conn.close()
    # Step 4: signal worker thread to stop
    worker_thread.stop()
    worker_conn.close()
    # Step 3: signal DLQ thread to stop
    dlq_thread.stop()
    dlq_conn.close()
    # Step 2: shut down APScheduler
    scheduler.shutdown(wait=False)
    # Step 1: priority_map / status_map are in-memory; no cleanup needed
    # Step 0d: close the shared Anthropic client
    anthropic_client.close()

    logger.info("PanPilot shut down")


app = FastAPI(title="PanPilot", lifespan=lifespan)

app.include_router(admin_router)
app.include_router(intake_router)
