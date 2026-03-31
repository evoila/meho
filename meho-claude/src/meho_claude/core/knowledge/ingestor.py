"""File parsing pipeline for knowledge ingestion.

Converts PDF, HTML, and Markdown files into markdown, then applies
heading-aware chunking. All formats pass through a uniform pipeline:
  input file -> markdown text -> list[Chunk]
"""

from __future__ import annotations

from pathlib import Path

from meho_claude.core.knowledge.chunker import Chunk, chunk_markdown

# Lazy-loaded modules (kept at module level after first import for mocking)
pymupdf4llm = None  # type: ignore[assignment]


def ingest_file(file_path: Path) -> list[Chunk]:
    """Parse a file and return heading-aware chunks.

    Dispatches to the appropriate converter based on file extension:
      .pdf  -> pymupdf4llm.to_markdown -> chunk_markdown
      .html/.htm -> markdownify -> chunk_markdown
      .md/.markdown -> read text -> chunk_markdown

    Args:
        file_path: Path to the file to ingest.

    Returns:
        List of Chunk objects from the file content.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file format is not supported.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        markdown = _pdf_to_markdown(file_path)
    elif suffix in (".html", ".htm"):
        markdown = _html_to_markdown(file_path)
    elif suffix in (".md", ".markdown"):
        markdown = file_path.read_text(encoding="utf-8")
    else:
        raise ValueError(f"Unsupported format: {suffix}")

    return chunk_markdown(markdown)


def _pdf_to_markdown(file_path: Path) -> str:
    """Convert a PDF file to markdown using pymupdf4llm.

    Args:
        file_path: Path to the PDF file.

    Returns:
        Markdown text extracted from the PDF.
    """
    global pymupdf4llm  # noqa: PLW0603
    if pymupdf4llm is None:
        import pymupdf4llm as _pymupdf4llm

        pymupdf4llm = _pymupdf4llm
    return pymupdf4llm.to_markdown(str(file_path))


def _html_to_markdown(file_path: Path) -> str:
    """Convert an HTML file to markdown using markdownify.

    Args:
        file_path: Path to the HTML file.

    Returns:
        Markdown text converted from the HTML.
    """
    from markdownify import markdownify as md

    html = file_path.read_text(encoding="utf-8")
    return md(html, heading_style="ATX")
