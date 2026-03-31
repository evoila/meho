# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Subprocess-isolated Docling document conversion (Phase 90.2).

Runs DoclingDocumentConverter.convert_file() in a child process via
multiprocessing.Process to prevent OOM from killing the uvicorn worker.
Uses JSON serialization (DoclingDocument.export_to_dict() / model_validate())
for cross-process data transfer.

For large PDFs, the batch loop runs in the parent (uvicorn) process and
spawns a fresh short-lived subprocess per batch. When each subprocess exits,
the OS reclaims ALL memory — including PyTorch's C++ allocator pools that
gc.collect() cannot free (proven: RSS grows ~2GB/convert() and never shrinks).
"""

import asyncio
import multiprocessing
import sys
import time
from collections.abc import Callable
from multiprocessing.connection import Connection

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


def _convert_in_subprocess(
    pipe: Connection,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    memory_limit_mb: int,
    ocr_enabled: bool = False,
) -> None:
    """Run Docling conversion in isolated subprocess.

    Converts a single document (or mini-PDF batch) and exits.
    On exit, the OS reclaims all memory including PyTorch C++ allocations.

    Args:
        pipe: Multiprocessing connection for sending results back.
        file_bytes: Raw file content to convert.
        filename: Original filename for Docling metadata.
        mime_type: MIME type of the file.
        memory_limit_mb: Memory limit in MB (Linux only, 0 to disable).
        ocr_enabled: Whether to enable OCR for scanned PDFs.
    """
    try:
        # Set memory limit (Linux only -- macOS RLIMIT_AS raises ValueError)
        if sys.platform == "linux" and memory_limit_mb > 0:
            import resource

            limit_bytes = memory_limit_mb * 1024 * 1024
            resource.setrlimit(
                resource.RLIMIT_AS,
                (limit_bytes, resource.RLIM_INFINITY),
            )

        # Import inside subprocess (required for 'spawn' start method on macOS/Python 3.12+)
        from meho_app.modules.knowledge.document_converter import (
            DoclingDocumentConverter,
        )

        converter = DoclingDocumentConverter(ocr_enabled=ocr_enabled)

        pipe.send({
            "type": "heartbeat",
            "message": f"Converting {filename}...",
        })

        start = time.monotonic()
        doc = converter.convert_file(file_bytes, filename, mime_type)
        elapsed = time.monotonic() - start

        # Serialize as JSON dict (not pickle) -- DoclingDocument is Pydantic v2
        doc_dict = doc.export_to_dict()

        pipe.send({
            "type": "result",
            "doc_dict": doc_dict,
            "elapsed_seconds": elapsed,
        })
    except Exception as e:
        pipe.send({
            "type": "error",
            "error": str(e),
            "error_type": type(e).__name__,
        })
    finally:
        pipe.close()


async def convert_file_in_subprocess(
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    memory_limit_mb: int = 8192,
    ocr_enabled: bool = False,
    timeout_seconds: int = 600,
    on_heartbeat: Callable[[str], None] | None = None,
) -> "DoclingDocument":  # noqa: F821
    """Run Docling conversion in a single short-lived subprocess.

    Spawns a child process, waits for results via multiprocessing.Pipe,
    and deserializes the DoclingDocument from JSON dict. When the subprocess
    exits, the OS reclaims all memory.

    Args:
        file_bytes: Raw file content to convert.
        filename: Original filename for Docling metadata.
        mime_type: MIME type of the file.
        memory_limit_mb: Memory limit in MB (Linux only, 0 to disable).
        ocr_enabled: Whether to enable OCR for scanned PDFs.
        timeout_seconds: Maximum time to wait for conversion.
        on_heartbeat: Optional callback for progress messages from subprocess.

    Returns:
        Deserialized DoclingDocument from subprocess result.

    Raises:
        ValueError: On conversion failure, OOM, or timeout.
    """
    parent_conn, child_conn = multiprocessing.Pipe()

    process = multiprocessing.Process(
        target=_convert_in_subprocess,
        args=(
            child_conn, file_bytes, filename, mime_type,
            memory_limit_mb, ocr_enabled,
        ),
    )
    process.start()
    child_conn.close()

    def _wait_for_result() -> tuple[dict | None, int | None]:
        """Blocking function to run in thread via run_in_executor."""
        result = None
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            if parent_conn.poll(timeout=5.0):
                msg = parent_conn.recv()
                if msg["type"] == "heartbeat":
                    logger.info(
                        "subprocess_heartbeat",
                        filename=filename,
                        heartbeat_msg=msg["message"],
                    )
                    if on_heartbeat is not None:
                        on_heartbeat(msg["message"])
                    continue
                elif msg["type"] in ("result", "error"):
                    result = msg
                    break

            if not process.is_alive():
                break

        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join()

        return result, process.exitcode

    loop = asyncio.get_event_loop()
    result, exitcode = await loop.run_in_executor(None, _wait_for_result)
    parent_conn.close()

    if result is not None and result["type"] == "result":
        from docling_core.types.doc import DoclingDocument

        logger.info(
            "subprocess_conversion_complete",
            filename=filename,
            elapsed_seconds=result.get("elapsed_seconds"),
        )
        return DoclingDocument.model_validate(result["doc_dict"])

    if result is not None and result["type"] == "error":
        raise ValueError(f"Document conversion failed: {result['error']}")

    raise ValueError(
        f"Document conversion process terminated unexpectedly (exit code: {exitcode}). "
        f"The document may require more memory than the {memory_limit_mb}MB limit allows. "
        "Increase MEHO_INGESTION_MEMORY_LIMIT_MB or reduce document size."
    )


async def convert_pdf_batched_in_subprocesses(
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    memory_limit_mb: int = 8192,
    page_batch_size: int = 50,
    ocr_enabled: bool = False,
    timeout_seconds_per_batch: int = 600,
    on_heartbeat: Callable[[str], None] | None = None,
) -> "DoclingDocument":  # noqa: F821
    """Convert a large PDF in chapter-aware batches, one subprocess per batch.

    Each batch spawns a fresh subprocess that loads PyTorch + Docling models,
    converts its mini-PDF, sends back the result, and exits. On exit the OS
    reclaims ALL memory — including PyTorch's C++ allocator pools that
    gc.collect() cannot free.

    The batch loop runs in the parent (uvicorn) process using only lightweight
    libraries (pypdfium2 for PDF splitting, no PyTorch).

    Args:
        file_bytes: Full PDF content.
        filename: Original filename for Docling metadata.
        mime_type: MIME type (must be application/pdf).
        memory_limit_mb: Memory limit per subprocess (Linux only, 0 to disable).
        page_batch_size: Max pages per batch.
        ocr_enabled: Whether to enable OCR for scanned PDFs.
        timeout_seconds_per_batch: Timeout for each individual batch subprocess.
        on_heartbeat: Optional callback for progress messages.

    Returns:
        Merged DoclingDocument from all batches.
    """
    from docling_core.types.doc import DoclingDocument

    from meho_app.modules.knowledge.document_converter import (
        _compute_chapter_batches,
        _extract_pdf_page_range,
        _extract_pdf_toc,
        _get_heading_stack_at_page,
        _get_pdf_page_count,
    )

    total_pages = _get_pdf_page_count(file_bytes)
    toc = _extract_pdf_toc(file_bytes)
    batches = _compute_chapter_batches(toc, total_pages, page_batch_size)

    logger.info(
        "pdf_batched_subprocess_starting",
        filename=filename,
        total_pages=total_pages,
        num_batches=len(batches),
        has_toc=len(toc) > 0,
    )

    batch_docs: list[DoclingDocument] = []

    for i, (start, end) in enumerate(batches):
        heading_stack = _get_heading_stack_at_page(toc, start)
        context = " > ".join(heading_stack) if heading_stack else f"pages {start}-{end}"
        msg = (
            f"Batch {i + 1}/{len(batches)}: "
            f"{context} (pages {start}-{end} of {total_pages})"
        )

        logger.info(
            "pdf_batch_subprocess_spawning",
            batch=i + 1,
            total_batches=len(batches),
            start=start,
            end=end,
            heading=context,
        )
        if on_heartbeat:
            on_heartbeat(msg)

        batch_bytes = _extract_pdf_page_range(file_bytes, start, end)
        batch_filename = f"{filename}[{start}-{end}]"

        doc = await convert_file_in_subprocess(
            file_bytes=batch_bytes,
            filename=batch_filename,
            mime_type=mime_type,
            memory_limit_mb=memory_limit_mb,
            ocr_enabled=ocr_enabled,
            timeout_seconds=timeout_seconds_per_batch,
            on_heartbeat=on_heartbeat,
        )
        batch_docs.append(doc)

    if len(batch_docs) == 1:
        return batch_docs[0]

    logger.info("pdf_batch_concatenating", num_batches=len(batch_docs))
    return DoclingDocument.concatenate(batch_docs)
