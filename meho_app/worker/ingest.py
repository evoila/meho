# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Ephemeral ingestion worker pipeline.

Stateless worker that processes a single document end-to-end:
download -> convert (lightweight) -> chunk -> embed (in-process fastembed)
-> serialize -> upload -> exit.

Invoked as ``python -m meho_app.worker`` by all container-based backends.
The worker reads env vars directly (no app config stack):

Environment variables:
    WORKER_JOB_ID (required): Unique job identifier from MEHO knowledge module.
    WORKER_INPUT_URL (required): Signed URL or file:// path to source document.
    WORKER_OUTPUT_URL (required): Signed URL or file:// path for Arrow IPC output.
    FASTEMBED_EMBEDDING_MODEL (optional): fastembed model name. If unset, the
        worker emits zero vectors and the API regenerates embeddings on its side.
    FASTEMBED_CACHE_DIR (optional): On-disk cache directory for ONNX weights.
    WORKER_CHUNK_PREFIX (optional): Context prefix for each chunk.
    WORKER_OCR_ENABLED (optional): Enable OCR for scanned PDFs (default "false").
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import pathlib
from typing import Any

import httpx

from meho_app.modules.knowledge.lightweight_converter import LightweightDocumentConverter
from meho_app.modules.knowledge.retrieval_context import build_retrieval_text_from_metadata
from meho_app.worker.arrow_codec import serialize_chunks

logger = logging.getLogger("meho_app.worker.ingest")

CONTENT_TYPE_PDF = "application/pdf"
FILE_URI_PREFIX = "file://"

# Embedding dimension for the default fastembed multilingual MiniLM-L12 model.
_EMBEDDING_DIM: int = 384


def _detect_mime_type(url: str, file_bytes: bytes) -> str:
    """Detect MIME type from URL extension or file magic bytes."""
    path = url.split("?")[0]
    mime, _ = mimetypes.guess_type(path)
    if mime:
        return mime

    if file_bytes[:5] == b"%PDF-":
        return CONTENT_TYPE_PDF
    if file_bytes[:4] == b"PK\x03\x04":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if file_bytes[:5] in (b"<html", b"<!DOC", b"<!doc"):
        return "text/html"

    return CONTENT_TYPE_PDF


async def _download_document(url: str) -> bytes:
    """Download document from signed URL or local filesystem."""
    if url.startswith(FILE_URI_PREFIX):
        local_path = url[7:]
        return await asyncio.to_thread(pathlib.Path(local_path).read_bytes)
    if url.startswith("/"):
        return await asyncio.to_thread(pathlib.Path(url).read_bytes)

    async with httpx.AsyncClient(timeout=600.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


async def _upload_results(url: str, data: bytes) -> None:
    """Upload Arrow IPC bytes to signed URL or local filesystem."""
    if url.startswith(FILE_URI_PREFIX):
        local_path = url[7:]
        await asyncio.to_thread(pathlib.Path(local_path).write_bytes, data)
        return
    if url.startswith("/"):
        await asyncio.to_thread(pathlib.Path(url).write_bytes, data)
        return

    async with httpx.AsyncClient(timeout=600.0) as client:
        response = await client.put(url, content=data)
        response.raise_for_status()


async def _generate_embeddings(
    texts: list[str],
    model_name: str | None,
    cache_dir: str | None,
) -> list[list[float]]:
    """Generate embeddings via fastembed in-process.

    If ``model_name`` is unset, returns zero vectors so the API can
    regenerate embeddings after the worker completes.
    """
    if not texts:
        return []

    if not model_name:
        logger.info("no_fastembed_model: returning zero vectors for embeddings")
        return [[0.0] * _EMBEDDING_DIM for _ in texts]

    from meho_app.modules.knowledge.embeddings import FastEmbedEmbeddings

    provider = FastEmbedEmbeddings(model_name=model_name, cache_dir=cache_dir)
    return await provider.embed_batch(texts)


async def _process(
    input_url: str,
    output_url: str,
    job_id: str,
    model_name: str | None = None,
    cache_dir: str | None = None,
    chunk_prefix: str = "",
    ocr_enabled: bool = False,
) -> None:
    """Main async processing pipeline."""
    logger.info("downloading_document", extra={"job_id": job_id, "input_url": input_url})
    file_bytes = await _download_document(input_url)
    logger.info("document_downloaded", extra={"job_id": job_id, "size_bytes": len(file_bytes)})

    mime_type = _detect_mime_type(input_url, file_bytes)
    logger.info("mime_type_detected", extra={"job_id": job_id, "mime_type": mime_type})

    filename = _extract_filename(input_url)
    chunks = _convert_and_chunk(
        file_bytes=file_bytes,
        filename=filename,
        mime_type=mime_type,
        chunk_prefix=chunk_prefix,
        ocr_enabled=ocr_enabled,
        job_id=job_id,
    )
    logger.info("chunks_produced", extra={"job_id": job_id, "num_chunks": len(chunks)})

    retrieval_texts = [
        build_retrieval_text_from_metadata(text=text, source_uri=filename, metadata=meta)
        for text, meta in chunks
    ]
    embeddings = await _generate_embeddings(retrieval_texts, model_name, cache_dir)
    logger.info(
        "embeddings_generated",
        extra={"job_id": job_id, "num_embeddings": len(embeddings)},
    )

    arrow_data = serialize_chunks(chunks, embeddings)
    logger.info("arrow_serialized", extra={"job_id": job_id, "size_bytes": len(arrow_data)})

    logger.info("uploading_results", extra={"job_id": job_id, "output_url": output_url})
    await _upload_results(output_url, arrow_data)
    logger.info("results_uploaded", extra={"job_id": job_id})


def _convert_and_chunk(
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    chunk_prefix: str,
    ocr_enabled: bool,
    job_id: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Convert document and produce chunks via the lightweight converter."""
    converter = LightweightDocumentConverter(ocr_enabled=ocr_enabled)
    doc = converter.convert_file(file_bytes, filename, mime_type)
    chunks = converter.chunk_document(doc, chunk_prefix=chunk_prefix)
    logger.info(
        "lightweight_conversion_complete",
        extra={"job_id": job_id, "num_chunks": len(chunks)},
    )
    return chunks


def _extract_filename(url: str) -> str:
    """Extract filename from URL or path."""
    path = url.split("?")[0]
    if path.startswith(FILE_URI_PREFIX):
        path = path[7:]
    return pathlib.Path(path).name or "document"


def run_worker() -> int:
    """Run the ephemeral ingestion worker."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

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

    model_name = os.environ.get("FASTEMBED_EMBEDDING_MODEL")
    cache_dir = os.environ.get("FASTEMBED_CACHE_DIR")
    chunk_prefix = os.environ.get("WORKER_CHUNK_PREFIX", "")
    ocr_enabled = os.environ.get("WORKER_OCR_ENABLED", "false").lower() == "true"

    logger.info(
        "worker_starting",
        extra={
            "job_id": job_id,
            "input_url": input_url,
            "output_url": output_url,
            "fastembed_model": model_name,
            "ocr_enabled": ocr_enabled,
        },
    )

    try:
        asyncio.run(
            _process(
                input_url=input_url,
                output_url=output_url,
                job_id=job_id,
                model_name=model_name,
                cache_dir=cache_dir,
                chunk_prefix=chunk_prefix,
                ocr_enabled=ocr_enabled,
            )
        )
        logger.info("worker_completed_successfully", extra={"job_id": job_id})
        return 0
    except Exception:
        logger.exception("worker_failed", extra={"job_id": job_id})
        return 1
