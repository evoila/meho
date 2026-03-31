# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Docling-based document conversion and chunking.

Replaces pypdf + TextChunker with IBM Docling for structure-aware
document processing. Provides element-type classification (TOC filtering),
heading-aware chunking (HybridChunker), and heading path enrichment.
"""

import io
from dataclasses import dataclass
from typing import Any

from docling.chunking import HybridChunker
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import DocumentStream
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import DoclingDocument
from docling_core.types.doc.labels import DocItemLabel

from meho_app.core.otel import get_logger

logger = get_logger(__name__)

# MIME type to Docling InputFormat mapping
_MIME_TO_FORMAT: dict[str, InputFormat] = {
    "application/pdf": InputFormat.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": InputFormat.DOCX,
    "text/html": InputFormat.HTML,
}

SUPPORTED_MIME_TYPES: frozenset[str] = frozenset(_MIME_TO_FORMAT.keys())


def _get_pdf_page_count(file_bytes: bytes) -> int:
    """Get PDF page count using pypdfium2 (lightweight, header-only read)."""
    import pypdfium2

    pdf = pypdfium2.PdfDocument(io.BytesIO(file_bytes))
    count = len(pdf)
    pdf.close()
    return count


def _extract_pdf_page_range(file_bytes: bytes, start: int, end: int) -> bytes:
    """Extract a page range from a PDF into a new smaller PDF in memory.

    Uses pypdfium2 to create a new PDF containing only the specified pages,
    so Docling's backend never parses the full 8000-page document structure.

    Args:
        file_bytes: Full PDF content.
        start: First page (1-based inclusive).
        end: Last page (1-based inclusive).

    Returns:
        Bytes of the new PDF containing only the requested pages.
    """
    import pypdfium2

    src = pypdfium2.PdfDocument(io.BytesIO(file_bytes))
    dst = pypdfium2.PdfDocument.new()
    # pypdfium2 page indices are 0-based
    dst.import_pages(src, list(range(start - 1, end)))
    buf = io.BytesIO()
    dst.save(buf)
    dst.close()
    src.close()
    return buf.getvalue()


@dataclass
class TocEntry:
    """A single entry from a PDF's table of contents (bookmark tree)."""

    level: int  # Nesting depth (0 = top-level chapter)
    title: str  # Bookmark title
    page: int  # 1-based page number (0 if unresolvable)


def _extract_pdf_toc(file_bytes: bytes) -> list[TocEntry]:
    """Extract the PDF's bookmark/outline tree using pypdfium2.

    Reads only the PDF header — does not parse page content.
    Returns an empty list if the PDF has no bookmarks.
    """
    import pypdfium2

    pdf = pypdfium2.PdfDocument(io.BytesIO(file_bytes))
    entries: list[TocEntry] = []
    try:
        for bookmark in pdf.get_toc(max_depth=4):
            title = bookmark.get_title() or ""
            dest = bookmark.get_dest()
            page_idx = dest.get_index() if dest else None
            # page_idx is 0-based; convert to 1-based, or 0 if unresolvable
            page = (page_idx + 1) if page_idx is not None and page_idx >= 0 else 0
            entries.append(TocEntry(level=bookmark.level, title=title.strip(), page=page))
    finally:
        pdf.close()
    return entries


def _compute_chapter_batches(
    toc: list[TocEntry],
    total_pages: int,
    max_batch_size: int,
) -> list[tuple[int, int]]:
    """Compute page-range batches aligned to chapter/section boundaries.

    Algorithm:
    1. Use level-0 TOC entries (top-level chapters) as primary split points.
    2. If a chapter exceeds max_batch_size, sub-split at level-1 entries.
    3. If still too large, fall back to fixed-size splits within the chapter.
    4. If no TOC entries exist, fall back to fixed-size batches entirely.

    Returns:
        List of (start, end) tuples, 1-based inclusive page ranges.
    """
    # Filter to entries with valid page numbers, sorted by page
    valid = [e for e in toc if e.page > 0]
    if not valid:
        # No bookmarks — fall back to fixed-size batches
        return _fixed_batches(1, total_pages, max_batch_size)

    # Extract level-0 (top-level) chapter boundaries
    chapters = [e for e in valid if e.level == 0]
    if not chapters:
        # All entries are sub-levels; use all valid entries as split points
        chapters = valid

    # Deduplicate by page and sort
    seen_pages: set[int] = set()
    unique_chapters: list[TocEntry] = []
    for ch in sorted(chapters, key=lambda e: e.page):
        if ch.page not in seen_pages:
            seen_pages.add(ch.page)
            unique_chapters.append(ch)

    # Convert chapter starts to page ranges
    chapter_ranges: list[tuple[int, int]] = []
    for i, ch in enumerate(unique_chapters):
        start = ch.page
        end = unique_chapters[i + 1].page - 1 if i + 1 < len(unique_chapters) else total_pages
        if end < start:
            continue
        chapter_ranges.append((start, end))

    # Ensure we cover pages before the first chapter (e.g., cover, copyright)
    if chapter_ranges and chapter_ranges[0][0] > 1:
        chapter_ranges.insert(0, (1, chapter_ranges[0][0] - 1))

    # Split oversized chapters
    batches: list[tuple[int, int]] = []
    for ch_start, ch_end in chapter_ranges:
        ch_size = ch_end - ch_start + 1
        if ch_size <= max_batch_size:
            batches.append((ch_start, ch_end))
        else:
            # Try sub-splitting at level-1 entries within this chapter
            sub_entries = [e for e in valid if e.level == 1 and ch_start <= e.page <= ch_end]
            if sub_entries:
                sub_batches = _split_with_entries(ch_start, ch_end, sub_entries, max_batch_size)
                batches.extend(sub_batches)
            else:
                batches.extend(_fixed_batches(ch_start, ch_end, max_batch_size))

    return batches


def _split_with_entries(
    range_start: int,
    range_end: int,
    entries: list[TocEntry],
    max_batch_size: int,
) -> list[tuple[int, int]]:
    """Split a page range using TOC entries as preferred split points."""
    split_pages = sorted({e.page for e in entries if range_start < e.page <= range_end})
    # Build sub-ranges from split points
    sub_ranges: list[tuple[int, int]] = []
    prev = range_start
    for sp in split_pages:
        sub_ranges.append((prev, sp - 1))
        prev = sp
    sub_ranges.append((prev, range_end))
    # Filter empty ranges
    sub_ranges = [(s, e) for s, e in sub_ranges if e >= s]

    # Merge small adjacent sub-ranges and split oversized ones
    batches: list[tuple[int, int]] = []
    accum_start = sub_ranges[0][0] if sub_ranges else range_start
    accum_end = sub_ranges[0][0] - 1 if sub_ranges else range_start - 1

    for s, e in sub_ranges:
        proposed_size = e - accum_start + 1
        if proposed_size <= max_batch_size:
            accum_end = e
        else:
            # Flush accumulated range
            if accum_end >= accum_start:
                batches.append((accum_start, accum_end))
            # Check if this sub-range itself fits
            if e - s + 1 <= max_batch_size:
                accum_start, accum_end = s, e
            else:
                # Sub-range itself is oversized — fixed-split it
                batches.extend(_fixed_batches(s, e, max_batch_size))
                accum_start = e + 1
                accum_end = e

    if accum_end >= accum_start:
        batches.append((accum_start, accum_end))

    return batches


def _fixed_batches(start: int, end: int, batch_size: int) -> list[tuple[int, int]]:
    """Generate fixed-size page-range batches (fallback)."""
    batches: list[tuple[int, int]] = []
    for s in range(start, end + 1, batch_size):
        e = min(s + batch_size - 1, end)
        batches.append((s, e))
    return batches


def _get_heading_stack_at_page(toc: list[TocEntry], page: int) -> list[str]:
    """Compute the active heading hierarchy at a given page number.

    Walks the TOC in order, tracking the heading at each nesting level.
    When a heading is set at level N, deeper levels are cleared.

    Returns:
        Ordered list of heading titles, e.g. ["Chapter 5", "Section 5.3"].
    """
    stack: dict[int, str] = {}
    for entry in toc:
        if entry.page <= 0 or entry.page > page:
            # Skip unresolvable entries; stop once past target page
            if entry.page > page:
                break
            continue
        stack[entry.level] = entry.title
        # Clear deeper levels
        for deeper in [k for k in stack if k > entry.level]:
            del stack[deeper]
    return [stack[k] for k in sorted(stack)]


class DoclingDocumentConverter:
    """Unified document converter using IBM Docling.

    Handles PDF, DOCX, and HTML with element-type classification,
    TOC filtering, and structure-aware chunking via HybridChunker.

    For large PDFs, batching is handled externally by subprocess_converter.py
    which spawns a fresh subprocess per batch (required because PyTorch's
    C++ allocator never returns memory to the OS within a single process).
    """

    # Element types to exclude before chunking (D-03)
    EXCLUDED_LABELS: set[DocItemLabel] = {
        DocItemLabel.DOCUMENT_INDEX,  # Table of contents
        DocItemLabel.PAGE_HEADER,  # Repeated page headers
        DocItemLabel.PAGE_FOOTER,  # Repeated page footers
    }

    def __init__(
        self,
        max_tokens: int = 512,
        ocr_enabled: bool = False,
    ) -> None:
        self._max_tokens = max_tokens

        pdf_options = PdfPipelineOptions(
            do_ocr=ocr_enabled,
            do_table_structure=True,
            generate_parsed_pages=False,  # Free page data after processing
            images_scale=1.0,  # Default 2.0 = ~90 MB/page tensor; 1.0 = ~22 MB
            layout_batch_size=2,  # Reduce concurrent memory for layout model
            table_batch_size=2,  # Reduce concurrent memory for table model
        )

        self._converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.HTML],
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options),
            },
        )
        self._chunker = HybridChunker(
            max_tokens=max_tokens,
            merge_peers=True,
        )

    def convert_file(
        self, file_bytes: bytes, filename: str, mime_type: str
    ) -> DoclingDocument:
        """Convert file bytes to a DoclingDocument.

        Args:
            file_bytes: Raw file content.
            filename: Original filename (used for Docling metadata).
            mime_type: MIME type of the file.

        Returns:
            Parsed DoclingDocument with element-type labels.

        Raises:
            ValueError: If the MIME type is unsupported or conversion fails.
        """
        input_format = _MIME_TO_FORMAT.get(mime_type)
        if not input_format:
            raise ValueError(f"Unsupported MIME type: {mime_type}")

        try:
            stream = DocumentStream(name=filename, stream=io.BytesIO(file_bytes))
            result = self._converter.convert(stream)
            return result.document
        except Exception as e:
            logger.warning(
                "docling_conversion_failed",
                filename=filename,
                mime_type=mime_type,
                error=str(e),
            )
            raise ValueError(f"Failed to convert {filename}: {e}") from e

    def get_full_text(self, doc: DoclingDocument) -> str:
        """Export document as markdown text.

        Used for document summary generation (Plan 02 will call this).

        Args:
            doc: Parsed DoclingDocument.

        Returns:
            Full document text in markdown format.
        """
        return doc.export_to_markdown()

    def chunk_document(
        self, doc: DoclingDocument, chunk_prefix: str = ""
    ) -> list[tuple[str, dict[str, Any]]]:
        """Chunk a DoclingDocument with heading enrichment.

        For each chunk produced by HybridChunker:
        1. Calls contextualize() to prepend heading path to text (D-04).
        2. Optionally prepends chunk_prefix (connector context + summary) (D-05).
        3. Extracts heading stack, page numbers, and document name.

        Args:
            doc: Parsed DoclingDocument to chunk.
            chunk_prefix: Optional context prefix (connector type + summary)
                prepended to each chunk before embedding.

        Returns:
            List of (enriched_text, context_dict) tuples where context
            includes heading_stack, page_numbers, and document_name.
        """
        results: list[tuple[str, dict[str, Any]]] = []

        for chunk in self._chunker.chunk(dl_doc=doc):
            # D-03: Filter chunks originating from excluded element types
            # (TOC, page headers, page footers, page numbers).
            # Skip chunk if ALL its source doc_items have excluded labels.
            if hasattr(chunk.meta, "doc_items") and chunk.meta.doc_items:
                labels = {item.label for item in chunk.meta.doc_items if hasattr(item, "label")}
                if labels and labels.issubset(self.EXCLUDED_LABELS):
                    logger.debug(
                        "chunk_excluded_by_label",
                        labels=[l.value for l in labels],
                    )
                    continue

            # D-04, D-19: Heading path enrichment via contextualize
            enriched_text = self._chunker.contextualize(chunk)

            # D-05: Prepend connector context + document summary
            if chunk_prefix:
                enriched_text = chunk_prefix + "\n\n" + enriched_text

            # Build context metadata
            context: dict[str, Any] = {
                "heading_stack": (
                    list(chunk.meta.headings)
                    if hasattr(chunk.meta, "headings") and chunk.meta.headings
                    else []
                ),
                "page_numbers": (
                    list(chunk.meta.page_numbers)
                    if hasattr(chunk.meta, "page_numbers") and chunk.meta.page_numbers
                    else []
                ),
                "document_name": doc.name if hasattr(doc, "name") and doc.name else "",
            }
            results.append((enriched_text, context))

        return results


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

    from meho_app.core.config import get_config

    config = get_config()
    model_name = config.classifier_model  # defaults to anthropic:claude-sonnet-4-6

    agent = Agent(
        model_name,
        system_prompt=(
            "Summarize this document in 1-2 sentences. "
            "Focus on what systems, technologies, or procedures it covers. "
            "If a table of contents is present, use it to identify the major topics. "
            "Return ONLY the summary, no explanation."
        ),
    )

    # First 16K chars (~4 pages) to capture title, TOC, and introduction.
    # Large technical docs (e.g., VMware VCF 8000-page PDF) have cover +
    # copyright + legal in the first ~4K chars; the TOC with chapter titles
    # usually starts on page 3-5. 16K ensures we reach it.
    text_preview = document_text[:16000]

    try:
        result = await asyncio.wait_for(agent.run(text_preview), timeout=15.0)
        return str(result.output).strip()
    except (TimeoutError, Exception) as e:
        logger.warning("document_summary_generation_failed", error=str(e))
        return ""  # Fallback: no summary, still use connector context


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
