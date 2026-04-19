# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from meho_app.api.routes_knowledge import _build_document_response


def _job(**overrides):
    """Helper to fabricate a fake ingestion job object."""
    base = {
        "id": "123",
        "filename": "sample.pdf",
        "knowledge_type": "documentation",
        "status": "completed",
        "tags": ["vcf"],
        "file_size": 1024,
        "total_chunks": 5,
        "chunks_created": 5,
        "chunks_processed": 5,
        "error": None,
        "started_at": datetime.now(UTC) - timedelta(minutes=5),
        "completed_at": datetime.now(UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_document_response_with_preview():
    job = _job()
    response = _build_document_response(job, "Preview text")

    assert response.id == "123"
    assert response.filename == "sample.pdf"
    assert response.tags == ["vcf"]
    assert response.preview_text == "Preview text"
    assert response.chunks_created == 5
    assert response.status == "completed"


def test_build_document_response_handles_missing_tags():
    job = _job(tags=None)
    response = _build_document_response(job, None)

    assert response.tags == []
    assert response.preview_text is None
