"""
T7 — Prompt templates and tool schema for the evaluation engine.

Prompts are in English (planning language). response_draft sent to customers is Spanish.
Ticket content is wrapped in XML delimiters to harden against prompt injection in
user-supplied fields (Title, Description).
"""
from __future__ import annotations

import html

from panpilot.config import Settings
from panpilot.intelligence.models import TicketContext

SYSTEM_PROMPT = """\
You are PanPilot, an AI triage assistant for a Proactivanet software support team.
Your job is to analyze incoming support tickets and decide on the appropriate automated action.
You must always call the record_decision tool with your analysis.

Available actions:

- clarify: The ticket is missing critical information needed to diagnose the issue \
(environment, software version, error message, steps to reproduce, or supporting evidence). \
Use this when the first response should request this missing information. \
Only use clarify when information is genuinely absent — not merely incomplete.

- auto_respond: This ticket is asking a factual how-to, configuration, or feature question \
that is likely answerable from product documentation. Use this when the question type matches — \
do NOT attempt to compose the answer here, and do NOT require documentation to be present in \
this context. A documentation retrieval step will run next to produce the actual answer. \
Use auto_respond for configuration questions, feature explanations, and procedural how-tos.

- remind: The ticket is waiting on a client response (awaiting_client_reply=yes) and \
has been silent too long. Send a polite follow-up in Spanish via response_draft.

- alert: The ticket has been open and unactioned for too long given its priority level. \
Flag it for agent attention with a brief internal note in reasoning.

- none: No automated action is warranted right now. Set none_reason to the most \
specific reason: no_action_warranted, needs_human, no_doc_coverage, or low_confidence. \
IMPORTANT: do NOT use no_doc_coverage or low_confidence here — those outcomes belong to the \
documentation retrieval step that follows auto_respond. Use no_doc_coverage only when the \
ticket is genuinely outside the product scope, not merely because docs are absent from context.

Rules you must follow:
- PanPilot NEVER changes ticket status, priority, or assignment. It only posts \
comments and annotations.
- response_draft must be in Spanish and must directly address the customer's question.
- response_draft must be plain text only. Proactivanet does not render markdown: \
do not use bold (**text**), italics (*text*), headers (# or ##), bullet symbols \
(- or *), or backtick code blocks. For lists, use plain numbered format \
(1. 2. 3.) followed by plain text. Use short paragraphs separated by blank lines.
- reasoning must be 2-3 sentences explaining WHY you chose this action, not what it is.
- When uncertain between acting and not acting, prefer none over a potentially wrong action.\
"""

DECISION_TOOL: dict = {
    "name": "record_decision",
    "description": "Record the triage decision for this support ticket.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["clarify", "auto_respond", "remind", "alert", "none"],
                "description": "The triage action to take on this ticket.",
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "2-3 sentences explaining why this action was chosen. "
                    "Focus on WHY, not what the action is."
                ),
            },
            "response_draft": {
                "type": "string",
                "description": (
                    "Customer-facing text in Spanish. "
                    "Required for auto_respond and remind. Null for all other actions. "
                    "Plain text only — no markdown, no asterisks, no bullet symbols, "
                    "no headers. Use numbered lists (1. 2. 3.) and plain paragraphs."
                ),
            },
            "none_reason": {
                "type": "string",
                "enum": [
                    "no_action_warranted",
                    "needs_human",
                    "no_doc_coverage",
                    "low_confidence",
                ],
                "description": "Required when action is none. Omit for all other actions.",
            },
        },
        "required": ["action", "reasoning"],
    },
}


RAG_DECISION_TOOL: dict = {
    "name": "record_rag_decision",
    "description": "Record the final answer and confidence after reviewing the documentation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "response_draft": {
                "type": "string",
                "description": (
                    "Complete customer-facing answer in Spanish. Must directly address the question. "
                    "Plain text only — no markdown, no asterisks, no bullet symbols, "
                    "no headers. Use numbered lists (1. 2. 3.) and plain paragraphs."
                ),
            },
            "confidence": {
                "type": "number",
                "description": "0.0-1.0 confidence that the provided documentation fully answers the question.",
            },
            "reasoning": {
                "type": "string",
                "description": "2-3 sentences on why this answer is or isn't well-supported by the docs.",
            },
        },
        "required": ["response_draft", "confidence", "reasoning"],
    },
}


GAP_ANALYSIS_TOOL: dict = {
    "name": "record_gap_analysis",
    "description": "Registra el análisis de la laguna documental para mejora de documentación.",
    "input_schema": {
        "type": "object",
        "properties": {
            "gap_category": {
                "type": "string",
                "description": (
                    "Etiqueta corta (3-6 palabras) que clasifica el tipo de laguna documental. "
                    "Ej: 'Configuración de webhooks', 'Instalación en Windows Server', "
                    "'Integración con Active Directory'."
                ),
            },
            "gap_explanation": {
                "type": "string",
                "maxLength": 300,
                "description": (
                    "Explicación en español (20-40 palabras) de por qué la documentación no fue "
                    "suficiente. Ej: 'La documentación confirma que la funcionalidad existe pero "
                    "no incluye los pasos de configuración avanzada.'"
                ),
            },
        },
        "required": ["gap_category", "gap_explanation"],
    },
}


def build_gap_analysis_message(
    ctx: TicketContext,
    chunks: list[dict],
    none_reason: str,
    confidence: float | None,
    settings: Settings,
) -> str:
    """Build the gap analysis user message for Claude."""
    title_esc = html.escape(ctx.title)
    desc_esc = html.escape((ctx.description or "")[:400])

    if chunks:
        if confidence is None:
            raise ValueError("confidence must be float when chunks are provided")
        docs_block = "\n".join(
            f"[Documento: {html.escape(c['metadata'].get('title', ''))}]\n"
            f"{html.escape(c['document'])}"
            for c in chunks
        )
        return (
            f"Ticket de soporte que no pudo responderse automáticamente.\n"
            f"Confianza obtenida: {confidence:.0%}"
            f" (umbral: {settings.confidence_threshold:.0%})\n\n"
            f"<ticket>\n"
            f"Título: {title_esc}\n"
            f"Descripción: {desc_esc}\n"
            f"</ticket>\n\n"
            f"Fragmentos de documentación recuperados:\n"
            f"<docs>\n"
            f"{docs_block}\n"
            f"</docs>\n\n"
            f"Analiza por qué la documentación existente no fue suficiente para responder con confianza.\n"
            f"Clasifica el tipo de laguna y proporciona una explicación breve."
        )
    return (
        f"Ticket de soporte que no pudo responderse automáticamente.\n"
        f"No se encontró ningún fragmento de documentación relevante en la base de conocimiento.\n\n"
        f"<ticket>\n"
        f"Título: {title_esc}\n"
        f"Descripción: {desc_esc}\n"
        f"</ticket>\n\n"
        f"Clasifica el tipo de pregunta para identificar qué tema falta en la documentación.\n"
        f"La explicación debe indicar que no se encontró documentación sobre este tema."
    )


def build_user_message(ctx: TicketContext) -> str:
    """
    Build the user turn for the evaluation prompt.

    Ticket content is wrapped in XML delimiters so that injection attempts in
    user-supplied fields (e.g. a Title containing </ticket><system>...</system>)
    remain scoped as data and do not affect the prompt structure.
    """
    awaiting = "yes" if ctx.awaiting_client_reply else "no"
    # html.escape on user-supplied fields so injection attempts in Title/Description
    # remain scoped as data and cannot break the XML structure of the prompt.
    return (
        "Analyze the following support ticket and record your decision "
        "using the record_decision tool.\n\n"
        "<ticket>\n"
        f"<id>{ctx.ticket_id}</id>\n"
        f"<title>{html.escape(ctx.title)}</title>\n"
        f"<description>{html.escape(ctx.description)}</description>\n"
        f"<status>{ctx.status}</status>\n"
        f"<priority>{ctx.priority}</priority>\n"
        f"<created_at>{ctx.created_at}</created_at>\n"
        f"<last_modified>{ctx.last_modified}</last_modified>\n"
        f"<awaiting_client_reply>{awaiting}</awaiting_client_reply>\n"
        "</ticket>"
    )


def build_rag_user_message(ctx: TicketContext, chunks: list[dict]) -> str:
    """
    Build the Pass 2 user message with retrieved documentation chunks injected.

    Ticket content and doc chunks are both XML-wrapped to harden against prompt injection.
    """
    awaiting = "yes" if ctx.awaiting_client_reply else "no"
    docs_xml = "\n".join(
        f"<doc index=\"{i + 1}\">\n{html.escape(c['document'])}\n</doc>"
        for i, c in enumerate(chunks)
    )
    return (
        "Review the following support ticket and the retrieved documentation excerpts, "
        "then call the record_rag_decision tool with a complete answer.\n\n"
        "<ticket>\n"
        f"<id>{ctx.ticket_id}</id>\n"
        f"<title>{html.escape(ctx.title)}</title>\n"
        f"<description>{html.escape(ctx.description)}</description>\n"
        f"<status>{ctx.status}</status>\n"
        f"<priority>{ctx.priority}</priority>\n"
        f"<awaiting_client_reply>{awaiting}</awaiting_client_reply>\n"
        "</ticket>\n\n"
        "<documentation>\n"
        f"{docs_xml}\n"
        "</documentation>"
    )
