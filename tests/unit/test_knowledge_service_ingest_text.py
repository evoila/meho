# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Regression test: KnowledgeService.ingest_text forwards expires_at.

The BFF route `/api/knowledge/ingest-text` calls KnowledgeService, which
delegates to IngestionService. Prior to this fix the wrapper dropped
`expires_at` and called a non-existent KnowledgeStore.ingest_text method.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.service import KnowledgeService


def _service_with_mocked_store() -> KnowledgeService:
    svc = KnowledgeService.__new__(KnowledgeService)
    svc.store = MagicMock(spec=KnowledgeStore)
    svc.session = MagicMock()
    svc.repository = MagicMock()
    svc.embedding_provider = MagicMock()
    svc.hybrid_search = MagicMock()
    return svc


@pytest.mark.asyncio
async def test_ingest_text_forwards_expires_at_to_ingestion_service() -> None:
    svc = _service_with_mocked_store()
    fake_ingestion = MagicMock()
    fake_ingestion.ingest_text = AsyncMock(return_value=["chunk-1", "chunk-2"])
    expires = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)

    with (
        patch(
            "meho_app.modules.knowledge.ingestion.IngestionService",
            return_value=fake_ingestion,
        ),
        patch("meho_app.modules.knowledge.object_storage.ObjectStorage"),
        patch("meho_app.modules.knowledge.job_repository.IngestionJobRepository"),
    ):
        result = await svc.ingest_text(
            text="Marathon tomorrow, all streets closed 6 AM - 6 PM",
            tenant_id="tenant-42",
            connector_id="conn-7",
            knowledge_type="event",
            priority=50,
            expires_at=expires,
        )

    fake_ingestion.ingest_text.assert_awaited_once()
    kwargs = fake_ingestion.ingest_text.await_args.kwargs
    assert kwargs["expires_at"] == expires
    assert kwargs["knowledge_type"] == "event"
    assert kwargs["tenant_id"] == "tenant-42"
    assert result == {"chunk_ids": ["chunk-1", "chunk-2"], "status": "completed"}


@pytest.mark.asyncio
async def test_ingest_text_defaults_expires_at_to_none() -> None:
    svc = _service_with_mocked_store()
    fake_ingestion = MagicMock()
    fake_ingestion.ingest_text = AsyncMock(return_value=["chunk-1"])

    with (
        patch(
            "meho_app.modules.knowledge.ingestion.IngestionService",
            return_value=fake_ingestion,
        ),
        patch("meho_app.modules.knowledge.object_storage.ObjectStorage"),
        patch("meho_app.modules.knowledge.job_repository.IngestionJobRepository"),
    ):
        await svc.ingest_text(
            text="Lesson learned: always double-check rollout status.",
            tenant_id="tenant-42",
            knowledge_type="procedure",
        )

    kwargs = fake_ingestion.ingest_text.await_args.kwargs
    assert kwargs["expires_at"] is None
