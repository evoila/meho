# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for ephemeral ingestion worker pipeline.

Tests the worker entrypoint and ingest pipeline with mocked
HTTP, Docling, and embedding dependencies.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunks(n: int = 3) -> list[tuple[str, dict[str, Any]]]:
    """Create mock chunk tuples (text, metadata)."""
    return [
        (f"chunk text {i}", {"heading_stack": [f"Section {i}"], "page_numbers": [i]})
        for i in range(n)
    ]


def _make_embeddings(n: int = 3, dim: int = 1024) -> list[list[float]]:
    """Create mock embeddings."""
    return [[0.1 * (i + 1)] * dim for i in range(n)]


# ---------------------------------------------------------------------------
# run_worker() env var reading
# ---------------------------------------------------------------------------


class TestRunWorkerEnvVars:
    """run_worker() reads required env vars and returns 1 on missing."""

    def test_missing_job_id_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Worker exits 1 when WORKER_JOB_ID is missing."""
        monkeypatch.delenv("WORKER_JOB_ID", raising=False)
        monkeypatch.delenv("WORKER_INPUT_URL", raising=False)
        monkeypatch.delenv("WORKER_OUTPUT_URL", raising=False)

        from meho_app.worker.ingest import run_worker

        assert run_worker() == 1

    def test_missing_input_url_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Worker exits 1 when WORKER_INPUT_URL is missing."""
        monkeypatch.setenv("WORKER_JOB_ID", "test-job-1")
        monkeypatch.delenv("WORKER_INPUT_URL", raising=False)
        monkeypatch.delenv("WORKER_OUTPUT_URL", raising=False)

        from meho_app.worker.ingest import run_worker

        assert run_worker() == 1

    def test_missing_output_url_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Worker exits 1 when WORKER_OUTPUT_URL is missing."""
        monkeypatch.setenv("WORKER_JOB_ID", "test-job-1")
        monkeypatch.setenv("WORKER_INPUT_URL", "https://storage.example.com/input.pdf")
        monkeypatch.delenv("WORKER_OUTPUT_URL", raising=False)

        from meho_app.worker.ingest import run_worker

        assert run_worker() == 1


# ---------------------------------------------------------------------------
# _download_document()
# ---------------------------------------------------------------------------


class TestDownloadDocument:
    """_download_document() fetches documents from HTTP or filesystem."""

    @pytest.mark.anyio
    async def test_download_from_http_url(self) -> None:
        """Downloads document via HTTP GET from signed URL."""
        from meho_app.worker.ingest import _download_document

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"PDF content bytes"
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("meho_app.worker.ingest.httpx.AsyncClient", return_value=mock_client):
            result = await _download_document("https://storage.example.com/doc.pdf")

        assert result == b"PDF content bytes"
        mock_client.get.assert_called_once_with("https://storage.example.com/doc.pdf")

    @pytest.mark.anyio
    async def test_download_http_404_raises(self) -> None:
        """HTTP 404 raises an exception."""
        from meho_app.worker.ingest import _download_document

        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Not Found", request=MagicMock(), response=mock_response
            )
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("meho_app.worker.ingest.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await _download_document("https://storage.example.com/missing.pdf")

    @pytest.mark.anyio
    async def test_download_from_file_url(self, tmp_path: Any) -> None:
        """Downloads document from file:// URL."""
        from meho_app.worker.ingest import _download_document

        doc_path = tmp_path / "test.pdf"
        doc_path.write_bytes(b"local PDF bytes")

        result = await _download_document(f"file://{doc_path}")
        assert result == b"local PDF bytes"

    @pytest.mark.anyio
    async def test_download_from_absolute_path(self, tmp_path: Any) -> None:
        """Downloads document from absolute filesystem path."""
        from meho_app.worker.ingest import _download_document

        doc_path = tmp_path / "test.pdf"
        doc_path.write_bytes(b"local PDF bytes direct")

        result = await _download_document(str(doc_path))
        assert result == b"local PDF bytes direct"


# ---------------------------------------------------------------------------
# _upload_results()
# ---------------------------------------------------------------------------


class TestUploadResults:
    """_upload_results() uploads Arrow bytes to HTTP or filesystem."""

    @pytest.mark.anyio
    async def test_upload_to_http_url(self) -> None:
        """Uploads result via HTTP PUT to signed URL."""
        from meho_app.worker.ingest import _upload_results

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.put = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("meho_app.worker.ingest.httpx.AsyncClient", return_value=mock_client):
            await _upload_results("https://storage.example.com/output.arrow", b"arrow data")

        mock_client.put.assert_called_once_with(
            "https://storage.example.com/output.arrow",
            content=b"arrow data",
        )

    @pytest.mark.anyio
    async def test_upload_http_500_raises(self) -> None:
        """HTTP 500 on upload raises an exception."""
        from meho_app.worker.ingest import _upload_results

        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error", request=MagicMock(), response=mock_response
            )
        )

        mock_client = AsyncMock()
        mock_client.put = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("meho_app.worker.ingest.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await _upload_results("https://storage.example.com/output.arrow", b"data")

    @pytest.mark.anyio
    async def test_upload_to_file_url(self, tmp_path: Any) -> None:
        """Uploads result to file:// URL."""
        from meho_app.worker.ingest import _upload_results

        output_path = tmp_path / "output.arrow"
        await _upload_results(f"file://{output_path}", b"arrow data local")

        assert output_path.read_bytes() == b"arrow data local"

    @pytest.mark.anyio
    async def test_upload_to_absolute_path(self, tmp_path: Any) -> None:
        """Uploads result to absolute filesystem path."""
        from meho_app.worker.ingest import _upload_results

        output_path = tmp_path / "output.arrow"
        await _upload_results(str(output_path), b"arrow data direct")

        assert output_path.read_bytes() == b"arrow data direct"


# ---------------------------------------------------------------------------
# run_worker() end-to-end pipeline (mocked)
# ---------------------------------------------------------------------------


class TestRunWorkerPipeline:
    """End-to-end tests for the worker pipeline with all deps mocked."""

    def _setup_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set required env vars for worker."""
        monkeypatch.setenv("WORKER_JOB_ID", "test-job-42")
        monkeypatch.setenv("WORKER_INPUT_URL", "https://storage.example.com/input.pdf")
        monkeypatch.setenv("WORKER_OUTPUT_URL", "https://storage.example.com/output.arrow")

    def test_successful_pipeline_returns_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Successful end-to-end pipeline returns exit code 0."""
        self._setup_env(monkeypatch)
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")

        chunks = _make_chunks(3)
        embeddings = _make_embeddings(3)

        # Mock document converter
        mock_doc = MagicMock()
        mock_converter_instance = MagicMock()
        mock_converter_instance.convert_file.return_value = mock_doc
        mock_converter_instance.chunk_document.return_value = chunks
        mock_converter_cls = MagicMock(return_value=mock_converter_instance)

        # Mock embedding provider
        mock_embed_instance = MagicMock()
        mock_embed_instance.embed_batch = AsyncMock(return_value=embeddings)
        mock_voyage_cls = MagicMock(return_value=mock_embed_instance)

        # Mock serialize
        mock_serialize = MagicMock(return_value=b"serialized-arrow-data")

        # Mock download and upload
        mock_download = AsyncMock(return_value=b"%PDF-1.4 fake pdf content")
        mock_upload = AsyncMock()

        # Mock _get_pdf_page_count (small doc, no batching)
        mock_page_count = MagicMock(return_value=10)

        # Mock _detect_mime_type
        mock_detect = MagicMock(return_value="application/pdf")

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
            patch("meho_app.worker.ingest.DoclingDocumentConverter", mock_converter_cls),
            patch("meho_app.worker.ingest.VoyageAIEmbeddings", mock_voyage_cls),
            patch("meho_app.worker.ingest.serialize_chunks", mock_serialize),
            patch("meho_app.worker.ingest._get_pdf_page_count", mock_page_count),
            patch("meho_app.worker.ingest._detect_mime_type", mock_detect),
        ):
            result = run_worker()

        assert result == 0
        mock_download.assert_called_once()
        mock_upload.assert_called_once()
        mock_serialize.assert_called_once_with(chunks, embeddings)

    def test_download_failure_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Download failure (HTTP 404) returns exit code 1, no upload attempted."""
        self._setup_env(monkeypatch)

        mock_download = AsyncMock(side_effect=Exception("HTTP 404 Not Found"))
        mock_upload = AsyncMock()

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
        ):
            result = run_worker()

        assert result == 1
        mock_upload.assert_not_called()

    def test_conversion_failure_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Docling conversion failure returns exit code 1."""
        self._setup_env(monkeypatch)

        mock_download = AsyncMock(return_value=b"%PDF-1.4 content")
        mock_upload = AsyncMock()

        mock_converter_instance = MagicMock()
        mock_converter_instance.convert_file.side_effect = ValueError("Docling error")
        mock_converter_cls = MagicMock(return_value=mock_converter_instance)

        mock_page_count = MagicMock(return_value=5)
        mock_detect = MagicMock(return_value="application/pdf")

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
            patch("meho_app.worker.ingest.DoclingDocumentConverter", mock_converter_cls),
            patch("meho_app.worker.ingest._get_pdf_page_count", mock_page_count),
            patch("meho_app.worker.ingest._detect_mime_type", mock_detect),
        ):
            result = run_worker()

        assert result == 1
        mock_upload.assert_not_called()

    def test_upload_failure_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Upload failure (HTTP 500) returns exit code 1."""
        self._setup_env(monkeypatch)

        chunks = _make_chunks(2)
        embeddings = _make_embeddings(2)

        mock_doc = MagicMock()
        mock_converter_instance = MagicMock()
        mock_converter_instance.convert_file.return_value = mock_doc
        mock_converter_instance.chunk_document.return_value = chunks
        mock_converter_cls = MagicMock(return_value=mock_converter_instance)

        mock_embed_instance = MagicMock()
        mock_embed_instance.embed_batch = AsyncMock(return_value=embeddings)

        mock_download = AsyncMock(return_value=b"%PDF-1.4 content")
        mock_upload = AsyncMock(side_effect=Exception("HTTP 500"))
        mock_serialize = MagicMock(return_value=b"arrow-data")
        mock_page_count = MagicMock(return_value=5)
        mock_detect = MagicMock(return_value="application/pdf")

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
            patch("meho_app.worker.ingest.DoclingDocumentConverter", mock_converter_cls),
            patch(
                "meho_app.worker.ingest.VoyageAIEmbeddings",
                MagicMock(return_value=mock_embed_instance),
            ),
            patch("meho_app.worker.ingest.serialize_chunks", mock_serialize),
            patch("meho_app.worker.ingest._get_pdf_page_count", mock_page_count),
            patch("meho_app.worker.ingest._detect_mime_type", mock_detect),
        ):
            result = run_worker()

        assert result == 1

    def test_no_voyage_key_uses_zero_vectors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without VOYAGE_API_KEY, embeddings are zero vectors."""
        self._setup_env(monkeypatch)
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        chunks = _make_chunks(2)

        mock_doc = MagicMock()
        mock_converter_instance = MagicMock()
        mock_converter_instance.convert_file.return_value = mock_doc
        mock_converter_instance.chunk_document.return_value = chunks
        mock_converter_cls = MagicMock(return_value=mock_converter_instance)

        captured_embeddings: list[list[float]] = []

        def capture_serialize(c: list[tuple[str, dict[str, Any]]], e: list[list[float]]) -> bytes:
            captured_embeddings.extend(e)
            return b"arrow-data"

        mock_download = AsyncMock(return_value=b"%PDF-1.4 content")
        mock_upload = AsyncMock()
        mock_page_count = MagicMock(return_value=5)
        mock_detect = MagicMock(return_value="application/pdf")

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
            patch("meho_app.worker.ingest.DoclingDocumentConverter", mock_converter_cls),
            patch("meho_app.worker.ingest.serialize_chunks", capture_serialize),
            patch("meho_app.worker.ingest._get_pdf_page_count", mock_page_count),
            patch("meho_app.worker.ingest._detect_mime_type", mock_detect),
        ):
            result = run_worker()

        assert result == 0
        assert len(captured_embeddings) == 2
        # All zero vectors with dimension 1024
        for emb in captured_embeddings:
            assert len(emb) == 1024
            assert all(v == pytest.approx(0.0) for v in emb)


# ---------------------------------------------------------------------------
# Chapter-aware batching for large PDFs
# ---------------------------------------------------------------------------


class TestChapterAwareBatching:
    """Worker uses chapter-aware batching for large PDFs (>batch_size pages)."""

    def test_large_pdf_uses_batching(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PDFs with pages > batch_size use chapter-aware batching."""
        monkeypatch.setenv("WORKER_JOB_ID", "test-batch-job")
        monkeypatch.setenv("WORKER_INPUT_URL", "https://storage.example.com/large.pdf")
        monkeypatch.setenv("WORKER_OUTPUT_URL", "https://storage.example.com/output.arrow")
        monkeypatch.setenv("WORKER_PAGE_BATCH_SIZE", "50")
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        # Mock download
        mock_download = AsyncMock(return_value=b"%PDF-1.4 large document")
        mock_upload = AsyncMock()
        mock_detect = MagicMock(return_value="application/pdf")

        # Return 120 pages (> 50 batch size)
        mock_page_count = MagicMock(return_value=120)
        # No TOC -> fixed batches: (1,50), (51,100), (101,120)
        mock_toc = MagicMock(return_value=[])
        mock_batches = MagicMock(return_value=[(1, 50), (51, 100), (101, 120)])
        mock_extract_range = MagicMock(return_value=b"%PDF-1.4 batch bytes")

        # Each batch produces 2 chunks
        batch_chunks = _make_chunks(2)
        mock_doc = MagicMock()
        mock_converter_instance = MagicMock()
        mock_converter_instance.convert_file.return_value = mock_doc
        mock_converter_instance.chunk_document.return_value = batch_chunks
        mock_converter_cls = MagicMock(return_value=mock_converter_instance)

        mock_serialize = MagicMock(return_value=b"arrow-data")

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
            patch("meho_app.worker.ingest.DoclingDocumentConverter", mock_converter_cls),
            patch("meho_app.worker.ingest.serialize_chunks", mock_serialize),
            patch("meho_app.worker.ingest._get_pdf_page_count", mock_page_count),
            patch("meho_app.worker.ingest._extract_pdf_toc", mock_toc),
            patch("meho_app.worker.ingest._compute_chapter_batches", mock_batches),
            patch("meho_app.worker.ingest._extract_pdf_page_range", mock_extract_range),
            patch("meho_app.worker.ingest._detect_mime_type", mock_detect),
        ):
            result = run_worker()

        assert result == 0
        # 3 batches -> 3 converter instances (fresh per batch)
        assert mock_converter_cls.call_count == 3
        # 3 batches * 2 chunks each = 6 total chunks
        serialize_call = mock_serialize.call_args
        assert len(serialize_call[0][0]) == 6  # 6 chunks
        assert len(serialize_call[0][1]) == 6  # 6 embeddings

    def test_small_pdf_no_batching(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PDFs with pages <= batch_size use single conversion."""
        monkeypatch.setenv("WORKER_JOB_ID", "test-small-job")
        monkeypatch.setenv("WORKER_INPUT_URL", "https://storage.example.com/small.pdf")
        monkeypatch.setenv("WORKER_OUTPUT_URL", "https://storage.example.com/output.arrow")
        monkeypatch.setenv("WORKER_PAGE_BATCH_SIZE", "50")
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        chunks = _make_chunks(2)
        mock_download = AsyncMock(return_value=b"%PDF-1.4 small")
        mock_upload = AsyncMock()
        mock_detect = MagicMock(return_value="application/pdf")

        # Only 15 pages (< 50 batch size)
        mock_page_count = MagicMock(return_value=15)

        mock_doc = MagicMock()
        mock_converter_instance = MagicMock()
        mock_converter_instance.convert_file.return_value = mock_doc
        mock_converter_instance.chunk_document.return_value = chunks
        mock_converter_cls = MagicMock(return_value=mock_converter_instance)

        mock_serialize = MagicMock(return_value=b"arrow-data")

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
            patch("meho_app.worker.ingest.DoclingDocumentConverter", mock_converter_cls),
            patch("meho_app.worker.ingest.serialize_chunks", mock_serialize),
            patch("meho_app.worker.ingest._get_pdf_page_count", mock_page_count),
            patch("meho_app.worker.ingest._detect_mime_type", mock_detect),
        ):
            result = run_worker()

        assert result == 0
        # Single conversion: only 1 converter instance
        assert mock_converter_cls.call_count == 1
