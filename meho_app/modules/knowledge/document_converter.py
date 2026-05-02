# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Shared document conversion utilities.

Provides SUPPORTED_MIME_TYPES, document summary generation, and
chunk prefix building used by both DoclingWrapperAdapter and
LightweightDocumentConverter ingestion paths.

NOTE: The DoclingDocumentConverter class that previously lived here
has been replaced by DoclingWrapperAdapter (docling_adapter.py), which
uses DoclingWrapper for subprocess-isolated conversion with
HierarchicalChunker. The old TOC/batch splitting functions have been
removed -- DoclingWrapper handles PDF splitting internally via pikepdf.
"""

from meho_app.core.otel import get_logger

logger = get_logger(__name__)

SUPPORTED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/html",
    }
)


async def generate_document_summary(
    document_text: str,
    connector_type: str | None = None,
    connector_name: str | None = None,
) -> str:
    """Generate a 1-2 sentence document summary for chunk enrichment (D-05).

    Uses the app's configured classifier model (defaults to Sonnet).
    Returns empty string on failure (ingestion continues without summary).
    """
    import asyncio

    from pydantic_ai import Agent

    from meho_app.modules.knowledge.answer import KNOWLEDGE_ANSWER_MODEL

    agent = Agent(
        KNOWLEDGE_ANSWER_MODEL,
        system_prompt=(
            "Summarize this document in 1-2 sentences. "
            "Focus on what systems, technologies, or procedures it covers. "
            "If a table of contents is present, use it to identify the major topics. "
            "Return ONLY the summary, no explanation."
        ),
    )

    # First 16K chars (~4 pages) to capture title, TOC, and introduction.
    text_preview = document_text[:16000]

    try:
        result = await asyncio.wait_for(agent.run(text_preview), timeout=15.0)
        return str(result.output).strip()
    except (TimeoutError, Exception) as e:
        logger.warning("document_summary_generation_failed", error=str(e))
        return ""


def build_chunk_prefix(
    connector_type: str | None = None,
    connector_name: str | None = None,
    document_summary: str = "",
) -> str:
    """Build the context prefix prepended to each chunk before embedding (D-05).

    Format: "{connector_type} connector ({connector_name}). {summary}"
    """
    parts: list[str] = []
    if connector_type:
        connector_context = f"{connector_type} connector"
        if connector_name:
            connector_context += f" ({connector_name})"
        connector_context += "."
        parts.append(connector_context)
    if document_summary:
        parts.append(document_summary)
    return " ".join(parts)
