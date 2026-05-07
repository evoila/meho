# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for the ephemeral ingestion worker pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _make_chunks(n: int = 3) -> list[tuple[str, dict[str, Any]]]:
    return [
        (f"chunk text {i}", {"heading_stack": [f"Section {i}"], "page_numbers": [i]})
        for i in range(n)
    ]


def _make_embeddings(n: int = 3, dim: int = 1024) -> list[list[float]]:
    return [[0.1 * (i + 1)] * dim for i in range(n)]


# ---------------------------------------------------------------------------
# run_worker() env var reading
# ---------------------------------------------------------------------------


class TestRunWorkerEnvVars:
    def test_missing_job_id_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WORKER_JOB_ID", raising=False)
        monkeypatch.delenv("WORKER_INPUT_URL", raising=False)
        monkeypatch.delenv("WORKER_OUTPUT_URL", raising=False)
        from meho_app.worker.ingest import run_worker

        assert run_worker() == 1

    def test_missing_input_url_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKER_JOB_ID", "test-job-1")
        monkeypatch.delenv("WORKER_INPUT_URL", raising=False)
        monkeypatch.delenv("WORKER_OUTPUT_URL", raising=False)
        from meho_app.worker.ingest import run_worker

        assert run_worker() == 1

    def test_missing_output_url_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKER_JOB_ID", "test-job-1")
        monkeypatch.setenv("WORKER_INPUT_URL", "https://storage.example.com/input.pdf")
        monkeypatch.delenv("WORKER_OUTPUT_URL", raising=False)
        from meho_app.worker.ingest import run_worker

        assert run_worker() == 1


# ---------------------------------------------------------------------------
# _download_document() / _upload_results()
# ---------------------------------------------------------------------------


class TestDownloadDocument:
    @pytest.mark.anyio
    async def test_download_from_http_url(self) -> None:
        from meho_app.worker.ingest import _download_document

        mock_response = MagicMock()
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
    async def test_download_from_file_url(self, tmp_path: Any) -> None:
        from meho_app.worker.ingest import _download_document

        doc_path = tmp_path / "test.pdf"
        doc_path.write_bytes(b"local PDF bytes")
        result = await _download_document(f"file://{doc_path}")
        assert result == b"local PDF bytes"

    @pytest.mark.anyio
    async def test_download_from_absolute_path(self, tmp_path: Any) -> None:
        from meho_app.worker.ingest import _download_document

        doc_path = tmp_path / "test.pdf"
        doc_path.write_bytes(b"local PDF bytes direct")
        result = await _download_document(str(doc_path))
        assert result == b"local PDF bytes direct"


class TestUploadResults:
    @pytest.mark.anyio
    async def test_upload_to_http_url(self) -> None:
        from meho_app.worker.ingest import _upload_results

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.put = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("meho_app.worker.ingest.httpx.AsyncClient", return_value=mock_client):
            await _upload_results("https://storage.example.com/output.arrow", b"arrow data")

        mock_client.put.assert_called_once_with(
            "https://storage.example.com/output.arrow", content=b"arrow data"
        )

    @pytest.mark.anyio
    async def test_upload_http_500_raises(self) -> None:
        from meho_app.worker.ingest import _upload_results

        mock_response = MagicMock()
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
        from meho_app.worker.ingest import _upload_results

        output_path = tmp_path / "output.arrow"
        await _upload_results(f"file://{output_path}", b"arrow data local")
        assert output_path.read_bytes() == b"arrow data local"


# ---------------------------------------------------------------------------
# _generate_embeddings() — fastembed in-process path
# ---------------------------------------------------------------------------


class TestGenerateEmbeddings:
    @pytest.mark.anyio
    async def test_no_model_returns_zero_vectors(self) -> None:
        from meho_app.worker.ingest import _EMBEDDING_DIM, _generate_embeddings

        result = await _generate_embeddings(["a", "b"], model_name=None, cache_dir=None)
        assert len(result) == 2
        assert all(len(v) == _EMBEDDING_DIM for v in result)
        assert all(all(x == 0.0 for x in v) for v in result)

    @pytest.mark.anyio
    async def test_empty_texts_returns_empty(self) -> None:
        from meho_app.worker.ingest import _generate_embeddings

        assert await _generate_embeddings([], model_name="m", cache_dir=None) == []

    @pytest.mark.anyio
    async def test_fastembed_invoked_when_model_set(self, tmp_path: Path) -> None:
        from meho_app.worker.ingest import _generate_embeddings

        fake_provider = MagicMock()
        fake_provider.embed_batch = AsyncMock(return_value=[[0.1] * 384, [0.2] * 384])

        with patch(
            "meho_app.modules.knowledge.embeddings.FastEmbedEmbeddings",
            return_value=fake_provider,
        ) as cls:
            result = await _generate_embeddings(["a", "b"], model_name="m", cache_dir=str(tmp_path))

        cls.assert_called_once_with(model_name="m", cache_dir=str(tmp_path))
        assert len(result) == 2
        assert all(len(v) == 384 for v in result)


# ---------------------------------------------------------------------------
# run_worker() end-to-end pipeline (mocked converter + TEI)
# ---------------------------------------------------------------------------


class TestRunWorkerPipeline:
    def _setup_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKER_JOB_ID", "test-job-42")
        monkeypatch.setenv("WORKER_INPUT_URL", "https://storage.example.com/input.pdf")
        monkeypatch.setenv("WORKER_OUTPUT_URL", "https://storage.example.com/output.arrow")
        monkeypatch.delenv("FASTEMBED_EMBEDDING_MODEL", raising=False)
        monkeypatch.delenv("FASTEMBED_CACHE_DIR", raising=False)

    def test_successful_pipeline_returns_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._setup_env(monkeypatch)
        monkeypatch.setenv("FASTEMBED_EMBEDDING_MODEL", "dummy-model")

        chunks = _make_chunks(3)
        embeddings = _make_embeddings(3)

        mock_converter = MagicMock()
        mock_converter.convert_file.return_value = MagicMock()
        mock_converter.chunk_document.return_value = chunks
        mock_converter_cls = MagicMock(return_value=mock_converter)

        mock_serialize = MagicMock(return_value=b"serialized-arrow-data")
        mock_download = AsyncMock(return_value=b"%PDF-1.4 fake pdf content")
        mock_upload = AsyncMock()
        mock_detect = MagicMock(return_value="application/pdf")
        mock_generate = AsyncMock(return_value=embeddings)

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
            patch("meho_app.worker.ingest.LightweightDocumentConverter", mock_converter_cls),
            patch("meho_app.worker.ingest._generate_embeddings", mock_generate),
            patch("meho_app.worker.ingest.serialize_chunks", mock_serialize),
            patch("meho_app.worker.ingest._detect_mime_type", mock_detect),
        ):
            result = run_worker()

        assert result == 0
        mock_download.assert_called_once()
        mock_upload.assert_called_once()
        mock_serialize.assert_called_once_with(chunks, embeddings)

    def test_download_failure_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        self._setup_env(monkeypatch)
        mock_download = AsyncMock(return_value=b"%PDF-1.4 content")
        mock_upload = AsyncMock()

        mock_converter = MagicMock()
        mock_converter.convert_file.side_effect = ValueError("conversion error")
        mock_converter_cls = MagicMock(return_value=mock_converter)

        mock_detect = MagicMock(return_value="application/pdf")

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
            patch("meho_app.worker.ingest.LightweightDocumentConverter", mock_converter_cls),
            patch("meho_app.worker.ingest._detect_mime_type", mock_detect),
        ):
            result = run_worker()

        assert result == 1
        mock_upload.assert_not_called()

    def test_no_fastembed_model_uses_zero_vectors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._setup_env(monkeypatch)

        chunks = _make_chunks(2)

        mock_converter = MagicMock()
        mock_converter.convert_file.return_value = MagicMock()
        mock_converter.chunk_document.return_value = chunks
        mock_converter_cls = MagicMock(return_value=mock_converter)

        captured_embeddings: list[list[float]] = []

        def capture_serialize(c: list[tuple[str, dict[str, Any]]], e: list[list[float]]) -> bytes:
            del c
            captured_embeddings.extend(e)
            return b"arrow-data"

        mock_download = AsyncMock(return_value=b"%PDF-1.4 content")
        mock_upload = AsyncMock()
        mock_detect = MagicMock(return_value="application/pdf")

        from meho_app.worker.ingest import run_worker

        with (
            patch("meho_app.worker.ingest._download_document", mock_download),
            patch("meho_app.worker.ingest._upload_results", mock_upload),
            patch("meho_app.worker.ingest.LightweightDocumentConverter", mock_converter_cls),
            patch("meho_app.worker.ingest.serialize_chunks", capture_serialize),
            patch("meho_app.worker.ingest._detect_mime_type", mock_detect),
        ):
            result = run_worker()

        assert result == 0
        assert len(captured_embeddings) == 2
        for emb in captured_embeddings:
            assert len(emb) == 384
            assert all(v == pytest.approx(0.0) for v in emb)
