# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for ingestion.py dispatch wiring.

Verifies that IngestionService routes documents to the ephemeral worker
when the feature flag is enabled and page count exceeds the threshold,
and falls back to existing subprocess path otherwise.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pyarrow as pa
import pytest

from meho_app.worker.arrow_codec import ARROW_SCHEMA, serialize_chunks
from meho_app.worker.backends.protocol import JobState, JobStatus


# ---------------------------------------------------------------------------
# _should_offload() tests
# ---------------------------------------------------------------------------


class TestShouldOffload:
    """Tests for _should_offload() routing decision."""

    def _make_service(self) -> MagicMock:
        """Create a minimal IngestionService with mocked dependencies."""
        from meho_app.modules.knowledge.ingestion import IngestionService

        svc = object.__new__(IngestionService)
        svc.knowledge_store = MagicMock()
        svc.object_storage = MagicMock()
        svc.job_repository = None
        svc.chunker = MagicMock()
        svc.docling_converter = MagicMock()
        svc.metadata_extractor = MagicMock()
        return svc

    @patch("meho_app.modules.knowledge.ingestion.get_feature_flags")
    def test_flag_off_returns_false(self, mock_flags: MagicMock) -> None:
        """When MEHO_FEATURE_EPHEMERAL_INGESTION=false, returns (False, 0)."""
        mock_flags.return_value = MagicMock(ephemeral_ingestion=False)
        svc = self._make_service()
        should, count = svc._should_offload("application/pdf", b"fake")
        assert should is False
        assert count == 0

    @patch("meho_app.modules.knowledge.ingestion.get_config")
    @patch("meho_app.modules.knowledge.ingestion.get_feature_flags")
    def test_local_backend_returns_false(
        self, mock_flags: MagicMock, mock_config: MagicMock
    ) -> None:
        """When backend='local', returns (False, 0) regardless of page count."""
        mock_flags.return_value = MagicMock(ephemeral_ingestion=True)
        mock_config.return_value = MagicMock(ingestion_backend="local")
        svc = self._make_service()
        should, count = svc._should_offload("application/pdf", b"fake")
        assert should is False
        assert count == 0

    @patch("meho_app.modules.knowledge.ingestion.get_config")
    @patch("meho_app.modules.knowledge.ingestion.get_feature_flags")
    def test_non_pdf_returns_false(self, mock_flags: MagicMock, mock_config: MagicMock) -> None:
        """Non-PDF mime types are never offloaded."""
        mock_flags.return_value = MagicMock(ephemeral_ingestion=True)
        mock_config.return_value = MagicMock(ingestion_backend="kubernetes")
        svc = self._make_service()
        should, count = svc._should_offload("text/html", b"<html>")
        assert should is False
        assert count == 0

    @patch(
        "meho_app.modules.knowledge.ingestion._get_pdf_page_count",
        return_value=30,
    )
    @patch("meho_app.modules.knowledge.ingestion.get_config")
    @patch("meho_app.modules.knowledge.ingestion.get_feature_flags")
    def test_below_threshold_returns_false(
        self, mock_flags: MagicMock, mock_config: MagicMock, mock_count: MagicMock
    ) -> None:
        """Documents below page threshold use existing path."""
        mock_flags.return_value = MagicMock(ephemeral_ingestion=True)
        mock_config.return_value = MagicMock(
            ingestion_backend="kubernetes",
            ingestion_offload_threshold_pages=50,
        )
        svc = self._make_service()
        should, count = svc._should_offload("application/pdf", b"fake")
        assert should is False
        assert count == 30

    @patch(
        "meho_app.modules.knowledge.ingestion._get_pdf_page_count",
        return_value=200,
    )
    @patch("meho_app.modules.knowledge.ingestion.get_config")
    @patch("meho_app.modules.knowledge.ingestion.get_feature_flags")
    def test_above_threshold_returns_true(
        self, mock_flags: MagicMock, mock_config: MagicMock, mock_count: MagicMock
    ) -> None:
        """Documents above page threshold should be offloaded."""
        mock_flags.return_value = MagicMock(ephemeral_ingestion=True)
        mock_config.return_value = MagicMock(
            ingestion_backend="kubernetes",
            ingestion_offload_threshold_pages=50,
        )
        svc = self._make_service()
        should, count = svc._should_offload("application/pdf", b"fake")
        assert should is True
        assert count == 200


# ---------------------------------------------------------------------------
# _import_arrow_chunks() tests
# ---------------------------------------------------------------------------


class TestImportArrowChunks:
    """Tests for importing Arrow IPC results into knowledge store."""

    def _make_service(self) -> MagicMock:
        """Create IngestionService with mocked repository."""
        from meho_app.modules.knowledge.ingestion import IngestionService

        svc = object.__new__(IngestionService)
        svc.knowledge_store = MagicMock()
        svc.object_storage = MagicMock()
        svc.job_repository = None
        svc.chunker = MagicMock()
        svc.docling_converter = MagicMock()
        svc.metadata_extractor = MagicMock()
        return svc

    def _make_arrow_table(self, n: int = 2) -> pa.Table:
        """Create an Arrow table with n test chunks."""
        chunks = []
        embeddings = []
        for i in range(n):
            chunks.append(
                (
                    f"Test chunk {i}",
                    {
                        "heading_stack": ["Chapter 1", f"Section {i}"],
                        "page_numbers": [i + 1],
                        "document_name": "test.pdf",
                        "chapter": "Chapter 1",
                        "section": f"Section {i}",
                        "subsection": None,
                        "content_type": "description",
                        "has_table": False,
                        "has_code_example": False,
                        "has_json_example": False,
                        "keywords": ["test"],
                        "resource_type": None,
                    },
                )
            )
            embeddings.append([float(i)] * 1024)

        arrow_bytes = serialize_chunks(chunks, embeddings)
        from meho_app.worker.arrow_codec import deserialize_chunks

        return deserialize_chunks(arrow_bytes)

    @pytest.mark.asyncio
    async def test_import_creates_chunks_with_embeddings(self) -> None:
        """Arrow import creates chunks with pre-computed embeddings."""
        svc = self._make_service()
        table = self._make_arrow_table(n=2)

        # Mock the repository's create_chunks_batch to return chunk IDs
        svc.knowledge_store.repository = MagicMock()
        svc.knowledge_store.repository.create_chunks_batch = AsyncMock(
            return_value=["chunk-id-1", "chunk-id-2"]
        )

        chunk_ids = await svc._import_arrow_chunks(
            table=table,
            tenant_id="tenant-1",
            connector_id="conn-1",
            user_id="user-1",
            roles=["reader"],
            groups=["ops"],
            tags=["test"],
            knowledge_type=None,
            priority=0,
            scope_type="instance",
            connector_type_scope=None,
            source_uri="s3://bucket/doc.pdf",
        )

        assert len(chunk_ids) == 2
        assert svc.knowledge_store.repository.create_chunks_batch.call_count == 1

        # Verify batch contains tuples of (chunk_create, embedding)
        batch_arg = svc.knowledge_store.repository.create_chunks_batch.call_args[0][0]
        assert len(batch_arg) == 2
        _chunk_create, embedding = batch_arg[0]
        assert embedding is not None
        assert len(embedding) == 1024

    @pytest.mark.asyncio
    async def test_import_uses_acl_from_request(self) -> None:
        """Arrow import preserves ACL context from original request."""
        svc = self._make_service()
        table = self._make_arrow_table(n=1)

        svc.knowledge_store.repository = MagicMock()
        svc.knowledge_store.repository.create_chunks_batch = AsyncMock(return_value=["chunk-abc"])

        await svc._import_arrow_chunks(
            table=table,
            tenant_id="tenant-99",
            connector_id="conn-55",
            user_id="user-77",
            roles=["admin"],
            groups=["engineering"],
            tags=["production"],
            knowledge_type=None,
            priority=5,
            scope_type="type",
            connector_type_scope="kubernetes",
            source_uri="s3://bucket/k8s-doc.pdf",
        )

        batch_arg = svc.knowledge_store.repository.create_chunks_batch.call_args[0][0]
        chunk_create, _embedding = batch_arg[0]
        assert chunk_create.tenant_id == "tenant-99"
        assert chunk_create.connector_id == "conn-55"
        assert chunk_create.scope_type == "type"
        assert chunk_create.connector_type_scope == "kubernetes"


# ---------------------------------------------------------------------------
# _dispatch_to_worker() tests
# ---------------------------------------------------------------------------


class TestDispatchToWorker:
    """Tests for the dispatch-to-worker flow."""

    def _make_service(self) -> MagicMock:
        """Create IngestionService with mocked dependencies."""
        from meho_app.modules.knowledge.ingestion import IngestionService

        svc = object.__new__(IngestionService)
        svc.knowledge_store = MagicMock()
        svc.knowledge_store.repository = MagicMock()
        svc.object_storage = MagicMock()
        svc.object_storage.bucket = "meho-knowledge"
        svc.job_repository = MagicMock()
        svc.job_repository.complete_job = AsyncMock()
        svc.chunker = MagicMock()
        svc.docling_converter = MagicMock()
        svc.metadata_extractor = MagicMock()
        return svc

    @pytest.mark.asyncio
    @patch("meho_app.worker.dispatcher.IngestionDispatcher")
    async def test_dispatch_generates_presigned_urls(self, mock_dispatcher_cls: MagicMock) -> None:
        """Dispatch path generates presigned URLs for worker access."""
        svc = self._make_service()
        svc.object_storage.generate_presigned_download_url = MagicMock(
            return_value="https://download.url"
        )
        svc.object_storage.generate_presigned_upload_url = MagicMock(
            return_value="https://upload.url"
        )

        # Mock dispatcher
        mock_instance = AsyncMock()
        mock_instance.dispatch.return_value = "exec-1"
        mock_instance.get_status.return_value = JobStatus(
            state=JobState.SUCCEEDED, execution_id="exec-1"
        )
        mock_dispatcher_cls.return_value = mock_instance

        # Mock Arrow results download and import
        chunks = [("chunk text", {"chapter": "Ch1", "section": "S1"})]
        embeddings = [[0.1] * 1024]
        arrow_bytes = serialize_chunks(chunks, embeddings)
        svc.object_storage.download_document = MagicMock(return_value=arrow_bytes)

        svc.knowledge_store.repository.create_chunks_batch = AsyncMock(return_value=["c-1"])

        svc._update_job_stage = AsyncMock()

        await svc._dispatch_to_worker(
            file_bytes=b"fakepdf",
            filename="test.pdf",
            mime_type="application/pdf",
            page_count=200,
            storage_key="documents/tenant/doc/test.pdf",
            job_id="job-1",
            tenant_id="tenant-1",
            connector_id="conn-1",
            user_id="user-1",
            roles=[],
            groups=[],
            tags=[],
            knowledge_type=None,
            priority=0,
            scope_type="instance",
            connector_type_scope=None,
            chunk_prefix="",
        )

        svc.object_storage.generate_presigned_download_url.assert_called_once()
        svc.object_storage.generate_presigned_upload_url.assert_called_once()

    @pytest.mark.asyncio
    @patch("meho_app.worker.dispatcher.IngestionDispatcher")
    async def test_dispatch_updates_job_stages(self, mock_dispatcher_cls: MagicMock) -> None:
        """Dispatch path updates job stages through EXTRACTING -> EMBEDDING -> STORING."""
        svc = self._make_service()
        svc.object_storage.generate_presigned_download_url = MagicMock(
            return_value="https://dl.url"
        )
        svc.object_storage.generate_presigned_upload_url = MagicMock(return_value="https://ul.url")

        mock_instance = AsyncMock()
        mock_instance.dispatch.return_value = "exec-2"
        mock_instance.get_status.return_value = JobStatus(
            state=JobState.SUCCEEDED, execution_id="exec-2"
        )
        mock_dispatcher_cls.return_value = mock_instance

        chunks = [("chunk", {})]
        embeddings = [[0.0] * 1024]
        arrow_bytes = serialize_chunks(chunks, embeddings)
        svc.object_storage.download_document = MagicMock(return_value=arrow_bytes)

        svc.knowledge_store.repository.create_chunks_batch = AsyncMock(return_value=["c-2"])

        svc._update_job_stage = AsyncMock()

        await svc._dispatch_to_worker(
            file_bytes=b"pdf",
            filename="doc.pdf",
            mime_type="application/pdf",
            page_count=100,
            storage_key="key",
            job_id="job-2",
            tenant_id=None,
            connector_id=None,
            user_id=None,
            roles=[],
            groups=[],
            tags=[],
            knowledge_type=None,
            priority=0,
            scope_type="global",
            connector_type_scope=None,
            chunk_prefix="",
        )

        # Should have called _update_job_stage at least for EXTRACTING and STORING
        stages_called = [
            call.kwargs.get("stage") or call.args[1]
            for call in svc._update_job_stage.call_args_list
        ]
        from meho_app.modules.knowledge.job_models import IngestionStage

        assert IngestionStage.EXTRACTING in stages_called
        assert IngestionStage.STORING in stages_called

    @pytest.mark.asyncio
    @patch("meho_app.worker.dispatcher.IngestionDispatcher")
    async def test_dispatch_failure_raises(self, mock_dispatcher_cls: MagicMock) -> None:
        """Dispatch raises ValueError when worker fails."""
        svc = self._make_service()
        svc.object_storage.generate_presigned_download_url = MagicMock(
            return_value="https://dl.url"
        )
        svc.object_storage.generate_presigned_upload_url = MagicMock(return_value="https://ul.url")

        mock_instance = AsyncMock()
        mock_instance.dispatch.return_value = "exec-3"
        mock_instance.get_status.return_value = JobStatus(
            state=JobState.FAILED,
            execution_id="exec-3",
            error_message="OOM killed",
        )
        mock_dispatcher_cls.return_value = mock_instance

        svc._update_job_stage = AsyncMock()

        with pytest.raises(ValueError, match="Worker failed"):
            await svc._dispatch_to_worker(
                file_bytes=b"pdf",
                filename="big.pdf",
                mime_type="application/pdf",
                page_count=500,
                storage_key="key",
                job_id="job-3",
                tenant_id=None,
                connector_id=None,
                user_id=None,
                roles=[],
                groups=[],
                tags=[],
                knowledge_type=None,
                priority=0,
                scope_type="global",
                connector_type_scope=None,
                chunk_prefix="",
            )


# ---------------------------------------------------------------------------
# Integration: ingest_document() routing
# ---------------------------------------------------------------------------


class TestIngestDocumentRouting:
    """Tests that ingest_document() correctly routes to ephemeral worker or existing path."""

    @pytest.mark.asyncio
    @patch("meho_app.modules.knowledge.ingestion.get_feature_flags")
    async def test_flag_off_uses_existing_path(self, mock_flags: MagicMock) -> None:
        """When feature flag is off, ingest_document uses existing subprocess path."""
        mock_flags.return_value = MagicMock(ephemeral_ingestion=False)

        from meho_app.modules.knowledge.ingestion import IngestionService

        svc = object.__new__(IngestionService)
        svc.knowledge_store = MagicMock()
        svc.object_storage = MagicMock()
        svc.object_storage.upload_document = MagicMock(return_value="s3://bucket/key")
        svc.job_repository = None
        svc.chunker = MagicMock()
        svc.docling_converter = MagicMock()
        svc.metadata_extractor = MagicMock()

        # The _should_offload should return False, so _dispatch_to_worker should NOT be called
        svc._should_offload = MagicMock(return_value=(False, 0))
        svc._dispatch_to_worker = AsyncMock()

        # Mock the existing path to avoid actual subprocess calls
        with (
            patch(
                "meho_app.modules.knowledge.ingestion.convert_file_in_subprocess",
                new_callable=AsyncMock,
            ) as mock_convert,
            patch(
                "meho_app.modules.knowledge.ingestion.generate_document_summary",
                new_callable=AsyncMock,
                return_value="summary",
            ),
            patch(
                "meho_app.modules.knowledge.ingestion.build_chunk_prefix",
                return_value="prefix",
            ),
            patch(
                "meho_app.modules.knowledge.ingestion._get_pdf_page_count",
                return_value=10,
            ),
        ):
            mock_doc = MagicMock()
            mock_convert.return_value = mock_doc
            svc.docling_converter.get_full_text = MagicMock(return_value="text")
            svc.docling_converter.chunk_document = MagicMock(
                return_value=[("chunk text", {"chapter": "Ch1"})]
            )
            from meho_app.modules.knowledge.schemas import ChunkMetadata

            svc.metadata_extractor.extract_metadata = MagicMock(return_value=ChunkMetadata())

            mock_chunk = MagicMock()
            mock_chunk.id = "cid-1"
            svc.knowledge_store.add_chunk = AsyncMock(return_value=mock_chunk)
            svc._resolve_connector_context = AsyncMock(return_value=(None, None))
            svc._update_job_stage = AsyncMock()

            with patch("meho_app.modules.knowledge.ingestion.get_config") as mock_cfg:
                mock_cfg.return_value = MagicMock(
                    ingestion_memory_limit_mb=8192,
                    ingestion_page_batch_size=50,
                    ingestion_ocr_enabled=False,
                )

                result = await svc.ingest_document(
                    file_bytes=b"data",
                    filename="small.pdf",
                    mime_type="application/pdf",
                )

            # _dispatch_to_worker should NOT have been called
            svc._dispatch_to_worker.assert_not_called()
            assert len(result) == 1
