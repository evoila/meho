# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for DoclingDocumentConverter.

Tests cover convert_file, get_full_text, and chunk_document methods
including TOC filtering via EXCLUDED_LABELS, heading enrichment via
contextualize(), prefix prepending, and error paths.

All Docling types are mocked since docling is not installed in the test env.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# FakeLabel: lightweight stand-in for DocItemLabel enum values
# ---------------------------------------------------------------------------

class FakeLabel:
    """Stand-in for DocItemLabel enum values that supports set comparison."""

    def __init__(self, value: str) -> None:
        self.value = value

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: object) -> bool:
        return hasattr(other, "value") and self.value == other.value

    def __repr__(self) -> str:
        return f"FakeLabel({self.value!r})"


# Pre-define label constants matching DocItemLabel names
DOCUMENT_INDEX = FakeLabel("document_index")
PAGE_HEADER = FakeLabel("page_header")
PAGE_FOOTER = FakeLabel("page_footer")
PAGE_NUMBER = FakeLabel("page_number")
TEXT = FakeLabel("text")
SECTION_HEADER = FakeLabel("section_header")


# ---------------------------------------------------------------------------
# Inject fake docling modules into sys.modules so document_converter.py
# can be imported without the real docling package.
# ---------------------------------------------------------------------------

_DOCLING_MODULES = [
    "docling",
    "docling.chunking",
    "docling.datamodel",
    "docling.datamodel.base_models",
    "docling.datamodel.document",
    "docling.document_converter",
    "docling_core",
    "docling_core.types",
    "docling_core.types.doc",
    "docling_core.types.doc.labels",
]

_fake_modules: dict[str, MagicMock] = {}
for _mod_name in _DOCLING_MODULES:
    if _mod_name not in sys.modules:
        _fake_modules[_mod_name] = MagicMock()
        sys.modules[_mod_name] = _fake_modules[_mod_name]

# Wire up DocItemLabel on the fake labels module so the class-level
# EXCLUDED_LABELS set gets our FakeLabel sentinels.
_labels_mod = sys.modules["docling_core.types.doc.labels"]
_labels_mod.DocItemLabel = MagicMock()
_labels_mod.DocItemLabel.DOCUMENT_INDEX = DOCUMENT_INDEX
_labels_mod.DocItemLabel.PAGE_HEADER = PAGE_HEADER
_labels_mod.DocItemLabel.PAGE_FOOTER = PAGE_FOOTER
_labels_mod.DocItemLabel.PAGE_NUMBER = PAGE_NUMBER
_labels_mod.DocItemLabel.TEXT = TEXT
_labels_mod.DocItemLabel.SECTION_HEADER = SECTION_HEADER

# Also expose DocItemLabel at the types.doc level (import path: from docling_core.types.doc import ...)
sys.modules["docling_core.types.doc"].DocItemLabel = _labels_mod.DocItemLabel
sys.modules["docling_core.types.doc"].DoclingDocument = MagicMock()

# Ensure document_converter is freshly imported with our fakes
if "meho_app.modules.knowledge.document_converter" in sys.modules:
    del sys.modules["meho_app.modules.knowledge.document_converter"]

# Now import the module under test -- all docling references resolve to our fakes
from meho_app.modules.knowledge.document_converter import DoclingDocumentConverter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_converter() -> DoclingDocumentConverter:
    """Create a DoclingDocumentConverter with mocked internals."""
    with patch("meho_app.modules.knowledge.document_converter.DocumentConverter"), \
         patch("meho_app.modules.knowledge.document_converter.HybridChunker"):
        converter = DoclingDocumentConverter()
    return converter


def _make_chunk(
    doc_items: list | None = None,
    headings: list[str] | None = None,
    page_numbers: list[int] | None = None,
) -> MagicMock:
    """Create a mock chunk with configurable meta attributes."""
    chunk = MagicMock()
    if doc_items is not None:
        chunk.meta.doc_items = doc_items
    else:
        # No doc_items attribute
        del chunk.meta.doc_items
    if headings is not None:
        chunk.meta.headings = headings
    else:
        chunk.meta.headings = []
    if page_numbers is not None:
        chunk.meta.page_numbers = page_numbers
    else:
        chunk.meta.page_numbers = []
    return chunk


def _make_doc_item(label: FakeLabel) -> MagicMock:
    """Create a mock doc_item with a specific label."""
    item = MagicMock()
    item.label = label
    return item


# ---------------------------------------------------------------------------
# Tests: convert_file
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_convert_file_pdf():
    """convert_file accepts PDF MIME type and returns the mock document."""
    converter = _make_converter()

    mock_result = MagicMock()
    mock_doc = MagicMock()
    mock_result.document = mock_doc
    converter._converter.convert.return_value = mock_result

    with patch("meho_app.modules.knowledge.document_converter.DocumentStream"):
        result = converter.convert_file(b"fake-pdf", "test.pdf", "application/pdf")

    assert result is mock_doc
    converter._converter.convert.assert_called_once()


@pytest.mark.unit
def test_convert_file_docx():
    """convert_file accepts DOCX MIME type and returns the mock document."""
    converter = _make_converter()

    mock_result = MagicMock()
    mock_doc = MagicMock()
    mock_result.document = mock_doc
    converter._converter.convert.return_value = mock_result

    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    with patch("meho_app.modules.knowledge.document_converter.DocumentStream"):
        result = converter.convert_file(b"fake-docx", "test.docx", docx_mime)

    assert result is mock_doc
    converter._converter.convert.assert_called_once()


@pytest.mark.unit
def test_convert_file_unsupported_mime():
    """convert_file raises ValueError for unsupported MIME types."""
    converter = _make_converter()

    with pytest.raises(ValueError, match="Unsupported MIME type: application/unknown"):
        converter.convert_file(b"data", "test.xyz", "application/unknown")


@pytest.mark.unit
def test_convert_file_conversion_failure():
    """convert_file raises ValueError when Docling conversion fails."""
    converter = _make_converter()
    converter._converter.convert.side_effect = Exception("parse error")

    with patch("meho_app.modules.knowledge.document_converter.DocumentStream"):
        with pytest.raises(ValueError, match="Failed to convert test.pdf: parse error"):
            converter.convert_file(b"bad-data", "test.pdf", "application/pdf")


# ---------------------------------------------------------------------------
# Tests: get_full_text
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_get_full_text():
    """get_full_text delegates to doc.export_to_markdown()."""
    converter = _make_converter()

    mock_doc = MagicMock()
    mock_doc.export_to_markdown.return_value = "# Title\nContent here"

    result = converter.get_full_text(mock_doc)

    assert result == "# Title\nContent here"
    mock_doc.export_to_markdown.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: chunk_document -- TOC filtering
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_chunk_document_filters_toc():
    """Chunks where ALL doc_items have DOCUMENT_INDEX label are excluded."""
    converter = _make_converter()

    toc_chunk = _make_chunk(
        doc_items=[_make_doc_item(DOCUMENT_INDEX)],
        headings=[],
        page_numbers=[3],
    )
    content_chunk = _make_chunk(
        doc_items=[_make_doc_item(TEXT)],
        headings=["Chapter 1"],
        page_numbers=[5],
    )

    converter._chunker.chunk.return_value = [toc_chunk, content_chunk]
    converter._chunker.contextualize.return_value = "# Chapter 1\nContent text"

    mock_doc = MagicMock()
    mock_doc.name = "test.pdf"

    results = converter.chunk_document(mock_doc)

    assert len(results) == 1
    text, context = results[0]
    assert "Content text" in text


@pytest.mark.unit
def test_chunk_document_filters_page_furniture():
    """Chunks with only PAGE_HEADER + PAGE_FOOTER labels are excluded."""
    converter = _make_converter()

    furniture_chunk = _make_chunk(
        doc_items=[_make_doc_item(PAGE_HEADER), _make_doc_item(PAGE_FOOTER)],
        headings=[],
        page_numbers=[1],
    )
    content_chunk = _make_chunk(
        doc_items=[_make_doc_item(TEXT)],
        headings=["Section A"],
        page_numbers=[2],
    )

    converter._chunker.chunk.return_value = [furniture_chunk, content_chunk]
    converter._chunker.contextualize.return_value = "Section A content"

    mock_doc = MagicMock()
    mock_doc.name = "report.pdf"

    results = converter.chunk_document(mock_doc)

    assert len(results) == 1
    text, _ = results[0]
    assert "Section A content" in text


# ---------------------------------------------------------------------------
# Tests: chunk_document -- heading enrichment
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_chunk_document_heading_enrichment():
    """contextualize() is called on each kept chunk for heading enrichment."""
    converter = _make_converter()

    content_chunk = _make_chunk(
        doc_items=[_make_doc_item(TEXT)],
        headings=["Chapter 1", "Section 1.1"],
        page_numbers=[10],
    )

    converter._chunker.chunk.return_value = [content_chunk]
    converter._chunker.contextualize.return_value = "# Chapter 1 > Section 1.1\nRich content"

    mock_doc = MagicMock()
    mock_doc.name = "doc.pdf"

    results = converter.chunk_document(mock_doc)

    assert len(results) == 1
    text, _ = results[0]
    assert text == "# Chapter 1 > Section 1.1\nRich content"
    converter._chunker.contextualize.assert_called_once_with(content_chunk)


# ---------------------------------------------------------------------------
# Tests: chunk_document -- prefix prepending
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_chunk_document_with_prefix():
    """chunk_prefix is prepended to each chunk with double newline separator."""
    converter = _make_converter()

    content_chunk = _make_chunk(
        doc_items=[_make_doc_item(TEXT)],
        headings=["Intro"],
        page_numbers=[1],
    )

    converter._chunker.chunk.return_value = [content_chunk]
    converter._chunker.contextualize.return_value = "Introduction content"

    mock_doc = MagicMock()
    mock_doc.name = "guide.pdf"

    results = converter.chunk_document(mock_doc, chunk_prefix="kubernetes connector.")

    assert len(results) == 1
    text, _ = results[0]
    assert text.startswith("kubernetes connector.\n\n")
    assert "Introduction content" in text


@pytest.mark.unit
def test_chunk_document_without_prefix():
    """Empty chunk_prefix does not add any prefix to output."""
    converter = _make_converter()

    content_chunk = _make_chunk(
        doc_items=[_make_doc_item(TEXT)],
        headings=["Intro"],
        page_numbers=[1],
    )

    converter._chunker.chunk.return_value = [content_chunk]
    converter._chunker.contextualize.return_value = "Raw content"

    mock_doc = MagicMock()
    mock_doc.name = "doc.pdf"

    results = converter.chunk_document(mock_doc, chunk_prefix="")

    assert len(results) == 1
    text, _ = results[0]
    assert text == "Raw content"
    assert not text.startswith("\n\n")


# ---------------------------------------------------------------------------
# Tests: chunk_document -- context metadata
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_chunk_document_context_metadata():
    """Returned context dict has heading_stack, page_numbers, and document_name."""
    converter = _make_converter()

    content_chunk = _make_chunk(
        doc_items=[_make_doc_item(TEXT)],
        headings=["Chapter 1", "Section 1.1"],
        page_numbers=[5, 6],
    )

    converter._chunker.chunk.return_value = [content_chunk]
    converter._chunker.contextualize.return_value = "Content"

    mock_doc = MagicMock()
    mock_doc.name = "manual.pdf"

    results = converter.chunk_document(mock_doc)

    assert len(results) == 1
    _, context = results[0]
    assert context["heading_stack"] == ["Chapter 1", "Section 1.1"]
    assert context["page_numbers"] == [5, 6]
    assert context["document_name"] == "manual.pdf"


# ---------------------------------------------------------------------------
# Tests: chunk_document -- edge cases
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_chunk_document_no_doc_items():
    """Chunk without doc_items attribute is included (not filtered)."""
    converter = _make_converter()

    # Create chunk where meta does not have doc_items
    no_items_chunk = MagicMock()
    # Remove the doc_items attribute so hasattr returns False
    del no_items_chunk.meta.doc_items
    no_items_chunk.meta.headings = ["Appendix"]
    no_items_chunk.meta.page_numbers = [99]

    converter._chunker.chunk.return_value = [no_items_chunk]
    converter._chunker.contextualize.return_value = "Appendix content"

    mock_doc = MagicMock()
    mock_doc.name = "ref.pdf"

    results = converter.chunk_document(mock_doc)

    assert len(results) == 1
    text, _ = results[0]
    assert "Appendix content" in text


@pytest.mark.unit
def test_chunk_document_mixed_labels():
    """Chunk with one excluded and one non-excluded label is kept (not all excluded)."""
    converter = _make_converter()

    mixed_chunk = _make_chunk(
        doc_items=[_make_doc_item(DOCUMENT_INDEX), _make_doc_item(TEXT)],
        headings=["TOC Section"],
        page_numbers=[2],
    )

    converter._chunker.chunk.return_value = [mixed_chunk]
    converter._chunker.contextualize.return_value = "Mixed content"

    mock_doc = MagicMock()
    mock_doc.name = "mixed.pdf"

    results = converter.chunk_document(mock_doc)

    # Not all labels are excluded (TEXT is not in EXCLUDED_LABELS), so chunk is kept
    assert len(results) == 1
    text, _ = results[0]
    assert "Mixed content" in text
