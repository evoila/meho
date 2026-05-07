# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Heading-aware Markdown chunker for knowledge ingestion.

Splits Markdown bodies into chunks bounded by *word count*, preserving the
ATX heading hierarchy as part of the per-chunk metadata. Word counting is
embedding-model-agnostic — a 512-word target leaves headroom for any
current dense model's 512+ token context, and avoids pulling in tokenizer
dependencies (tiktoken/transformers/torch).

Algorithm ported from MEHO.Knowledge/src/meho_knowledge/chunking/pipeline.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

# ATX-style Markdown headings (`#` through `######`).
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*\s*$", re.MULTILINE)


@dataclass
class _Section:
    heading_path: list[str] = field(default_factory=list)
    body: str = ""


class TextChunker:
    """Heading-aware Markdown chunker.

    Public API (unchanged from earlier tiktoken-based implementation):

    - ``chunk_text(text)`` -> ``list[str]``
    - ``chunk_pages(pages)`` -> ``list[(text, page_number)]``
    - ``chunk_document_with_structure(text, document_name, detect_headings=True)``
      -> ``list[(text, context)]`` where context contains ``heading_stack`` +
      ``document_name``.

    Internally:

    1. Split the document on ATX headings, tracking the heading hierarchy.
    2. Within each section, greedy-pack paragraphs (split on blank lines)
       until the next paragraph would push the buffer past ``max_tokens``
       words. Emit a chunk and seed the next buffer with ``overlap_tokens``
       trailing words for context continuity.
    """

    def __init__(
        self,
        max_tokens: int = 512,
        overlap_tokens: int = 64,
        encoding_name: str = "",
    ) -> None:
        # ``encoding_name`` is accepted for backwards compatibility with
        # older callers that explicitly passed a tiktoken encoding. It has
        # no effect — chunking is word-based.
        del encoding_name
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens

    # ------------------------------------------------------------------
    # Plain-text chunking
    # ------------------------------------------------------------------

    def chunk_text(self, text: str) -> list[str]:
        """Chunk a Markdown/plain-text body into word-bounded chunks."""
        if not text or not text.strip():
            return []

        sections = _split_sections(text)
        out: list[str] = []
        for section in sections:
            out.extend(
                _pack_paragraphs(
                    section.body,
                    max_tokens=self.max_tokens,
                    overlap_tokens=self.overlap_tokens,
                )
            )
        return out

    def chunk_pages(self, pages: list[str]) -> list[tuple[str, int]]:
        """Chunk a list of per-page texts (e.g., from PDF) preserving page numbers."""
        chunks_with_pages: list[tuple[str, int]] = []
        for page_num, page_text in enumerate(pages, start=1):
            if not page_text or not page_text.strip():
                continue
            for chunk in self.chunk_text(page_text):
                chunks_with_pages.append((chunk, page_num))
        return chunks_with_pages

    # ------------------------------------------------------------------
    # Heading-aware chunking with structural context
    # ------------------------------------------------------------------

    def chunk_document_with_structure(
        self,
        text: str,
        document_name: str,
        detect_headings: bool = True,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Chunk a document and return chunks paired with structural context.

        Each context dict carries ``heading_stack`` (the hierarchical
        heading path, e.g., ``["Chapter 1", "1.2 Foo"]``) and
        ``document_name``.
        """
        chunks: list[tuple[str, dict[str, Any]]] = []
        if not text or not text.strip():
            return chunks

        if not detect_headings:
            for chunk in self.chunk_text(text):
                chunks.append((chunk, {"document_name": document_name, "heading_stack": []}))
            return chunks

        for section in _split_sections(text):
            for body in _pack_paragraphs(
                section.body,
                max_tokens=self.max_tokens,
                overlap_tokens=self.overlap_tokens,
            ):
                chunks.append(
                    (
                        body,
                        {
                            "document_name": document_name,
                            "heading_stack": list(section.heading_path),
                        },
                    )
                )
        return chunks


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def chunk_text(
    text: str,
    max_tokens: int = 512,
    overlap_tokens: int = 64,
    encoding_name: str = "",
) -> list[str]:
    """Convenience wrapper that returns word-bounded Markdown chunks."""
    del encoding_name  # accepted for backwards compatibility
    return TextChunker(max_tokens=max_tokens, overlap_tokens=overlap_tokens).chunk_text(text)


def chunk_markdown(
    markdown: str,
    *,
    max_words: int = 512,
    overlap_words: int = 64,
) -> list[tuple[str, list[str]]]:
    """Heading-aware Markdown chunker returning ``(text, heading_path)`` pairs.

    Handy for ingestion paths that want the heading path attached to each
    chunk without going through the document_name-shaped context dict.
    """
    if not markdown.strip():
        return []
    out: list[tuple[str, list[str]]] = []
    for section in _split_sections(markdown):
        for body in _pack_paragraphs(
            section.body, max_tokens=max_words, overlap_tokens=overlap_words
        ):
            out.append((body, list(section.heading_path)))
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _split_sections(markdown: str) -> list[_Section]:
    """Split the document on ATX headings, tracking the heading hierarchy."""
    matches = list(_HEADING_RE.finditer(markdown))
    if not matches:
        return [_Section(heading_path=[], body=markdown.strip())]

    sections: list[_Section] = []
    pre = markdown[: matches[0].start()].strip()
    if pre:
        sections.append(_Section(heading_path=[], body=pre))

    stack: list[tuple[int, str]] = []
    for i, m in enumerate(matches):
        level = len(m.group("hashes"))
        heading_text = m.group("text").strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading_text))
        path = [t for _, t in stack]

        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[body_start:body_end].strip()
        if body:
            sections.append(_Section(heading_path=path, body=body))

    return sections


def _pack_paragraphs(
    body: str,
    *,
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    """Greedy-pack paragraphs into word-bounded chunks with tail overlap."""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", body) if p.strip()]
    if not paragraphs:
        return []

    out: list[str] = []
    buf: list[str] = []
    buf_words = 0

    def flush() -> None:
        nonlocal buf, buf_words
        if buf:
            out.append("\n\n".join(buf))
            tail = _tail_words(buf, overlap_tokens)
            buf = [tail] if tail else []
            buf_words = _count_words(tail) if tail else 0

    for paragraph in paragraphs:
        words = _count_words(paragraph)
        if words >= max_tokens and not buf:
            out.append(paragraph)
            buf = []
            buf_words = 0
            continue
        if buf_words + words > max_tokens and buf:
            flush()
        buf.append(paragraph)
        buf_words += words

    flush()
    return out


def _count_words(text: str) -> int:
    return len(text.split())


def _tail_words(parts: Iterable[str], n: int) -> str:
    """Return the last ``n`` words across the joined parts, as a string."""
    if n <= 0:
        return ""
    joined = "\n\n".join(parts)
    words = joined.split()
    if len(words) <= n:
        return joined
    return " ".join(words[-n:])
