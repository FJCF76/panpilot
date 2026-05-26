"""
RAG (Retrieval-Augmented Generation) engine for Pass 2 auto-respond evaluation.

Retrieves the most relevant documentation chunks for a ticket from a ChromaDB
collection, then calls Claude with the retrieved context to produce a
customer-facing answer with a confidence score.

Architecture:
  - Embeddings: local sentence-transformers/all-MiniLM-L6-v2 (384-dim, CPU)
  - Vector store: ChromaDB PersistentClient, collection "pandocs"
  - Indexing: on-demand via scripts/index_pandocs.py (not at startup)
  - Query: always uses query_embeddings= (never query_texts= which would trigger
    ChromaDB's own embedded model)

When the collection is unavailable or encode/query errors occur, rag_evaluate()
returns Decision(none, no_doc_coverage) so the ticket is escalated to NEEDS_HUMAN
rather than silently dropped to the DLQ.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

import anthropic

from panpilot.config import Settings
from panpilot.intelligence.models import Decision, TicketContext
from panpilot.intelligence.prompts import (
    GAP_ANALYSIS_TOOL,
    RAG_DECISION_TOOL,
    build_gap_analysis_message,
    build_rag_user_message,
)

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
_GAP_ANALYSIS_SYSTEM = (
    "Eres un analista de documentación técnica para Proactivanet S.A.\n"
    "Tu tarea es identificar lagunas en la documentación de producto basándote en preguntas\n"
    "de soporte que el sistema no pudo responder automáticamente.\n"
    "Responde siempre en español. Sé conciso y específico."
)
RAG_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
_CHUNK_HEADER_PATTERN = re.compile(r"(?m)^## ")
_CHUNK_MAX_CHARS = 1200


@dataclass
class RagDeps:
    model: Any        # SentenceTransformer | None
    collection: Any   # chromadb.Collection | None

    @property
    def available(self) -> bool:
        return (
            self.model is not None
            and self.collection is not None
            and self.collection.count() > 0
        )


def _load_model(name: str = RAG_EMBEDDING_MODEL) -> Any:
    """Lazy-import SentenceTransformer to avoid pulling torch into the test suite."""
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415
    return SentenceTransformer(name)


def chunk_document(content: str, title: str) -> list[str]:
    """
    Split a markdown document body into chunks suitable for embedding.

    Strategy:
      1. Split on '## ' section headers (semantic boundaries).
      2. Any chunk exceeding _CHUNK_MAX_CHARS is further split on blank lines
         (paragraph fallback) to avoid silent truncation at the model's 256-token
         window.
      3. Every chunk is prefixed with the document title for retrieval context.
    """
    # Remove YAML frontmatter (--- ... ---) if present
    body = re.sub(r"(?s)^---.*?---\s*", "", content).strip()
    if not body:
        return [f"Título: {title}\n\n"]

    # Split on '## ' headers; keep the header text as part of each chunk
    raw_sections = _CHUNK_HEADER_PATTERN.split(body)
    chunks: list[str] = []
    for section in raw_sections:
        section = section.strip()
        if not section:
            continue
        text = f"Título: {title}\n\n{section}"
        if len(text) <= _CHUNK_MAX_CHARS:
            chunks.append(text)
        else:
            # Paragraph fallback: split on blank lines.
            # Exception: never split immediately after a ":" line — that would
            # detach a list-intro sentence from its bullet items, gutting the
            # semantic signal of the list chunk during retrieval.
            paragraphs = re.split(r"\n\n+", section)
            header = f"Título: {title}\n\n"
            current = header
            for para in paragraphs:
                candidate = current + para + "\n\n"
                intro_colon = current.rstrip().endswith(":")
                over_limit = len(candidate) > _CHUNK_MAX_CHARS
                has_content = len(current) > len(header)
                if over_limit and has_content and not intro_colon:
                    chunks.append(current.strip())
                    current = header + para + "\n\n"
                else:
                    current = candidate
            if current.strip() != header.strip():
                chunks.append(current.strip())
    return chunks if chunks else [f"Título: {title}\n\n"]


def retrieve_relevant_chunks(
    ctx: TicketContext,
    collection: Any,
    model: Any,
    k: int = 5,
) -> list[dict]:
    """
    Embed the ticket query and return the top-k most similar chunks.

    Always uses query_embeddings= (never query_texts=) so ChromaDB does not
    attempt to load its own default embedding model.

    Returns an empty list if the collection is empty.
    """
    if collection.count() == 0:
        return []

    query_text = f"{ctx.title}\n{ctx.description}"
    embedding = model.encode(query_text).tolist()
    results = collection.query(
        query_embeddings=[embedding],
        n_results=min(k, collection.count()),
        include=["documents", "metadatas", "distances"],
    )
    chunks = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        chunks.append({"document": doc, "metadata": meta, "distance": dist})
    return chunks


def _parse_rag_decision(response: anthropic.types.Message) -> Decision:
    """Extract Decision from a Claude response that called record_rag_decision."""
    for block in response.content:
        if block.type == "tool_use" and block.name == "record_rag_decision":
            inp = block.input
            confidence = inp.get("confidence")
            if confidence is not None:
                confidence = max(0.0, min(1.0, float(confidence)))
            return Decision(
                action="auto_respond",
                reasoning=inp.get("reasoning", ""),
                response_draft=inp.get("response_draft", ""),
                confidence=confidence,
            )
    # Claude did not call the tool — treat as no coverage
    logger.warning("RAG: Claude did not call record_rag_decision — treating as no_doc_coverage")
    return Decision(action="none", reasoning="No RAG decision tool call in response.", none_reason="no_doc_coverage")


def evaluate_with_context(
    ctx: TicketContext,
    chunks: list[dict],
    settings: Settings,
    client: anthropic.Anthropic,
) -> Decision:
    """Call Claude with the ticket and retrieved chunks; return a Decision with confidence."""
    user_message = build_rag_user_message(ctx, chunks)
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        tools=[RAG_DECISION_TOOL],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": user_message}],
    )
    return _parse_rag_decision(response)


def _generate_gap_explanation(
    ctx: TicketContext,
    chunks: list[dict],
    none_reason: str,
    confidence: float | None,
    settings: Settings,
    client: anthropic.Anthropic,
) -> tuple[str, str]:
    """
    Call Claude to classify the documentation gap and generate a Spanish explanation.

    Returns (gap_category, gap_explanation). On any exception returns fallback values
    so that the miss row is still written — explanation data is best-effort.
    """
    try:
        user_message = build_gap_analysis_message(ctx, chunks, none_reason, confidence, settings)
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=_GAP_ANALYSIS_SYSTEM,
            tools=[GAP_ANALYSIS_TOOL],
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": user_message}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_gap_analysis":
                inp = block.input
                return (
                    inp.get("gap_category", "Sin categorizar") or "Sin categorizar",
                    inp.get("gap_explanation", "—") or "—",
                )
        logger.warning(
            "Gap analysis: Claude did not call record_gap_analysis for ticket=%s",
            ctx.ticket_id,
        )
        return ("Sin categorizar", "—")
    except Exception:
        logger.warning(
            "Gap analysis: exception for ticket=%s", ctx.ticket_id, exc_info=True
        )
        return ("Sin categorizar", "—")


def _write_rag_miss(
    conn: sqlite3.Connection,
    ticket_id: str,
    ticket_code: str | None,
    question_summary: str,
    *,
    confidence: float | None,
    none_reason: str,
    chunk_sources: list[dict],
    gap_category: str,
    gap_explanation: str,
) -> None:
    """Record a documentation gap for admin review."""
    conn.execute(
        "INSERT INTO rag_misses "
        "(ticket_id, ticket_code, question_summary, confidence, none_reason, chunk_sources, gap_category, gap_explanation) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ticket_id,
            ticket_code,
            question_summary[:200],
            confidence,
            none_reason,
            json.dumps(chunk_sources, ensure_ascii=False),
            gap_category,
            gap_explanation,
        ),
    )
    conn.commit()


def rag_evaluate(
    ctx: TicketContext,
    rag_deps: RagDeps,
    settings: Settings,
    conn: sqlite3.Connection,
    client: anthropic.Anthropic,
) -> Decision:
    """
    Pass 2 RAG evaluation for tickets that reached auto_respond in Pass 1.

    Flow:
      1. Retrieve top-k chunks for the ticket.
      2. If no chunks: return Decision(none, no_doc_coverage).
      3. Call Claude with retrieved chunks → Decision with confidence.
      4. If confidence < settings.confidence_threshold: write rag_miss, return low_confidence.
      5. Else: return the auto_respond Decision.

    Wraps encode and query errors in try/except — failures return no_doc_coverage
    rather than propagating to the DLQ.
    """
    try:
        chunks = retrieve_relevant_chunks(
            ctx, rag_deps.collection, rag_deps.model, k=settings.rag_top_k
        )
    except Exception:
        logger.exception("RAG: retrieve_relevant_chunks failed for ticket=%s", ctx.ticket_id)
        return Decision(
            action="none",
            reasoning="Error al recuperar documentación relevante.",
            none_reason="no_doc_coverage",
        )

    if not chunks:
        gap_category, gap_explanation = _generate_gap_explanation(
            ctx, [], "no_doc_coverage", None, settings, client
        )
        _write_rag_miss(
            conn, ctx.ticket_id, ctx.ticket_code, ctx.title,
            confidence=None,
            none_reason="no_doc_coverage",
            chunk_sources=[],
            gap_category=gap_category,
            gap_explanation=gap_explanation,
        )
        return Decision(
            action="none",
            reasoning="No se encontró documentación relevante para responder esta consulta.",
            none_reason="no_doc_coverage",
        )

    decision = evaluate_with_context(ctx, chunks, settings, client)

    confidence = decision.confidence if decision.confidence is not None else 0.0
    if confidence < settings.confidence_threshold:
        logger.info(
            "RAG: low confidence (%.2f < %.2f) for ticket=%s — writing rag_miss",
            confidence,
            settings.confidence_threshold,
            ctx.ticket_id,
        )
        chunk_sources = [
            {
                "title": c["metadata"].get("title", ""),
                "filename": c["metadata"].get("filename", ""),
            }
            for c in chunks
        ]
        gap_category, gap_explanation = _generate_gap_explanation(
            ctx, chunks, "low_confidence", confidence, settings, client
        )
        _write_rag_miss(
            conn, ctx.ticket_id, ctx.ticket_code, ctx.title,
            confidence=confidence,
            none_reason="low_confidence",
            chunk_sources=chunk_sources,
            gap_category=gap_category,
            gap_explanation=gap_explanation,
        )
        return Decision(
            action="none",
            reasoning=(
                f"Confianza insuficiente ({confidence:.0%}) para generar una respuesta automática."
            ),
            none_reason="low_confidence",
        )

    return decision
