"""Tests for knowledge file ingestor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from meho_claude.core.knowledge.ingestor import ingest_file


class TestIngestFile:
    """Tests for ingest_file function."""

    def test_markdown_file_returns_chunks(self, tmp_path: Path):
        """ingest_file on .md file returns chunks from markdown content."""
        md_file = tmp_path / "doc.md"
        md_file.write_text("# Title\n\nSome content here.\n\n## Section\n\nMore content.")
        result = ingest_file(md_file)
        assert len(result) >= 1
        # All results should be Chunk objects
        assert all(hasattr(c, "content") for c in result)
        assert all(hasattr(c, "heading") for c in result)

    def test_html_file_converts_to_markdown_then_chunks(self, tmp_path: Path):
        """ingest_file on .html file converts to markdown then chunks."""
        html_file = tmp_path / "doc.html"
        html_file.write_text(
            "<html><body>"
            "<h1>Title</h1>"
            "<p>Some content here.</p>"
            "<h2>Section</h2>"
            "<p>More content.</p>"
            "</body></html>"
        )
        result = ingest_file(html_file)
        assert len(result) >= 1
        # Should have parsed the heading structure
        assert any("Title" in c.content for c in result)

    def test_pdf_file_converts_to_markdown_then_chunks(self, tmp_path: Path):
        """ingest_file on .pdf file converts to markdown then chunks (mock pymupdf4llm)."""
        pdf_file = tmp_path / "doc.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake pdf content")

        mock_markdown = "# PDF Title\n\nExtracted content from PDF.\n\n## Section\n\nMore PDF text."
        with patch("meho_claude.core.knowledge.ingestor.pymupdf4llm") as mock_pymupdf:
            mock_pymupdf.to_markdown.return_value = mock_markdown
            result = ingest_file(pdf_file)

        assert len(result) >= 1
        mock_pymupdf.to_markdown.assert_called_once_with(str(pdf_file))

    def test_unsupported_format_raises_value_error(self, tmp_path: Path):
        """ingest_file on .docx raises ValueError("Unsupported format")."""
        docx_file = tmp_path / "doc.docx"
        docx_file.write_bytes(b"fake docx content")
        with pytest.raises(ValueError, match="Unsupported format"):
            ingest_file(docx_file)

    def test_nonexistent_file_raises_file_not_found(self):
        """ingest_file on nonexistent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            ingest_file(Path("/nonexistent/file.md"))

    def test_htm_extension_treated_as_html(self, tmp_path: Path):
        """ingest_file on .htm file is treated as HTML."""
        htm_file = tmp_path / "doc.htm"
        htm_file.write_text("<h1>Title</h1><p>Content</p>")
        result = ingest_file(htm_file)
        assert len(result) >= 1
