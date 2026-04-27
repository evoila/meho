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

        mock_adapter_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.pages = 10
        mock_result.elapsed = 1.5
        mock_result.mem_peak_mb = 100.0
        mock_adapter_instance.convert_file.return_value = mock_result
        mock_adapter_instance.chunk_document.return_value = chunks
        mock_adapter_cls = MagicMock(return_value=mock_adapter_instance)

        mock_embed_instance = MagicMock()
        mock_embed_instance.embed_batch = AsyncMock(return_value=embeddings)
        mock_voyage_cls = MagicMock(return_value=mock_embed_instance)

        mock_serialize = MagicMock(return_value=b"serialized-arrow-data")
        mock_download = AsyncMock(return_value=b"%PDF-1.4 fake pdf content")
        mock_upload = AsyncMock()
        mock_detect = MagicMock(return_value="application/pdf")

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
            patch("meho_app.worker.ingest.DoclingWrapperAdapter", mock_adapter_cls),
            patch("meho_app.worker.ingest.VoyageAIEmbeddings", mock_voyage_cls),
            patch("meho_app.worker.ingest.serialize_chunks", mock_serialize),
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
        """DoclingWrapperAdapter conversion failure returns exit code 1."""
        self._setup_env(monkeypatch)

        mock_download = AsyncMock(return_value=b"%PDF-1.4 content")
        mock_upload = AsyncMock()

        mock_adapter_instance = MagicMock()
        mock_adapter_instance.convert_file.side_effect = ValueError("Docling error")
        mock_adapter_cls = MagicMock(return_value=mock_adapter_instance)

        mock_detect = MagicMock(return_value="application/pdf")

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
            patch("meho_app.worker.ingest.DoclingWrapperAdapter", mock_adapter_cls),
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

        mock_adapter_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.pages = 5
        mock_result.elapsed = 1.0
        mock_result.mem_peak_mb = 80.0
        mock_adapter_instance.convert_file.return_value = mock_result
        mock_adapter_instance.chunk_document.return_value = chunks
        mock_adapter_cls = MagicMock(return_value=mock_adapter_instance)

        mock_embed_instance = MagicMock()
        mock_embed_instance.embed_batch = AsyncMock(return_value=embeddings)

        mock_download = AsyncMock(return_value=b"%PDF-1.4 content")
        mock_upload = AsyncMock(side_effect=Exception("HTTP 500"))
        mock_serialize = MagicMock(return_value=b"arrow-data")
        mock_detect = MagicMock(return_value="application/pdf")

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
            patch("meho_app.worker.ingest.DoclingWrapperAdapter", mock_adapter_cls),
            patch(
                "meho_app.worker.ingest.VoyageAIEmbeddings",
                MagicMock(return_value=mock_embed_instance),
            ),
            patch("meho_app.worker.ingest.serialize_chunks", mock_serialize),
            patch("meho_app.worker.ingest._detect_mime_type", mock_detect),
        ):
            result = run_worker()

        assert result == 1

    def test_no_voyage_key_uses_zero_vectors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without VOYAGE_API_KEY, embeddings are zero vectors."""
        self._setup_env(monkeypatch)
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        chunks = _make_chunks(2)

        mock_adapter_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.pages = 5
        mock_result.elapsed = 1.0
        mock_result.mem_peak_mb = 80.0
        mock_adapter_instance.convert_file.return_value = mock_result
        mock_adapter_instance.chunk_document.return_value = chunks
        mock_adapter_cls = MagicMock(return_value=mock_adapter_instance)

        captured_embeddings: list[list[float]] = []

        def capture_serialize(c: list[tuple[str, dict[str, Any]]], e: list[list[float]]) -> bytes:
            captured_embeddings.extend(e)
            return b"arrow-data"

        mock_download = AsyncMock(return_value=b"%PDF-1.4 content")
        mock_upload = AsyncMock()
        mock_detect = MagicMock(return_value="application/pdf")

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
            patch("meho_app.worker.ingest.DoclingWrapperAdapter", mock_adapter_cls),
            patch("meho_app.worker.ingest.serialize_chunks", capture_serialize),
            patch("meho_app.worker.ingest._detect_mime_type", mock_detect),
        ):
            result = run_worker()

        assert result == 0
        assert len(captured_embeddings) == 2
        for emb in captured_embeddings:
            assert len(emb) == 1024
            assert all(v == pytest.approx(0.0) for v in emb)
