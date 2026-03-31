"""Heading-aware markdown chunker with token limit fallback.

Splits markdown text at heading boundaries (# through ######), keeping
each section as one chunk if under the token limit. Oversized sections
fall back to paragraph splitting (double newline), then fixed-size splitting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Heading regex: matches markdown ATX headings (# through ######)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

# Approximate token limit per chunk (sweet spot for RAG retrieval)
MAX_CHUNK_TOKENS = 512
WORDS_PER_TOKEN = 0.75  # ~1.3 tokens per word


@dataclass
class Chunk:
    """A single chunk of knowledge content."""

    content: str
    heading: str  # The heading this chunk falls under (empty for preamble/no-heading)
    chunk_index: int
    token_estimate: int


def _estimate_tokens(text: str) -> int:
    """Approximate token count from word count."""
    words = text.split()
    if not words:
        return 0
    return int(len(words) / WORDS_PER_TOKEN)


def chunk_markdown(text: str, max_tokens: int = MAX_CHUNK_TOKENS) -> list[Chunk]:
    """Split markdown text into chunks respecting heading boundaries.

    Strategy:
    1. Split on headings (any level)
    2. Keep each section as one chunk if under token limit
    3. If a section exceeds limit, split on double newlines (paragraphs)
    4. If a paragraph still exceeds limit, split on fixed token boundary

    Args:
        text: Markdown text to chunk.
        max_tokens: Maximum tokens per chunk (default 512).

    Returns:
        List of Chunk objects with sequential chunk_index values.
    """
    if not text or not text.strip():
        return []

    sections = _split_on_headings(text)
    chunks: list[Chunk] = []

    for heading, section_text in sections:
        tokens = _estimate_tokens(section_text)
        if tokens <= max_tokens:
            chunks.append(
                Chunk(
                    content=section_text.strip(),
                    heading=heading,
                    chunk_index=len(chunks),
                    token_estimate=tokens,
                )
            )
        else:
            # Sub-split on paragraphs
            sub_chunks = _split_large_section(heading, section_text, max_tokens)
            for sc in sub_chunks:
                sc.chunk_index = len(chunks)
                chunks.append(sc)

    return chunks


def _split_on_headings(text: str) -> list[tuple[str, str]]:
    """Split text into (heading, content) pairs at heading boundaries.

    The heading line is included in the section content.
    Content before the first heading (preamble) gets heading="".

    Args:
        text: Markdown text to split.

    Returns:
        List of (heading_line, section_content) tuples.
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [("", text)]

    sections: list[tuple[str, str]] = []

    # Content before first heading (preamble)
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()]
        if preamble.strip():
            sections.append(("", preamble))

    for i, match in enumerate(matches):
        heading = match.group(0)  # Full heading line including #
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end]
        sections.append((heading, content))

    return sections


def _split_large_section(
    heading: str, text: str, max_tokens: int
) -> list[Chunk]:
    """Split an oversized section into smaller chunks.

    First splits on double newlines (paragraphs). If a single paragraph
    exceeds the limit, falls back to fixed-size word splitting.

    Args:
        heading: The heading this section falls under.
        text: Section text to split.
        max_tokens: Maximum tokens per chunk.

    Returns:
        List of Chunk objects (chunk_index will be reassigned by caller).
    """
    paragraphs = re.split(r"\n\n+", text.strip())
    chunks: list[Chunk] = []
    current_parts: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_tokens = _estimate_tokens(para)

        if para_tokens > max_tokens:
            # Flush accumulated content first
            if current_parts:
                content = "\n\n".join(current_parts)
                chunks.append(
                    Chunk(
                        content=content,
                        heading=heading,
                        chunk_index=0,
                        token_estimate=_estimate_tokens(content),
                    )
                )
                current_parts = []
                current_tokens = 0

            # Fixed-size split for oversized paragraph
            words = para.split()
            max_words = int(max_tokens * WORDS_PER_TOKEN)
            if max_words < 1:
                max_words = 1
            for j in range(0, len(words), max_words):
                word_slice = words[j : j + max_words]
                content = " ".join(word_slice)
                chunks.append(
                    Chunk(
                        content=content,
                        heading=heading,
                        chunk_index=0,
                        token_estimate=_estimate_tokens(content),
                    )
                )
        elif current_tokens + para_tokens > max_tokens and current_parts:
            # Flush current accumulation, start new chunk
            content = "\n\n".join(current_parts)
            chunks.append(
                Chunk(
                    content=content,
                    heading=heading,
                    chunk_index=0,
                    token_estimate=_estimate_tokens(content),
                )
            )
            current_parts = [para]
            current_tokens = para_tokens
        else:
            current_parts.append(para)
            current_tokens += para_tokens

    # Flush remaining
    if current_parts:
        content = "\n\n".join(current_parts)
        chunks.append(
            Chunk(
                content=content,
                heading=heading,
                chunk_index=0,
                token_estimate=_estimate_tokens(content),
            )
        )

    return chunks
