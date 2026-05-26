#!/usr/bin/env python3
"""
RAG smoke test — exercises Pass 1 (evaluate_ticket) + Pass 2 (rag_evaluate)
end-to-end against real pandocs and a real Anthropic call.

Usage:
    uv run scripts/rag_smoke_test.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic

from panpilot.config import get_settings
from panpilot.intelligence.engine import evaluate_ticket
from panpilot.intelligence.models import TicketContext
from panpilot.intelligence.rag import RagDeps, _load_model, rag_evaluate

PANDOCS_DIR = Path.home() / "pandocs"
CHROMA_DIR  = Path("data/chroma")

TICKETS = [
    TicketContext(
        ticket_id="de56367e-c783-4f7a-beb5-fa9d985d13d6",
        title="TEST RAG: ¿Cómo crear campos personalizados en Proactivanet?",
        description=(
            "Necesito saber cómo puedo crear campos personalizados en Proactivanet "
            "para añadir información adicional a los tickets. "
            "¿Cuáles son los pasos a seguir para configurarlos?"
        ),
        status="Assigned",
        priority="P2",
        created_at="2026-05-26T10:00:00Z",
        last_modified="2026-05-26T10:00:00Z",
        awaiting_client_reply=False,
        ticket_code="INC 2026-000007",
    ),
    TicketContext(
        ticket_id="b5f72578-59a9-4e41-b826-898dbf575ad5",
        title="TEST RAG: ¿Cuánto cuesta la licencia anual de Proactivanet?",
        description=(
            "Necesito información sobre el coste de la licencia anual de Proactivanet "
            "para 500 usuarios. ¿Cuál es el precio actual y qué descuentos existen para renovaciones?"
        ),
        status="Assigned",
        priority="P2",
        created_at="2026-05-26T10:05:00Z",
        last_modified="2026-05-26T10:05:00Z",
        awaiting_client_reply=False,
        ticket_code="INC 2026-000008",
    ),
]


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    schema = (Path(__file__).parent.parent / "panpilot" / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    return conn


def hr(ch: str = "─", width: int = 72) -> None:
    print(ch * width)


def section(title: str) -> None:
    hr("═")
    print(f"  {title}")
    hr("═")


def main() -> None:
    settings = get_settings()
    settings.confidence_threshold = 0.70  # use a slightly relaxed threshold for smoke test visibility

    print("\n🔍  RAG Smoke Test — PanPilot Phase 2")
    hr()

    # ── Load RAG deps ─────────────────────────────────────────────────────────
    print(f"\n[1/4]  Loading sentence-transformers model …")
    model = _load_model()
    print(f"       Model loaded: all-MiniLM-L6-v2")

    print(f"\n[2/4]  Connecting ChromaDB collection …")
    import chromadb
    chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = chroma_client.get_collection("pandocs")
    chunk_count = collection.count()
    print(f"       Collection 'pandocs': {chunk_count} chunks indexed")

    if chunk_count == 0:
        print("\n  ERROR: Collection is empty. Run:")
        print("  uv run scripts/index_pandocs.py --pandocs ~/pandocs --chroma data/chroma")
        sys.exit(1)

    rag_deps = RagDeps(model=model, collection=collection)
    print(f"       RagDeps.available = {rag_deps.available}")

    # ── Anthropic client ──────────────────────────────────────────────────────
    print(f"\n[3/4]  Anthropic client ready")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    conn = _make_conn()

    # ── Evaluate each ticket ──────────────────────────────────────────────────
    print(f"\n[4/4]  Running Pass 1 + Pass 2 evaluation for {len(TICKETS)} tickets …")

    results = []
    for i, ctx in enumerate(TICKETS, 1):
        section(f"Ticket {i}/{len(TICKETS)}: {ctx.ticket_code}  —  {ctx.title}")

        print(f"\n  Title      : {ctx.title}")
        print(f"  Description: {ctx.description[:120]}…")
        print()

        # Pass 1: evaluate_ticket
        print("  ── Pass 1 (evaluate_ticket) ──")
        p1 = evaluate_ticket(ctx, settings, client=client)
        print(f"     action   : {p1.action}")
        print(f"     reasoning: {p1.reasoning[:100]}…" if p1.reasoning else "     reasoning: (none)")

        if p1.action != "auto_respond":
            print(f"\n  ℹ  Pass 1 did not return auto_respond — RAG Pass 2 skipped")
            print(f"     (action={p1.action}, none_reason={p1.none_reason})")
            results.append({"ticket": ctx.ticket_code, "p1": p1, "p2": None})
            continue

        # Pass 2: rag_evaluate
        print()
        print("  ── Pass 2 (rag_evaluate) ──")
        p2 = rag_evaluate(ctx, rag_deps, settings, conn, client)
        print(f"     action     : {p2.action}")
        print(f"     none_reason: {p2.none_reason}")
        print(f"     confidence : {p2.confidence}")
        threshold_label = "✓ ABOVE threshold" if (p2.confidence or 0) >= settings.confidence_threshold else "✗ below threshold"
        print(f"     threshold  : {settings.confidence_threshold:.2f}  →  {threshold_label}")
        print()
        print("  ── response_draft ──")
        if p2.response_draft:
            for line in p2.response_draft.splitlines():
                print(f"     {line}")
        else:
            print("     (no draft)")
        print()
        print("  ── reasoning ──")
        if p2.reasoning:
            for line in p2.reasoning.splitlines():
                print(f"     {line}")

        results.append({"ticket": ctx.ticket_code, "p1": p1, "p2": p2})

    # ── Summary ───────────────────────────────────────────────────────────────
    section("SUMMARY")
    print()
    for r in results:
        ticket = r["ticket"]
        p1 = r["p1"]
        p2 = r["p2"]
        if p2 is None:
            print(f"  {ticket}  →  P1={p1.action}/{p1.none_reason}  (no RAG)")
        else:
            conf_str = f"conf={p2.confidence:.2f}" if p2.confidence is not None else "conf=None"
            print(f"  {ticket}  →  P1={p1.action}  P2={p2.action}/{p2.none_reason or '—'}  {conf_str}")

    # ── Audit log (in-memory) ─────────────────────────────────────────────────
    rag_misses = conn.execute("SELECT * FROM rag_misses").fetchall()
    print()
    print(f"  rag_misses table: {len(rag_misses)} row(s)")
    for row in rag_misses:
        print(f"    ticket_id={row['ticket_id']}  summary={row['question_summary']}")

    print()
    hr()
    print("  Smoke test complete.")
    hr()
    print()


if __name__ == "__main__":
    main()
