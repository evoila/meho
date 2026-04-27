# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Grounded LLM answer generation for knowledge search results."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from meho_app.core.config import MODEL_CLAUDE_SONNET
from meho_app.core.otel import get_logger
from meho_app.modules.agents.base.inference import infer_structured
from meho_app.modules.knowledge.retrieval_context import format_heading_path, format_page_range

logger = get_logger(__name__)

KNOWLEDGE_ANSWER_MODEL = MODEL_CLAUDE_SONNET

# Matches inline citation markers the LLM sometimes emits in the answer body,
# e.g. [0], [12], or grouped forms like [0, 1] / [0][1]. We only strip markers
# whose index is no longer backed by a valid citation after sanitization.
_CITATION_MARKER_RE = re.compile(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]")

INSUFFICIENT_CONTEXT_MESSAGE = (
    "The retrieved chunks do not contain enough information to answer the question."
)

SYSTEM_PROMPT = """\
You are a precise document research assistant.

The user will ask a question and provide numbered retrieved chunks as context.
Answer using ONLY the provided chunks.

Critical rules:
- Never answer from your pre-trained knowledge or general world knowledge.
- Never invent, infer, or fill in missing details that are not explicitly stated in the chunks.
- If the retrieved chunks do not contain enough information, say that clearly.
- Synthesize across multiple chunks when needed.
- Write the answer in your own words. Do not just copy chunk text into the answer.
- Use verbatim text only inside citations.
- Every factual claim in the answer must be supported by at least one citation.
- Each citation must include:
  - the 0-based `chunk_index`
  - a short verbatim `quote` copied from that chunk
- The quote must appear exactly in the cited chunk.
- Keep the answer concise and factual.
- Write the answer in the same language as the user's question.

Return valid JSON matching the response schema."""


class GroundedCitation(BaseModel):
    """Reference to a supporting chunk."""

    chunk_index: int = Field(ge=0, description="0-based index into the retrieved chunks array")
    quote: str = Field(min_length=1, description="Short verbatim quote copied from the chunk")


class GroundedAnswer(BaseModel):
    """Structured grounded answer with citations."""

    answer: str = Field(description="Concise grounded answer to the user's question")
    citations: list[GroundedCitation] = Field(
        default_factory=list,
        description="Chunk citations supporting the answer",
    )


def _string_field(result: Mapping[str, Any], key: str) -> str:
    """Return a normalized string field."""
    value = result.get(key)
    return value.strip() if isinstance(value, str) else ""


def _int_field(result: Mapping[str, Any], key: str) -> int:
    """Return a normalized integer field."""
    value = result.get(key)
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _context_block(results: Sequence[Mapping[str, Any]]) -> str:
    """Render retrieved chunks into a prompt-friendly context block."""
    context_parts: list[str] = []

    for index, result in enumerate(results):
        header_parts = [f"[Chunk {index}]"]

        filename = _string_field(result, "filename")
        if filename:
            header_parts.append(f"file: {filename}")

        heading_path = result.get("heading_path")
        is_path_list = isinstance(heading_path, Sequence) and not isinstance(
            heading_path, (str, bytes)
        )
        path = format_heading_path(heading_path if is_path_list else [])
        if path:
            header_parts.append(f"path: {path}")
        else:
            section_header = _string_field(result, "section_header")
            if section_header:
                header_parts.append(f"section: {section_header}")

        page_label = format_page_range(
            page_number=_int_field(result, "page_number"),
            page_start=_int_field(result, "page_start"),
            page_end=_int_field(result, "page_end"),
        )
        if page_label:
            header_parts.append(f"pages: {page_label}")

        source_chunk_index = result.get("source_chunk_index")
        if isinstance(source_chunk_index, int) and source_chunk_index >= 0:
            header_parts.append(f"source chunk: {source_chunk_index}")

        header = " (" + ") (".join(header_parts[1:]) + ")" if len(header_parts) > 1 else ""
        context_parts.append(f"{header_parts[0]}{header}\n{_string_field(result, 'text')}")

    return "\n\n---\n\n".join(context_parts)


def _normalize_text(text: str) -> str:
    """Collapse whitespace for fuzzy quote matching."""
    return " ".join(text.split()).lower()


def _strip_dangling_citation_markers(text: str, valid_indices: set[int]) -> str:
    """Remove inline ``[N]`` markers whose index is no longer a valid citation.

    Handles single markers (``[0]``) and comma-grouped forms (``[0, 1]``):
    if every referenced index was dropped, the marker is removed entirely;
    otherwise the marker is rewritten with only the surviving indices.
    Adjacent whitespace is collapsed so the answer does not end up with
    stray double spaces.
    """

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        indices = [int(part.strip()) for part in raw.split(",")]
        kept = [idx for idx in indices if idx in valid_indices]
        if not kept:
            return ""
        return "[" + ", ".join(str(idx) for idx in kept) + "]"

    cleaned = _CITATION_MARKER_RE.sub(_replace, text)
    # Collapse the whitespace holes left behind when we removed a marker.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" +([.,;:!?])", r"\1", cleaned)
    return cleaned.strip()


def _sanitize_answer(
    answer: GroundedAnswer,
    results: Sequence[Mapping[str, Any]],
) -> GroundedAnswer:
    """Drop invalid citations and normalize the final answer payload.

    Validates bounds, deduplication, whitespace, and that each quoted
    fragment actually appears in the referenced chunk text. Any inline
    ``[N]`` markers in the answer that reference a citation we dropped are
    stripped so the rendered answer never points at a missing source.
    """
    result_count = len(results)

    sanitized_citations: list[GroundedCitation] = []
    seen: set[tuple[int, str]] = set()
    valid_indices: set[int] = set()
    for citation in answer.citations:
        quote = citation.quote.strip()
        if not quote:
            continue
        if citation.chunk_index < 0 or citation.chunk_index >= result_count:
            continue

        chunk_text = str(results[citation.chunk_index].get("text", ""))
        if _normalize_text(quote) not in _normalize_text(chunk_text):
            continue

        key = (citation.chunk_index, quote)
        if key in seen:
            continue
        seen.add(key)
        valid_indices.add(citation.chunk_index)
        sanitized_citations.append(
            GroundedCitation(
                chunk_index=citation.chunk_index,
                quote=quote,
            )
        )

    cleaned_answer = _strip_dangling_citation_markers(answer.answer, valid_indices)
    normalized_answer = cleaned_answer.strip() or INSUFFICIENT_CONTEXT_MESSAGE

    return GroundedAnswer(answer=normalized_answer, citations=sanitized_citations)


async def generate_grounded_answer(
    *,
    query: str,
    results: Sequence[Mapping[str, Any]],
) -> GroundedAnswer:
    """Generate a grounded answer from retrieved chunks only."""
    prompt = (
        f"Question: {query}\n\n"
        "IMPORTANT: Answer in the same language as the question.\n\n"
        f"Retrieved chunks:\n\n{_context_block(results)}"
    )

    answer = await infer_structured(
        prompt,
        GroundedAnswer,
        model=KNOWLEDGE_ANSWER_MODEL,
        instructions=SYSTEM_PROMPT,
        temperature=0.0,
        max_tokens=800,
    )
    return _sanitize_answer(answer, results)
