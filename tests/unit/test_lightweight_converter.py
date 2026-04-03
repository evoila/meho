# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for LightweightDocumentConverter.

Tests cover all three format handlers (PDF, DOCX, HTML), table-to-markdown
conversion, chunking output shape, and the shared utility functions.
External libraries (pymupdf4llm, pdfplumber, python-docx, bs4) are mocked
where necessary to avoid hard dependencies in test environment.
"""

from __future__ import annotations

import io
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Stub production-only dependencies before importing lightweight_converter so
# @patch decorators can resolve the modules. These may not be installed in test env.
for _mod in (
    "pymupdf4llm",
    "pdfplumber",
    "docx",
    "docx.opc",
    "docx.opc.constants",
    "rapidocr_onnxruntime",
):
    sys.modules.setdefault(_mod, MagicMock())

from meho_app.modules.knowledge.lightweight_converter import (
    SUPPORTED_MIME_TYPES,
    LightweightDocument,
    LightweightDocumentConverter,
    _table_to_markdown,
    build_chunk_prefix,
)


# ---------------------------------------------------------------------------
# _table_to_markdown helper
# ---------------------------------------------------------------------------


class TestTableToMarkdown:
    """Tests for the _table_to_markdown helper function."""

    def test_basic_table(self) -> None:
        table: list[list[str | None]] = [
            ["Name", "Age"],
            ["Alice", "30"],
            ["Bob", "25"],
        ]
        result = _table_to_markdown(table)
        assert "| Name | Age |" in result
        assert "| --- | --- |" in result
        assert "| Alice | 30 |" in result
        assert "| Bob | 25 |" in result

    def test_none_values_replaced(self) -> None:
        table: list[list[str | None]] = [[" A ", None], [None, " B "]]
        result = _table_to_markdown(table)
        # None -> "" and values are stripped
        assert "| A |  |" in result
        assert "|  | B |" in result

    def test_empty_table(self) -> None:
        result = _table_to_markdown([])
        assert result == ""

    def test_single_row_header_only(self) -> None:
        table: list[list[str | None]] = [["Col1", "Col2"]]
        result = _table_to_markdown(table)
        assert "| Col1 | Col2 |" in result
        assert "| --- | --- |" in result
        # No data rows after separator
        lines = result.strip().split("\n")
        assert len(lines) == 2  # header + separator, no data rows

    def test_row_padding(self) -> None:
        """Rows with fewer columns than the header are padded."""
        table: list[list[str | None]] = [["A", "B", "C"], ["1"]]
        result = _table_to_markdown(table)
        # The short row should be padded to 3 columns
        assert "| 1 |  |  |" in result


# ---------------------------------------------------------------------------
# SUPPORTED_MIME_TYPES
# ---------------------------------------------------------------------------


def test_supported_mime_types() -> None:
    assert "application/pdf" in SUPPORTED_MIME_TYPES
    assert (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        in SUPPORTED_MIME_TYPES
    )
    assert "text/html" in SUPPORTED_MIME_TYPES
    assert len(SUPPORTED_MIME_TYPES) == 3


# ---------------------------------------------------------------------------
# build_chunk_prefix
# ---------------------------------------------------------------------------


class TestBuildChunkPrefix:
    """Tests for the build_chunk_prefix utility."""

    def test_full_prefix(self) -> None:
        result = build_chunk_prefix("kubernetes", "prod-cluster", "K8s deployment docs")
        assert result == "kubernetes connector (prod-cluster). K8s deployment docs"

    def test_connector_type_only(self) -> None:
        result = build_chunk_prefix("vmware", None, "Host inventory")
        assert result == "vmware connector. Host inventory"

    def test_no_connector(self) -> None:
        result = build_chunk_prefix(None, None, "Some summary")
        assert result == "Some summary"

    def test_empty(self) -> None:
        result = build_chunk_prefix(None, None, "")
        assert result == ""

    def test_connector_no_summary(self) -> None:
        result = build_chunk_prefix("aws", "us-east-1", "")
        assert result == "aws connector (us-east-1)."


# ---------------------------------------------------------------------------
# LightweightDocumentConverter.convert_file dispatch
# ---------------------------------------------------------------------------


class TestConvertFile:
    """Tests for convert_file dispatching to the correct handler."""

    def test_unsupported_mime_type_raises(self) -> None:
        converter = LightweightDocumentConverter()
        with pytest.raises(ValueError, match="Unsupported MIME type"):
            converter.convert_file(b"data", "file.xyz", "application/octet-stream")

    @patch(
        "meho_app.modules.knowledge.lightweight_converter.LightweightDocumentConverter._convert_pdf"
    )
    def test_pdf_dispatch(self, mock_pdf: MagicMock) -> None:
        mock_pdf.return_value = LightweightDocument(
            markdown="# Test", name="test.pdf", page_count=1, pages=[]
        )
        converter = LightweightDocumentConverter()
        result = converter.convert_file(b"%PDF-", "test.pdf", "application/pdf")
        mock_pdf.assert_called_once_with(b"%PDF-", "test.pdf")
        assert result.name == "test.pdf"

    @patch(
        "meho_app.modules.knowledge.lightweight_converter.LightweightDocumentConverter._convert_docx"
    )
    def test_docx_dispatch(self, mock_docx: MagicMock) -> None:
        mock_docx.return_value = LightweightDocument(
            markdown="# Doc", name="test.docx", page_count=0, pages=[]
        )
        converter = LightweightDocumentConverter()
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        result = converter.convert_file(b"PK", "test.docx", mime)
        mock_docx.assert_called_once_with(b"PK", "test.docx")
        assert result.name == "test.docx"

    @patch(
        "meho_app.modules.knowledge.lightweight_converter.LightweightDocumentConverter._convert_html"
    )
    def test_html_dispatch(self, mock_html: MagicMock) -> None:
        mock_html.return_value = LightweightDocument(
            markdown="# Page", name="test.html", page_count=0, pages=[]
        )
        converter = LightweightDocumentConverter()
        result = converter.convert_file(b"<html>", "test.html", "text/html")
        mock_html.assert_called_once_with(b"<html>", "test.html")
        assert result.name == "test.html"


# ---------------------------------------------------------------------------
# LightweightDocumentConverter.get_full_text
# ---------------------------------------------------------------------------


class TestGetFullText:
    """Tests for get_full_text returning the markdown field."""

    def test_returns_markdown(self) -> None:
        converter = LightweightDocumentConverter()
        doc = LightweightDocument(
            markdown="# Hello\n\nWorld", name="test.pdf", page_count=1, pages=[]
        )
        assert converter.get_full_text(doc) == "# Hello\n\nWorld"

    def test_empty_markdown(self) -> None:
        converter = LightweightDocumentConverter()
        doc = LightweightDocument(markdown="", name="empty.pdf", page_count=0, pages=[])
        assert converter.get_full_text(doc) == ""


# ---------------------------------------------------------------------------
# LightweightDocumentConverter.chunk_document
# ---------------------------------------------------------------------------


class TestChunkDocument:
    """Tests for chunk_document output shape and prefix handling."""

    def test_output_shape_matches_docling_contract(self) -> None:
        """chunk_document returns list[tuple[str, dict]] with heading_stack,
        page_numbers, and document_name keys -- matching DoclingDocumentConverter."""
        converter = LightweightDocumentConverter()
        doc = LightweightDocument(
            markdown=(
                "# Chapter 1\n\n"
                "Some text here that is long enough to be a chunk.\n\n"
                "## Section 1.1\n\n"
                "More text in section one point one."
            ),
            name="test.pdf",
            page_count=1,
            pages=[],
        )
        chunks = converter.chunk_document(doc)
        assert isinstance(chunks, list)
        assert len(chunks) > 0
        for text, ctx in chunks:
            assert isinstance(text, str)
            assert isinstance(ctx, dict)
            assert "heading_stack" in ctx
            assert "document_name" in ctx
            assert "page_numbers" in ctx
            assert isinstance(ctx["heading_stack"], list)
            assert isinstance(ctx["page_numbers"], list)
            assert ctx["document_name"] == "test.pdf"

    def test_chunk_prefix_prepended(self) -> None:
        converter = LightweightDocumentConverter()
        doc = LightweightDocument(
            markdown="Some content for chunking.", name="test.pdf", page_count=1, pages=[]
        )
        chunks = converter.chunk_document(doc, chunk_prefix="kubernetes connector (prod).")
        assert len(chunks) > 0
        text, _ = chunks[0]
        assert text.startswith("kubernetes connector (prod).")

    def test_empty_document(self) -> None:
        converter = LightweightDocumentConverter()
        doc = LightweightDocument(markdown="", name="empty.pdf", page_count=0, pages=[])
        chunks = converter.chunk_document(doc)
        assert chunks == []

    def test_no_prefix_when_empty_string(self) -> None:
        converter = LightweightDocumentConverter()
        doc = LightweightDocument(
            markdown="Some text to chunk.", name="test.pdf", page_count=1, pages=[]
        )
        chunks = converter.chunk_document(doc, chunk_prefix="")
        assert len(chunks) > 0
        text, _ = chunks[0]
        # Should NOT start with double newline from empty prefix
        assert not text.startswith("\n\n")


# ---------------------------------------------------------------------------
# PDF conversion (_convert_pdf)
# ---------------------------------------------------------------------------


class TestConvertPdf:
    """Tests for _convert_pdf with mocked pymupdf4llm and pdfplumber."""

    @patch(
        "meho_app.modules.knowledge.lightweight_converter.LightweightDocumentConverter._extract_tables_pdfplumber"
    )
    @patch("pymupdf4llm.to_markdown")
    def test_pdf_produces_markdown_with_headings(
        self, mock_pymupdf: MagicMock, mock_tables: MagicMock
    ) -> None:
        # pymupdf4llm.to_markdown with page_chunks=True returns list of dicts
        mock_pymupdf.return_value = [
            {"metadata": {"page": 0}, "text": "# Introduction\n\nWelcome to the guide."},
            {"metadata": {"page": 1}, "text": "## Chapter 1\n\nSome content here."},
        ]
        # No tables
        mock_tables.return_value = {}

        converter = LightweightDocumentConverter()
        doc = converter._convert_pdf(b"%PDF-fake", "test.pdf")

        assert isinstance(doc, LightweightDocument)
        assert "# Introduction" in doc.markdown
        assert "## Chapter 1" in doc.markdown
        assert doc.name == "test.pdf"
        assert doc.page_count == 2
        assert len(doc.pages) == 2

    @patch(
        "meho_app.modules.knowledge.lightweight_converter.LightweightDocumentConverter._extract_tables_pdfplumber"
    )
    @patch("pymupdf4llm.to_markdown")
    def test_pdf_merges_pdfplumber_tables(
        self, mock_pymupdf: MagicMock, mock_tables: MagicMock
    ) -> None:
        mock_pymupdf.return_value = [
            {"metadata": {"page": 0}, "text": "# Data\n\nSee table below."},
        ]
        # Return a table for page 0
        mock_tables.return_value = {0: [[["Name", "Value"], ["CPU", "4 cores"], ["RAM", "16GB"]]]}

        converter = LightweightDocumentConverter()
        doc = converter._convert_pdf(b"%PDF-fake", "test.pdf")

        assert "| Name | Value |" in doc.markdown
        assert "| CPU | 4 cores |" in doc.markdown
        assert "| RAM | 16GB |" in doc.markdown

    @patch(
        "meho_app.modules.knowledge.lightweight_converter.LightweightDocumentConverter._extract_tables_pdfplumber"
    )
    @patch("pymupdf4llm.to_markdown")
    def test_pdf_page_data_populated(self, mock_pymupdf: MagicMock, mock_tables: MagicMock) -> None:
        mock_pymupdf.return_value = [
            {"metadata": {"page": 0}, "text": "Page one content."},
            {"metadata": {"page": 1}, "text": "Page two content."},
        ]
        mock_tables.return_value = {}

        converter = LightweightDocumentConverter()
        doc = converter._convert_pdf(b"%PDF-fake", "report.pdf")

        assert doc.page_count == 2
        assert doc.pages[0].page_number == 1
        assert doc.pages[1].page_number == 2
        assert "Page one content" in doc.pages[0].text
        assert "Page two content" in doc.pages[1].text


# ---------------------------------------------------------------------------
# HTML conversion (_convert_html)
# ---------------------------------------------------------------------------


class TestConvertHtml:
    """Tests for _convert_html using real BeautifulSoup (lightweight dep)."""

    def test_html_produces_markdown_headings(self) -> None:
        html = (
            b"<html><body>"
            b"<h1>Title</h1><p>Some text.</p>"
            b"<h2>Section</h2><p>More text.</p>"
            b"</body></html>"
        )
        converter = LightweightDocumentConverter()
        doc = converter._convert_html(html, "test.html")
        assert "# Title" in doc.markdown
        assert "## Section" in doc.markdown
        assert "Some text." in doc.markdown
        assert "More text." in doc.markdown

    def test_html_strips_script_tags(self) -> None:
        html = (
            b"<html><body>"
            b"<script>alert('xss')</script>"
            b"<h1>Clean</h1>"
            b"<style>.x{color:red}</style>"
            b"<p>Content</p>"
            b"</body></html>"
        )
        converter = LightweightDocumentConverter()
        doc = converter._convert_html(html, "test.html")
        assert "alert" not in doc.markdown
        assert ".x{" not in doc.markdown
        assert "# Clean" in doc.markdown
        assert "Content" in doc.markdown

    def test_html_extracts_tables(self) -> None:
        html = (
            b"<html><body>"
            b"<table><tr><th>Key</th><th>Value</th></tr>"
            b"<tr><td>CPU</td><td>8</td></tr></table>"
            b"</body></html>"
        )
        converter = LightweightDocumentConverter()
        doc = converter._convert_html(html, "test.html")
        assert "| Key | Value |" in doc.markdown
        assert "| CPU | 8 |" in doc.markdown

    def test_html_handles_lists(self) -> None:
        html = (
            b"<html><body>"
            b"<ul><li>Item 1</li><li>Item 2</li></ul>"
            b"<ol><li>First</li><li>Second</li></ol>"
            b"</body></html>"
        )
        converter = LightweightDocumentConverter()
        doc = converter._convert_html(html, "test.html")
        assert "- Item 1" in doc.markdown
        assert "- Item 2" in doc.markdown
        assert "1. First" in doc.markdown
        assert "2. Second" in doc.markdown

    def test_html_document_metadata(self) -> None:
        html = b"<html><body><p>Hello</p></body></html>"
        converter = LightweightDocumentConverter()
        doc = converter._convert_html(html, "page.html")
        assert doc.name == "page.html"
        assert doc.page_count == 0
        assert doc.pages == []


# ---------------------------------------------------------------------------
# DOCX conversion (_convert_docx) - requires mocking python-docx
# ---------------------------------------------------------------------------


class TestConvertDocx:
    """Tests for _convert_docx with mocked python-docx."""

    def _make_mock_paragraph(self, style_name: str, text: str) -> MagicMock:
        """Create a mock python-docx paragraph."""
        para = MagicMock()
        para.style.name = style_name
        para.text = text
        para._element = MagicMock()
        return para

    def _make_mock_table(self, rows_data: list[list[str]]) -> MagicMock:
        """Create a mock python-docx table."""
        table = MagicMock()
        table._element = MagicMock()
        mock_rows = []
        for row_data in rows_data:
            row = MagicMock()
            cells = []
            for cell_text in row_data:
                cell = MagicMock()
                cell.text = cell_text
                cells.append(cell)
            row.cells = cells
            mock_rows.append(row)
        table.rows = mock_rows
        return table

    @patch("docx.Document")
    def test_docx_produces_markdown_headings(self, mock_doc_cls: MagicMock) -> None:
        # Create mock paragraphs
        para1 = self._make_mock_paragraph("Heading 1", "My Document Title")
        para2 = self._make_mock_paragraph("Normal", "Some body text here.")
        para3 = self._make_mock_paragraph("Heading 2", "Section One")

        # Build the mock document
        mock_doc = MagicMock()
        mock_doc.paragraphs = [para1, para2, para3]
        mock_doc.tables = []

        # Set up element.body children in document order
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        para1._element.tag = f"{ns}p"
        para2._element.tag = f"{ns}p"
        para3._element.tag = f"{ns}p"
        mock_doc.element.body = [para1._element, para2._element, para3._element]

        mock_doc_cls.return_value = mock_doc

        converter = LightweightDocumentConverter()
        doc = converter._convert_docx(b"PK\x03\x04fake", "test.docx")

        assert "# My Document Title" in doc.markdown
        assert "## Section One" in doc.markdown
        assert "Some body text here." in doc.markdown
        assert doc.name == "test.docx"
        assert doc.page_count == 0

    @patch("docx.Document")
    def test_docx_handles_title_style(self, mock_doc_cls: MagicMock) -> None:
        para = self._make_mock_paragraph("Title", "Report Title")

        mock_doc = MagicMock()
        mock_doc.paragraphs = [para]
        mock_doc.tables = []

        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        para._element.tag = f"{ns}p"
        mock_doc.element.body = [para._element]

        mock_doc_cls.return_value = mock_doc

        converter = LightweightDocumentConverter()
        doc = converter._convert_docx(b"PK", "report.docx")

        assert "# Report Title" in doc.markdown

    @patch("docx.Document")
    def test_docx_handles_tables(self, mock_doc_cls: MagicMock) -> None:
        table = self._make_mock_table([["Col A", "Col B"], ["1", "2"]])

        mock_doc = MagicMock()
        mock_doc.paragraphs = []
        mock_doc.tables = [table]

        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        table._element.tag = f"{ns}tbl"
        mock_doc.element.body = [table._element]

        mock_doc_cls.return_value = mock_doc

        converter = LightweightDocumentConverter()
        doc = converter._convert_docx(b"PK", "data.docx")

        assert "| Col A | Col B |" in doc.markdown
        assert "| 1 | 2 |" in doc.markdown

    @patch("docx.Document")
    def test_docx_handles_list_styles(self, mock_doc_cls: MagicMock) -> None:
        para_bullet = self._make_mock_paragraph("List Bullet", "Bullet item")
        para_number = self._make_mock_paragraph("List Number", "Numbered item")

        mock_doc = MagicMock()
        mock_doc.paragraphs = [para_bullet, para_number]
        mock_doc.tables = []

        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        para_bullet._element.tag = f"{ns}p"
        para_number._element.tag = f"{ns}p"
        mock_doc.element.body = [para_bullet._element, para_number._element]

        mock_doc_cls.return_value = mock_doc

        converter = LightweightDocumentConverter()
        doc = converter._convert_docx(b"PK", "list.docx")

        assert "- Bullet item" in doc.markdown
        assert "1. Numbered item" in doc.markdown
