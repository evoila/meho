# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for the heading-aware Markdown chunker.
"""

import pytest

from meho_app.modules.knowledge.chunking import TextChunker, chunk_markdown, chunk_text


@pytest.mark.unit
def test_chunk_short_text() -> None:
    chunker = TextChunker(max_tokens=100, overlap_tokens=10)
    short_text = "This is a short text."
    chunks = chunker.chunk_text(short_text)
    assert len(chunks) == 1
    assert chunks[0] == short_text


@pytest.mark.unit
def test_chunk_empty_text() -> None:
    chunker = TextChunker()
    assert chunker.chunk_text("") == []
    assert chunker.chunk_text("   ") == []


@pytest.mark.unit
def test_chunk_long_paragraphs_into_word_bounded_chunks() -> None:
    chunker = TextChunker(max_tokens=50, overlap_tokens=10)
    paragraphs = "\n\n".join(" ".join(["word"] * 30) for _ in range(10))
    chunks = chunker.chunk_text(paragraphs)
    assert len(chunks) > 1
    for chunk in chunks:
        # Allow exceeding by one paragraph minus overlap; greedy packing.
        assert len(chunk.split()) <= 50 + 30


@pytest.mark.unit
def test_oversized_paragraph_emitted_alone() -> None:
    chunker = TextChunker(max_tokens=20, overlap_tokens=4)
    text = " ".join(["word"] * 100)
    chunks = chunker.chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0] == text


@pytest.mark.unit
def test_chunk_respects_paragraph_boundaries() -> None:
    chunker = TextChunker(max_tokens=20, overlap_tokens=4)
    text = "First paragraph.\n\nSecond paragraph here.\n\nThird paragraph closes."
    chunks = chunker.chunk_text(text)
    assert all("\n\n" in c or len(c.split()) <= 20 for c in chunks)


@pytest.mark.unit
def test_heading_path_tracked_in_chunk_markdown() -> None:
    md = (
        "# Top\n\nintro paragraph.\n\n"
        "## Subsection A\n\nsubsection a body.\n\n"
        "### Deeper\n\ndeeper body.\n\n"
        "## Subsection B\n\nsubsection b body.\n"
    )
    chunks = chunk_markdown(md, max_words=64, overlap_words=8)
    paths = [path for _, path in chunks]
    assert ["Top"] in paths
    assert ["Top", "Subsection A"] in paths
    assert ["Top", "Subsection A", "Deeper"] in paths
    assert ["Top", "Subsection B"] in paths


@pytest.mark.unit
def test_chunk_text_module_helper() -> None:
    chunks = chunk_text("hello world", max_tokens=64, overlap_tokens=8)
    assert chunks == ["hello world"]


@pytest.mark.unit
def test_chunk_document_with_structure_attaches_metadata() -> None:
    chunker = TextChunker(max_tokens=64, overlap_tokens=8)
    md = "# Top\n\nbody.\n\n## Sub\n\nsub body.\n"
    out = chunker.chunk_document_with_structure(md, document_name="doc.md")
    assert {ctx["document_name"] for _, ctx in out} == {"doc.md"}
    headings = {tuple(ctx["heading_stack"]) for _, ctx in out}
    assert ("Top",) in headings
    assert ("Top", "Sub") in headings


@pytest.mark.unit
def test_chunk_document_with_structure_disabled_headings() -> None:
    chunker = TextChunker(max_tokens=64, overlap_tokens=8)
    md = "# Top\n\nbody.\n"
    out = chunker.chunk_document_with_structure(md, document_name="doc.md", detect_headings=False)
    assert all(ctx["heading_stack"] == [] for _, ctx in out)
