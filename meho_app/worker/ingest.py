# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Ephemeral ingestion worker pipeline.

Stateless worker that processes a single document end-to-end:
download -> convert (Docling) -> chunk -> embed -> serialize (Arrow IPC) -> upload -> exit.

Invoked as ``python -m meho_app.worker`` by all container-based backends.
Process termination reclaims all Docling/PyTorch memory (the whole point
of the ephemeral architecture -- see CONTEXT.md for measured evidence).

Environment variables:
    WORKER_JOB_ID (required): Unique job identifier from MEHO knowledge module.
    WORKER_INPUT_URL (required): Signed URL or file:// path to source document.
    WORKER_OUTPUT_URL (required): Signed URL or file:// path for Arrow IPC output.
    VOYAGE_API_KEY (optional): If set, generate Voyage AI embeddings. Otherwise zero vectors.
    WORKER_CHUNK_PREFIX (optional): Context prefix for each chunk (connector + summary).
    WORKER_OCR_ENABLED (optional): Enable OCR for scanned PDFs (default "false").
    WORKER_PAGE_BATCH_SIZE (optional): Max pages per batch for large PDFs (default "50").
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import pathlib
from typing import Any

import httpx

# Conditional import: use lightweight converter when MEHO_FEATURE_USE_DOCLING=false.
# Worker reads env vars directly (no app config stack).
_use_docling = os.environ.get("MEHO_FEATURE_USE_DOCLING", "true").lower() != "false"

if _use_docling:
    # Imports that tests patch at this module's namespace.
    # These are the patchable names: meho_app.worker.ingest.DoclingDocumentConverter, etc.
    from meho_app.modules.knowledge.document_converter import (
        DoclingDocumentConverter,
        _compute_chapter_batches,
        _extract_pdf_page_range,
        _extract_pdf_toc,
        _get_pdf_page_count,
    )
else:
    from meho_app.modules.knowledge.lightweight_converter import (  # type: ignore[assignment]
        LightweightDocumentConverter as DoclingDocumentConverter,
    )
    from meho_app.modules.knowledge.lightweight_converter import (
        _get_pdf_page_count,
    )

from meho_app.modules.knowledge.embeddings import VoyageAIEmbeddings
from meho_app.worker.arrow_codec import serialize_chunks

# Use stdlib logging directly to avoid pulling in meho_app.core.otel
# (which imports the full app config stack with Pydantic, Redis, etc.)
logger = logging.getLogger("meho_app.worker.ingest")

CONTENT_TYPE_PDF = "application/pdf"
FILE_URI_PREFIX = "file://"

# Embedding dimension matches Voyage AI voyage-4-large (1024D).
_EMBEDDING_DIM: int = 1024

# Maximum number of texts per Voyage AI embed_batch call to avoid OOM.
_EMBEDDING_BATCH_SIZE: int = 100


# ---------------------------------------------------------------------------
# MIME type detection
# ---------------------------------------------------------------------------


def _detect_mime_type(url: str, file_bytes: bytes) -> str:
    """Detect MIME type from URL extension or file magic bytes.

    Args:
        url: Source URL or file path.
        file_bytes: Raw file content (first bytes checked for magic).

    Returns:
        MIME type string (e.g., CONTENT_TYPE_PDF).
    """
    # Try URL extension first
    path = url.split("?")[0]  # Strip query params from signed URLs
    mime, _ = mimetypes.guess_type(path)
    if mime:
        return mime

    # Magic byte detection for common formats
    if file_bytes[:5] == b"%PDF-":
        return CONTENT_TYPE_PDF
    if file_bytes[:4] == b"PK\x03\x04":
        # ZIP-based: could be DOCX, XLSX, etc.
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if file_bytes[:5] in (b"<html", b"<!DOC", b"<!doc"):
        return "text/html"

    # Default to PDF (most common ingestion format)
    return CONTENT_TYPE_PDF


# ---------------------------------------------------------------------------
# Download / Upload
# ---------------------------------------------------------------------------


async def _download_document(url: str) -> bytes:
    """Download document from signed URL or local filesystem.

    Args:
        url: HTTP(S) URL, file:// URL, or absolute filesystem path.

    Returns:
        Raw file bytes.

    Raises:
        httpx.HTTPStatusError: On HTTP error responses.
        FileNotFoundError: If local file does not exist.
    """
    # Local filesystem: file:// URL or absolute path
    if url.startswith(FILE_URI_PREFIX):
        local_path = url[7:]  # Strip file:// prefix
        return await asyncio.to_thread(pathlib.Path(local_path).read_bytes)
    if url.startswith("/"):
        return await asyncio.to_thread(pathlib.Path(url).read_bytes)

    # HTTP download
    async with httpx.AsyncClient(timeout=600.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


async def _upload_results(url: str, data: bytes) -> None:
    """Upload Arrow IPC bytes to signed URL or local filesystem.

    Args:
        url: HTTP(S) URL, file:// URL, or absolute filesystem path.
        data: Serialized Arrow IPC bytes to upload.

    Raises:
        httpx.HTTPStatusError: On HTTP error responses.
    """
    # Local filesystem: file:// URL or absolute path
    if url.startswith(FILE_URI_PREFIX):
        local_path = url[7:]  # Strip file:// prefix
        await asyncio.to_thread(pathlib.Path(local_path).write_bytes, data)
        return
    if url.startswith("/"):
        await asyncio.to_thread(pathlib.Path(url).write_bytes, data)
        return

    # HTTP upload
    async with httpx.AsyncClient(timeout=600.0) as client:
        response = await client.put(url, content=data)
        response.raise_for_status()


# ---------------------------------------------------------------------------
# Embedding generation
# ---------------------------------------------------------------------------


async def _generate_embeddings(
    texts: list[str],
    voyage_api_key: str | None,
) -> list[list[float]]:
    """Generate embeddings for chunk texts.

    If VOYAGE_API_KEY is set, uses Voyage AI cloud embeddings in batches
    of _EMBEDDING_BATCH_SIZE. Otherwise returns zero vectors (on-prem will
    regenerate using local TEI).

    Args:
        texts: List of chunk texts to embed.
        voyage_api_key: Voyage AI API key, or None for zero vectors.

    Returns:
        List of embedding vectors (1024D float32).
    """
    if not texts:
        return []

    if not voyage_api_key:
        logger.info("no_voyage_api_key: using zero vectors for embeddings")
        return [[0.0] * _EMBEDDING_DIM for _ in texts]

    provider = VoyageAIEmbeddings(api_key=voyage_api_key)

    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), _EMBEDDING_BATCH_SIZE):
        batch = texts[i : i + _EMBEDDING_BATCH_SIZE]
        batch_embeddings = await provider.embed_batch(batch)
        all_embeddings.extend(batch_embeddings)

    return all_embeddings


# ---------------------------------------------------------------------------
# Document processing pipeline
# ---------------------------------------------------------------------------


async def _process(
    input_url: str,
    output_url: str,
    job_id: str,
    voyage_api_key: str | None = None,
    chunk_prefix: str = "",
    ocr_enabled: bool = False,
    page_batch_size: int = 50,
) -> None:
    """Main async processing pipeline.

    Downloads document, converts with Docling (chapter-aware batching for
    large PDFs), chunks, generates embeddings, serializes as Arrow IPC + zstd,
    and uploads results.

    Args:
        input_url: Signed URL or file path to source document.
        output_url: Signed URL or file path for Arrow IPC output.
        job_id: Unique job identifier for logging.
        voyage_api_key: Optional Voyage AI API key for cloud embeddings.
        chunk_prefix: Context prefix for each chunk.
        ocr_enabled: Whether to enable OCR.
        page_batch_size: Max pages per conversion batch for large PDFs.
    """
    # Step 1: Download document
    logger.info("downloading_document", extra={"job_id": job_id, "input_url": input_url})
    file_bytes = await _download_document(input_url)
    logger.info("document_downloaded", extra={"job_id": job_id, "size_bytes": len(file_bytes)})

    # Step 2: Detect MIME type
    mime_type = _detect_mime_type(input_url, file_bytes)
    logger.info("mime_type_detected", extra={"job_id": job_id, "mime_type": mime_type})

    # Step 3: Convert and chunk
    chunks = _convert_and_chunk(
        file_bytes=file_bytes,
        filename=_extract_filename(input_url),
        mime_type=mime_type,
        chunk_prefix=chunk_prefix,
        ocr_enabled=ocr_enabled,
        page_batch_size=page_batch_size,
        job_id=job_id,
    )
    logger.info("chunks_produced", extra={"job_id": job_id, "num_chunks": len(chunks)})

    # Step 4: Generate embeddings
    texts = [text for text, _meta in chunks]
    embeddings = await _generate_embeddings(texts, voyage_api_key)
    logger.info("embeddings_generated", extra={"job_id": job_id, "num_embeddings": len(embeddings)})

    # Step 5: Serialize as Arrow IPC + zstd
    arrow_data = serialize_chunks(chunks, embeddings)
    logger.info("arrow_serialized", extra={"job_id": job_id, "size_bytes": len(arrow_data)})

    # Step 6: Upload results
    logger.info("uploading_results", extra={"job_id": job_id, "output_url": output_url})
    await _upload_results(output_url, arrow_data)
    logger.info("results_uploaded", extra={"job_id": job_id})


def _convert_and_chunk(
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    chunk_prefix: str,
    ocr_enabled: bool,
    page_batch_size: int,
    job_id: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Convert document and produce chunks.

    For PDFs with more pages than page_batch_size, uses chapter-aware
    batching with a fresh DoclingDocumentConverter per batch (to bound
    memory). Small documents use a single conversion.

    Args:
        file_bytes: Raw document bytes.
        filename: Original filename.
        mime_type: Detected MIME type.
        chunk_prefix: Context prefix for each chunk.
        ocr_enabled: Whether to enable OCR.
        page_batch_size: Max pages per batch.
        job_id: Job ID for logging.

    Returns:
        List of (text, metadata) chunk tuples.
    """
    if not _use_docling:
        # Lightweight path: direct conversion, no batching needed
        # (no PyTorch memory leak concern)
        converter = DoclingDocumentConverter(ocr_enabled=ocr_enabled)
        doc = converter.convert_file(file_bytes, filename, mime_type)
        chunks = converter.chunk_document(doc, chunk_prefix=chunk_prefix)
        return chunks

    is_pdf = mime_type == CONTENT_TYPE_PDF
    page_count = 0

    if is_pdf:
        page_count = _get_pdf_page_count(file_bytes)
        logger.info(
            "pdf_page_count",
            extra={"job_id": job_id, "page_count": page_count},
        )

    # Large PDF: chapter-aware batching
    if is_pdf and page_count > page_batch_size:
        return _convert_batched_pdf(
            file_bytes=file_bytes,
            filename=filename,
            mime_type=mime_type,
            chunk_prefix=chunk_prefix,
            ocr_enabled=ocr_enabled,
            page_batch_size=page_batch_size,
            page_count=page_count,
            job_id=job_id,
        )

    # Small doc or non-PDF: single conversion
    converter = DoclingDocumentConverter(ocr_enabled=ocr_enabled)
    doc = converter.convert_file(file_bytes, filename, mime_type)
    chunks = converter.chunk_document(doc, chunk_prefix=chunk_prefix)

    # Help GC reclaim Docling's internal state
    del doc
    del converter

    return chunks


def _convert_batched_pdf(
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    chunk_prefix: str,
    ocr_enabled: bool,
    page_batch_size: int,
    page_count: int,
    job_id: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Convert a large PDF in chapter-aware batches.

    Each batch gets a fresh DoclingDocumentConverter to prevent memory
    accumulation. This is the in-process equivalent of
    subprocess_converter.py's batch loop, but here the container IS
    the ephemeral process.

    Args:
        file_bytes: Full PDF bytes.
        filename: Original filename.
        mime_type: MIME type (application/pdf).
        chunk_prefix: Context prefix for each chunk.
        ocr_enabled: Whether to enable OCR.
        page_batch_size: Max pages per batch.
        page_count: Total page count.
        job_id: Job ID for logging.

    Returns:
        Aggregated list of (text, metadata) chunk tuples from all batches.
    """
    toc = _extract_pdf_toc(file_bytes)
    batches = _compute_chapter_batches(toc, page_count, page_batch_size)

    logger.info(
        "pdf_batched_conversion_starting",
        extra={
            "job_id": job_id,
            "total_pages": page_count,
            "num_batches": len(batches),
            "has_toc": len(toc) > 0,
        },
    )

    all_chunks: list[tuple[str, dict[str, Any]]] = []

    for i, (start, end) in enumerate(batches):
        logger.info(
            "pdf_batch_converting",
            extra={
                "job_id": job_id,
                "batch": i + 1,
                "total_batches": len(batches),
                "start_page": start,
                "end_page": end,
            },
        )

        # Extract mini-PDF for this batch
        batch_bytes = _extract_pdf_page_range(file_bytes, start, end)
        batch_filename = f"{filename}[{start}-{end}]"

        # Fresh converter per batch to prevent memory accumulation
        converter = DoclingDocumentConverter(ocr_enabled=ocr_enabled)
        doc = converter.convert_file(batch_bytes, batch_filename, mime_type)
        batch_chunks = converter.chunk_document(doc, chunk_prefix=chunk_prefix)
        all_chunks.extend(batch_chunks)

        # Explicitly delete to help Python GC between batches
        del doc
        del converter
        del batch_bytes

        logger.info(
            "pdf_batch_complete",
            extra={
                "job_id": job_id,
                "batch": i + 1,
                "chunks_in_batch": len(batch_chunks),
            },
        )

    return all_chunks


def _extract_filename(url: str) -> str:
    """Extract filename from URL or path.

    Args:
        url: HTTP URL, file:// URL, or filesystem path.

    Returns:
        Filename component (e.g., "document.pdf").
    """
    # Strip query parameters from signed URLs
    path = url.split("?")[0]
    # Handle file:// prefix
    if path.startswith(FILE_URI_PREFIX):
        path = path[7:]
    return pathlib.Path(path).name or "document"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_worker() -> int:
    """Run the ephemeral ingestion worker.

    Reads job parameters from environment variables, executes the async
    processing pipeline, and returns an exit code.

    Returns:
        0 on success, 1 on any failure.
    """
    # Configure logging for worker context
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Read required env vars
    job_id = os.environ.get("WORKER_JOB_ID")
    input_url = os.environ.get("WORKER_INPUT_URL")
    output_url = os.environ.get("WORKER_OUTPUT_URL")

    if not job_id:
        logger.error("WORKER_JOB_ID environment variable is required")
        return 1
    if not input_url:
        logger.error("WORKER_INPUT_URL environment variable is required")
        return 1
    if not output_url:
        logger.error("WORKER_OUTPUT_URL environment variable is required")
        return 1

    # Read optional env vars
    voyage_api_key = os.environ.get("VOYAGE_API_KEY")
    chunk_prefix = os.environ.get("WORKER_CHUNK_PREFIX", "")
    ocr_enabled = os.environ.get("WORKER_OCR_ENABLED", "false").lower() == "true"
    page_batch_size = int(os.environ.get("WORKER_PAGE_BATCH_SIZE", "50"))

    logger.info(
        "worker_starting",
        extra={
            "job_id": job_id,
            "input_url": input_url,
            "output_url": output_url,
            "has_voyage_key": bool(voyage_api_key),
            "ocr_enabled": ocr_enabled,
            "page_batch_size": page_batch_size,
        },
    )

    try:
        asyncio.run(
            _process(
                input_url=input_url,
                output_url=output_url,
                job_id=job_id,
                voyage_api_key=voyage_api_key,
                chunk_prefix=chunk_prefix,
                ocr_enabled=ocr_enabled,
                page_batch_size=page_batch_size,
            )
        )
        logger.info("worker_completed_successfully", extra={"job_id": job_id})
        return 0
    except Exception:
        logger.exception("worker_failed", extra={"job_id": job_id})
        return 1
