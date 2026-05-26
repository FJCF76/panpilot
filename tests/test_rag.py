"""Tests for the RAG engine (panpilot/intelligence/rag.py)."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from panpilot.config import get_settings
from panpilot.intelligence.models import Decision, TicketContext
from panpilot.intelligence.rag import (
    RagDeps,
    _load_model,
    _parse_rag_decision,
    _write_rag_miss,
    chunk_document,
    evaluate_with_context,
    rag_evaluate,
    retrieve_relevant_chunks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    schema = (Path(__file__).parent.parent / "panpilot" / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    return conn


def _ctx(**kwargs) -> TicketContext:
    defaults = dict(
        ticket_id="TKT-1",
        title="How to reset password",
        description="User forgot their password.",
        status="Assigned",
        priority="P2",
        created_at="2026-05-25T08:00:00Z",
        last_modified="2026-05-25T09:00:00Z",
        awaiting_client_reply=False,
    )
    return TicketContext(**{**defaults, **kwargs})


def _mock_claude_response(
    response_draft: str = "Respuesta de prueba.",
    confidence: float = 0.9,
    reasoning: str = "Bien cubierto.",
) -> MagicMock:
    """Build a mock Anthropic Message with a record_rag_decision tool_use block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "record_rag_decision"
    block.input = {
        "response_draft": response_draft,
        "confidence": confidence,
        "reasoning": reasoning,
    }
    response = MagicMock()
    response.content = [block]
    return response


def _mock_rag_deps(count: int = 5) -> RagDeps:
    model = MagicMock()
    model.encode.return_value = MagicMock()
    model.encode.return_value.tolist.return_value = [0.1] * 384

    collection = MagicMock()
    collection.count.return_value = count
    collection.query.return_value = {
        "documents": [["Título: Doc 1\n\nContenido de prueba."] * min(count, 5)],
        "metadatas": [[{"title": "Doc 1", "filename": "doc1.md"}] * min(count, 5)],
        "distances": [[0.1] * min(count, 5)],
    }
    return RagDeps(model=model, collection=collection)


# ---------------------------------------------------------------------------
# chunk_document
# ---------------------------------------------------------------------------

class TestChunkDocument:

    def test_single_section_produces_one_chunk(self):
        content = "Contenido de la sección."
        chunks = chunk_document(content, "Mi Artículo")
        assert len(chunks) == 1
        assert chunks[0].startswith("Título: Mi Artículo")

    def test_title_prepended_to_every_chunk(self):
        content = "## Sección 1\nTexto uno.\n## Sección 2\nTexto dos.\n## Sección 3\nTexto tres."
        chunks = chunk_document(content, "Guía")
        assert len(chunks) == 3
        for chunk in chunks:
            assert chunk.startswith("Título: Guía")

    def test_multi_section_doc_three_headers_gives_three_chunks(self):
        content = "## A\nTexto A.\n## B\nTexto B.\n## C\nTexto C."
        chunks = chunk_document(content, "Doc")
        assert len(chunks) == 3

    def test_long_chunk_split_on_blank_line(self):
        # Create a section > 1200 chars with multiple paragraphs
        para = "x" * 500
        content = f"## Sección\n{para}\n\n{para}\n\n{para}"
        chunks = chunk_document(content, "Título")
        # All three 500-char paras + "Título: Título\n\n" prefix → should split
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 1200

    def test_colon_guard_keeps_list_with_intro(self):
        # Paragraph ending in ":" must not be separated from its bullet list even
        # when accumulating them would push the chunk over _CHUNK_MAX_CHARS.
        title = "Guía"
        header = f"Título: {title}\n\n"
        # Build: intro paragraph (~600 chars) that ends with ":",
        # then a bullet list (~400 chars) — together they exceed 1000 but stay under 1200.
        # Add a third plain paragraph big enough that flushing after the colon
        # paragraph would have been "safe" by length, verifying the guard fires.
        intro = "A" * 598 + ":"
        bullets = "\n".join(f"- Item {i}: " + "B" * 30 for i in range(8))
        plain = "C" * 300
        # Put everything in one ## section so paragraph-fallback runs
        content = f"## Sección\n{intro}\n\n{bullets}\n\n{plain}"
        chunks = chunk_document(content, title)
        # The intro+bullets must appear in the same chunk (colon guard prevents split)
        intro_chunk = next(c for c in chunks if intro in c)
        assert bullets.split("\n")[0] in intro_chunk

    def test_frontmatter_stripped(self):
        content = "---\ntitle: Test\narticle_id: 123\n---\nContenido del artículo."
        chunks = chunk_document(content, "Test")
        assert len(chunks) == 1
        assert "article_id" not in chunks[0]
        assert "---" not in chunks[0]

    def test_empty_body_returns_one_chunk(self):
        chunks = chunk_document("", "Vacío")
        assert len(chunks) == 1

    def test_frontmatter_only_returns_one_chunk(self):
        content = "---\ntitle: Solo frontmatter\n---\n"
        chunks = chunk_document(content, "Solo frontmatter")
        assert len(chunks) == 1


# ---------------------------------------------------------------------------
# retrieve_relevant_chunks
# ---------------------------------------------------------------------------

class TestRetrieveRelevantChunks:

    def test_returns_k_results(self):
        deps = _mock_rag_deps(count=10)
        chunks = retrieve_relevant_chunks(_ctx(), deps.collection, deps.model, k=5)
        assert len(chunks) == 5

    def test_returns_empty_when_collection_empty(self):
        deps = _mock_rag_deps(count=0)
        chunks = retrieve_relevant_chunks(_ctx(), deps.collection, deps.model, k=5)
        assert chunks == []

    def test_model_encode_called_with_title_description(self):
        deps = _mock_rag_deps()
        ctx = _ctx(title="Título consulta", description="Descripción consulta.")
        retrieve_relevant_chunks(ctx, deps.collection, deps.model, k=5)
        call_arg = deps.model.encode.call_args[0][0]
        assert "Título consulta" in call_arg
        assert "Descripción consulta." in call_arg

    def test_uses_query_embeddings_not_query_texts(self):
        deps = _mock_rag_deps()
        retrieve_relevant_chunks(_ctx(), deps.collection, deps.model, k=5)
        call_kwargs = deps.collection.query.call_args[1]
        assert "query_embeddings" in call_kwargs
        assert "query_texts" not in call_kwargs

    def test_chunk_dicts_have_document_metadata_distance_keys(self):
        deps = _mock_rag_deps()
        chunks = retrieve_relevant_chunks(_ctx(), deps.collection, deps.model, k=3)
        for chunk in chunks:
            assert "document" in chunk
            assert "metadata" in chunk
            assert "distance" in chunk


# ---------------------------------------------------------------------------
# _parse_rag_decision
# ---------------------------------------------------------------------------

class TestParseRagDecision:

    def test_parses_tool_use_block(self):
        response = _mock_claude_response(confidence=0.88)
        decision = _parse_rag_decision(response)
        assert decision.action == "auto_respond"
        assert decision.confidence == pytest.approx(0.88)
        assert decision.response_draft == "Respuesta de prueba."

    def test_returns_no_doc_coverage_when_no_tool_call(self):
        response = MagicMock()
        response.content = []
        decision = _parse_rag_decision(response)
        assert decision.action == "none"
        assert decision.none_reason == "no_doc_coverage"

    def test_confidence_as_float(self):
        response = _mock_claude_response(confidence=1)
        decision = _parse_rag_decision(response)
        assert isinstance(decision.confidence, float)

    def test_reasoning_preserved(self):
        response = _mock_claude_response(reasoning="Bien cubierto por la documentación.")
        decision = _parse_rag_decision(response)
        assert decision.reasoning == "Bien cubierto por la documentación."

    def test_missing_confidence_key_returns_none(self):
        block = MagicMock()
        block.type = "tool_use"
        block.name = "record_rag_decision"
        block.input = {"response_draft": "Resp.", "reasoning": "Razón."}  # no confidence key
        response = MagicMock()
        response.content = [block]
        decision = _parse_rag_decision(response)
        assert decision.confidence is None


# ---------------------------------------------------------------------------
# evaluate_with_context
# ---------------------------------------------------------------------------

class TestEvaluateWithContext:

    def test_claude_called_with_rag_decision_tool(self):
        settings = get_settings()
        client = MagicMock()
        client.messages.create.return_value = _mock_claude_response()
        ctx = _ctx()
        chunks = [{"document": "Doc content.", "metadata": {}, "distance": 0.1}]

        evaluate_with_context(ctx, chunks, settings, client)

        call_kwargs = client.messages.create.call_args[1]
        tool_names = [t["name"] for t in call_kwargs["tools"]]
        assert "record_rag_decision" in tool_names

    def test_returns_decision_with_confidence(self):
        settings = get_settings()
        client = MagicMock()
        client.messages.create.return_value = _mock_claude_response(confidence=0.92)
        ctx = _ctx()
        chunks = [{"document": "Doc.", "metadata": {}, "distance": 0.1}]

        decision = evaluate_with_context(ctx, chunks, settings, client)

        assert decision.action == "auto_respond"
        assert decision.confidence == pytest.approx(0.92)


# ---------------------------------------------------------------------------
# _write_rag_miss
# ---------------------------------------------------------------------------

class TestWriteRagMiss:

    def test_row_inserted_in_rag_misses(self):
        conn = _conn()
        _write_rag_miss(conn, "TKT-1", "How to reset password")
        row = conn.execute("SELECT * FROM rag_misses WHERE ticket_id='TKT-1'").fetchone()
        assert row is not None
        assert row["question_summary"] == "How to reset password"

    def test_summary_truncated_to_200_chars(self):
        conn = _conn()
        long_summary = "x" * 300
        _write_rag_miss(conn, "TKT-2", long_summary)
        row = conn.execute("SELECT question_summary FROM rag_misses WHERE ticket_id='TKT-2'").fetchone()
        assert len(row["question_summary"]) == 200


# ---------------------------------------------------------------------------
# rag_evaluate
# ---------------------------------------------------------------------------

class TestRagEvaluate:

    def test_empty_collection_returns_no_doc_coverage(self):
        conn = _conn()
        settings = get_settings()
        client = MagicMock()
        deps = _mock_rag_deps(count=0)
        decision = rag_evaluate(_ctx(), deps, settings, conn, client)
        assert decision.action == "none"
        assert decision.none_reason == "no_doc_coverage"

    def test_high_confidence_returns_auto_respond(self):
        conn = _conn()
        settings = get_settings()
        client = MagicMock()
        client.messages.create.return_value = _mock_claude_response(confidence=0.95)
        deps = _mock_rag_deps()
        decision = rag_evaluate(_ctx(), deps, settings, conn, client)
        assert decision.action == "auto_respond"
        assert decision.confidence == pytest.approx(0.95)

    def test_low_confidence_returns_low_confidence_decision(self):
        conn = _conn()
        settings = get_settings()
        client = MagicMock()
        client.messages.create.return_value = _mock_claude_response(confidence=0.3)
        deps = _mock_rag_deps()
        decision = rag_evaluate(_ctx(), deps, settings, conn, client)
        assert decision.action == "none"
        assert decision.none_reason == "low_confidence"

    def test_low_confidence_writes_rag_miss(self):
        conn = _conn()
        settings = get_settings()
        client = MagicMock()
        client.messages.create.return_value = _mock_claude_response(confidence=0.1)
        deps = _mock_rag_deps()
        ctx = _ctx(ticket_id="TKT-MISS", title="Pregunta sin cobertura")
        rag_evaluate(ctx, deps, settings, conn, client)
        row = conn.execute("SELECT * FROM rag_misses WHERE ticket_id='TKT-MISS'").fetchone()
        assert row is not None

    def test_confidence_none_treated_as_zero(self):
        conn = _conn()
        settings = get_settings()
        client = MagicMock()
        # Claude response omits confidence field
        block = MagicMock()
        block.type = "tool_use"
        block.name = "record_rag_decision"
        block.input = {"response_draft": "Resp.", "reasoning": "Razón.", "confidence": None}
        response = MagicMock()
        response.content = [block]
        client.messages.create.return_value = response
        deps = _mock_rag_deps()
        decision = rag_evaluate(_ctx(), deps, settings, conn, client)
        assert decision.action == "none"
        assert decision.none_reason == "low_confidence"

    def test_encode_error_returns_no_doc_coverage(self):
        conn = _conn()
        settings = get_settings()
        client = MagicMock()
        deps = _mock_rag_deps()
        deps.model.encode.side_effect = RuntimeError("OOM")
        decision = rag_evaluate(_ctx(), deps, settings, conn, client)
        assert decision.action == "none"
        assert decision.none_reason == "no_doc_coverage"

    def test_collection_query_error_returns_no_doc_coverage(self):
        conn = _conn()
        settings = get_settings()
        client = MagicMock()
        deps = _mock_rag_deps()
        deps.collection.query.side_effect = RuntimeError("DB error")
        decision = rag_evaluate(_ctx(), deps, settings, conn, client)
        assert decision.action == "none"
        assert decision.none_reason == "no_doc_coverage"


# ---------------------------------------------------------------------------
# RagDeps.available
# ---------------------------------------------------------------------------

class TestRagDepsAvailable:

    def test_available_when_model_and_collection_and_count(self):
        deps = _mock_rag_deps(count=5)
        assert deps.available is True

    def test_not_available_when_model_none(self):
        deps = _mock_rag_deps()
        deps.model = None
        assert deps.available is False

    def test_not_available_when_collection_none(self):
        deps = _mock_rag_deps()
        deps.collection = None
        assert deps.available is False

    def test_not_available_when_collection_empty(self):
        deps = _mock_rag_deps(count=0)
        assert deps.available is False
