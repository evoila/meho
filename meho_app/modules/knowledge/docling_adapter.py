# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Adapter bridging DoclingWrapper into MEHO's ingestion pipeline.

Translates between DoclingWrapper's standalone API (file-path-based, synchronous,
HierarchicalChunker) and MEHO's converter interface (bytes-based, async-friendly,
(text, context_dict) chunk tuples consumed by MetadataExtractor).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.knowledge.docling_wrapper import (
    ConversionResult,
    DoclingWrapper,
    ProgressEvent,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

SUPPORTED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/html",
    }
)

_MIME_TO_EXTENSION: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/html": ".html",
}


class DoclingWrapperAdapter:
    """Bridges DoclingWrapper into MEHO's ingestion pipeline.

    Provides the same 3-method interface as the old DoclingDocumentConverter
    (convert_file, get_full_text, chunk_document) so callers in ingestion.py
    can use this as a drop-in replacement.

    The wrapper handles subprocess isolation, PDF page-splitting, progress
    reporting, and RSS monitoring internally. This adapter only translates
    data formats and manages temp files.
    """

    def __init__(
        self,
        *,
        ocr_enabled: bool = False,
        table_structure: bool = True,
        device: str = "auto",
        num_threads: int = 4,
        pdf_chunk_pages: int = 100,
        max_workers: int = 4,
        worker_timeout_s: float | None = 3600.0,
        worker_idle_timeout_s: float | None = 300.0,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        self._wrapper = DoclingWrapper(
            ocr=ocr_enabled,
            table_structure=table_structure,
            device=device,
            num_threads=num_threads,
            pdf_chunk_pages=pdf_chunk_pages,
            max_workers=max_workers,
            worker_timeout_s=worker_timeout_s,
            worker_idle_timeout_s=worker_idle_timeout_s,
            chunking=True,
            on_progress=on_progress,
        )

    def convert_file(self, file_bytes: bytes, filename: str, mime_type: str) -> ConversionResult:
        """Convert file bytes to a ConversionResult via DoclingWrapper.

        Writes bytes to a temp file (the wrapper needs a filesystem path),
        calls wrapper.parse(), and cleans up.

        Args:
            file_bytes: Raw file content.
            filename: Original filename (used for extension detection).
            mime_type: MIME type of the file.

        Returns:
            ConversionResult with markdown, chunks, and performance metrics.

        Raises:
            ValueError: If the MIME type is unsupported or conversion fails.
        """
        ext = _MIME_TO_EXTENSION.get(mime_type)
        if ext is None:
            raise ValueError(f"Unsupported MIME type: {mime_type}")

        fd = -1
        tmp_path = ""
        try:
            suffix = ext
            if filename:
                _, file_ext = os.path.splitext(filename)
                if file_ext:
                    suffix = file_ext

            fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            with os.fdopen(fd, "wb") as fh:
                fh.write(file_bytes)
            fd = -1

            result = self._wrapper.parse(tmp_path)
            return result

        except Exception as e:
            logger.warning(
                "docling_wrapper_conversion_failed",
                filename=filename,
                mime_type=mime_type,
                error=str(e),
            )
            raise ValueError(f"Failed to convert {filename}: {e}") from e
        finally:
            if fd >= 0:
                os.close(fd)
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    async def convert_file_async(
        self, file_bytes: bytes, filename: str, mime_type: str
    ) -> ConversionResult:
        """Async wrapper around convert_file.

        DoclingWrapper.parse() is synchronous (manages its own threads and
        subprocesses internally). This runs it in a thread executor so it
        does not block the async event loop.
        """
        return await asyncio.to_thread(self.convert_file, file_bytes, filename, mime_type)

    def get_full_text(self, result: ConversionResult) -> str:
        """Export document as markdown text.

        Args:
            result: ConversionResult from convert_file.

        Returns:
            Full document text in markdown format.
        """
        return result.markdown

    def chunk_document(
        self, result: ConversionResult, chunk_prefix: str = ""
    ) -> list[tuple[str, dict[str, Any]]]:
        """Translate wrapper Chunks into MEHO's (text, context_dict) format.

        The wrapper's HierarchicalChunker splits on document structure and
        each Chunk carries full provenance:
          - chunk.headings   -> context["heading_stack"]
          - chunk.page_numbers -> context["page_numbers"]
          - source filename  -> context["document_name"]

        MetadataExtractor.extract_metadata() then derives:
          - chapter          = heading_stack[0]
          - section          = heading_stack[1]
          - heading_hierarchy = full heading_stack
        All other metadata (content_type, keywords, entities, etc.) is
        extracted from the chunk text directly.

        Args:
            result: ConversionResult from convert_file (must have chunks).
            chunk_prefix: Deprecated enrichment hook retained for compatibility.
                Retrieval context is now built from metadata instead of mutating
                the stored chunk body.

        Returns:
            List of (raw_chunk_text, context_dict) tuples.
        """
        results: list[tuple[str, dict[str, Any]]] = []
        source_name = result.source.name if result.source else ""

        for chunk in result.chunks:
            if not chunk.text or not chunk.text.strip():
                continue

            text = chunk.text

            context: dict[str, Any] = {
                "heading_stack": list(chunk.headings) if chunk.headings else [],
                "page_numbers": list(chunk.page_numbers) if chunk.page_numbers else [],
                "document_name": source_name,
            }

            results.append((text, context))

        return results

    @property
    def on_progress(self) -> Callable[[ProgressEvent], None] | None:
        """Get the current progress callback."""
        return self._wrapper.on_progress

    @on_progress.setter
    def on_progress(self, callback: Callable[[ProgressEvent], None] | None) -> None:
        """Set the progress callback for the underlying wrapper."""
        self._wrapper.on_progress = callback
