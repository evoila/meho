# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Contract tests for BFF ↔ Knowledge Service schema compatibility.

These tests prevent schema mismatch bugs where the BFF expects fields
that the Knowledge service doesn't return, or vice versa.

Why: Session 43 bug - ChunkResponse was missing fields (knowledge_type,
priority, expires_at) causing 500 errors when BFF tried to construct responses.
"""

from datetime import UTC, datetime

import pytest


class TestChunkSchemaCompatibility:
    """Test that chunk schemas are compatible between BFF and Knowledge service"""

    def test_knowledge_chunk_response_has_all_bff_required_fields(self):
        """
        Verify Knowledge service ChunkResponse includes all fields needed by BFF.

        Prevents: BFF trying to access missing fields → KeyError → 500 error

        Bug prevented: Session 43 - BFF expected knowledge_type, priority, expires_at
        but ChunkResponse didn't include them.
        """
        from meho_app.api.routes_knowledge import KnowledgeChunkResponse as BFFChunkResponse
        from meho_app.modules.knowledge.api_schemas import ChunkResponse as KnowledgeChunkResponse

        # Get fields that BFF expects
        bff_fields = set(BFFChunkResponse.model_fields.keys())

        # Get fields that Knowledge service provides
        knowledge_fields = set(KnowledgeChunkResponse.model_fields.keys())

        # Knowledge service MUST provide all fields that BFF expects
        missing_in_knowledge = bff_fields - knowledge_fields

        assert not missing_in_knowledge, (
            f"Knowledge service ChunkResponse is missing fields that BFF expects: {missing_in_knowledge}. "
            f"This will cause KeyError when BFF tries to construct responses. "
            f"Add these fields to meho_knowledge/api_schemas.py:ChunkResponse"
        )

    def test_bff_can_construct_response_from_knowledge_data(self):
        """
        Test that BFF can actually construct its response from Knowledge service data.

        This simulates the actual transformation that happens in routes_knowledge.py.
        """
        from meho_app.api.routes_knowledge import KnowledgeChunkResponse as BFFChunkResponse
        from meho_app.modules.knowledge.api_schemas import ChunkResponse as KnowledgeChunkResponse

        # Create a sample Knowledge service response
        knowledge_chunk = KnowledgeChunkResponse(
            id="test-id",
            text="test text",
            tenant_id="test-tenant",
            system_id=None,
            user_id=None,
            roles=["admin"],
            groups=["ops"],
            tags=["test"],
            source_uri="job:123",
            knowledge_type="documentation",
            priority=5,
            expires_at=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        # Convert to dict (simulates HTTP JSON response)
        chunk_dict = knowledge_chunk.model_dump()

        # BFF should be able to construct its response from this data
        try:
            bff_chunk = BFFChunkResponse(
                id=chunk_dict.get("id"),
                text=chunk_dict.get("text"),
                tenant_id=chunk_dict.get("tenant_id"),
                system_id=chunk_dict.get("system_id"),
                user_id=chunk_dict.get("user_id"),
                roles=chunk_dict.get("roles", []),
                groups=chunk_dict.get("groups", []),
                tags=chunk_dict.get("tags", []),
                knowledge_type=chunk_dict.get("knowledge_type"),
                priority=chunk_dict.get("priority", 0),
                created_at=chunk_dict.get("created_at"),
                updated_at=chunk_dict.get("updated_at"),
                expires_at=chunk_dict.get("expires_at"),
                source_uri=chunk_dict.get("source_uri"),
            )

            # Verify construction succeeded
            assert bff_chunk.id == "test-id"
            assert bff_chunk.text == "test text"
            assert bff_chunk.knowledge_type == "documentation"
            assert bff_chunk.priority == 5

        except (KeyError, TypeError, ValueError) as e:
            pytest.fail(
                f"BFF cannot construct response from Knowledge service data: {e}. "
                f"This indicates a schema mismatch that will cause 500 errors in production."
            )


class TestDocumentSchemaCompatibility:
    """Test document/job schema compatibility between services"""

    def test_ingestion_job_id_is_serializable(self):
        """
        Test that IngestionJob.id can be serialized to JSON (string).

        Bug prevented: Session 43 - IngestionJob schema expected str but
        database returned UUID, causing validation errors.
        """
        from uuid import uuid4

        from meho_app.modules.knowledge.job_schemas import IngestionJob

        # The id field should accept UUID
        fields = IngestionJob.model_fields
        id_field = fields.get("id")

        assert id_field is not None, "IngestionJob must have id field"

        # Create an IngestionJob with UUID id
        from datetime import datetime

        job_data = {
            "id": uuid4(),  # UUID object (from database)
            "job_type": "document",
            "status": "completed",
            "tenant_id": "test",
            "filename": "test.pdf",
            "file_size": 1024,
            "knowledge_type": "documentation",
            "tags": [],
            "total_chunks": 10,
            "chunks_processed": 10,
            "chunks_created": 10,
            "chunk_ids": [],
            "error": None,
            "started_at": datetime.now(UTC),
            "completed_at": datetime.now(UTC),
        }

        try:
            job = IngestionJob(**job_data)

            # Should be able to serialize to JSON (converts UUID to str)
            json_data = job.model_dump(mode="json")

            # id should be a string in JSON
            assert isinstance(json_data["id"], str), (
                "IngestionJob.id must serialize to string for JSON. "
                "Add @field_serializer to convert UUID to str."
            )

        except Exception as e:
            pytest.fail(
                f"Failed to create/serialize IngestionJob with UUID id: {e}. "
                f"This will cause validation errors when returning jobs via API."
            )


class TestFilterSchemaCompleteness:
    """Test that filter schemas support all necessary operations"""

    def test_chunk_filter_supports_document_deletion(self):
        """
        Test that KnowledgeChunkFilter has source_uri for document deletion.

        Required for TASK-52: Deleting documents must delete associated chunks.
        """
        from meho_app.modules.knowledge.schemas import KnowledgeChunkFilter

        # Should be able to create filter with source_uri
        filter_obj = KnowledgeChunkFilter(source_uri="job:test-123", limit=1000)

        assert filter_obj.source_uri == "job:test-123", (
            "KnowledgeChunkFilter must support source_uri filtering for document deletion"
        )
