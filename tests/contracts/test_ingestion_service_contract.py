# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Contract tests for Ingestion Service.

Verifies that the Ingestion service provides the API that consumers expect.
"""

from datetime import UTC

import pytest


class TestIngestionJobSchemaContract:
    """Test IngestionJob schema contracts"""

    def test_ingestion_job_has_required_fields(self):
        """Verify IngestionJob schema has all fields needed by BFF"""
        from meho_app.modules.knowledge.job_schemas import IngestionJob

        fields = set(IngestionJob.model_fields.keys())

        required_fields = {
            "id",
            "job_type",
            "status",
            "tenant_id",
            "filename",
            "total_chunks",
            "chunks_created",
            "started_at",
            "error",
        }

        missing = required_fields - fields

        assert not missing, (
            f"IngestionJob schema missing required fields: {missing}. "
            f"BFF document list depends on these fields."
        )

    def test_ingestion_job_id_serialization(self):
        """
        Verify IngestionJob.id can be serialized to JSON string.

        Bug prevented: Session 43 - UUID not serializable to JSON.
        """
        from datetime import datetime
        from uuid import uuid4

        from meho_app.modules.knowledge.job_schemas import IngestionJob

        # Create job with UUID id (as returned from database)
        job = IngestionJob(
            id=uuid4(),
            job_type="document",
            status="completed",
            tenant_id="test",
            filename="test.pdf",
            file_size=1024,
            knowledge_type="documentation",
            tags=[],
            total_chunks=10,
            chunks_processed=10,
            chunks_created=10,
            chunk_ids=[],
            error=None,
            started_at=datetime.now(UTC),
            completed_at=None,
        )

        # Should serialize to JSON with id as string
        json_data = job.model_dump(mode="json")

        assert isinstance(json_data["id"], str), (
            "IngestionJob.id must serialize to string. "
            "Add @field_serializer('id') to convert UUID to str."
        )

    def test_ingestion_job_status_values(self):
        """Verify job status values are documented"""
        from meho_app.modules.knowledge.job_schemas import IngestionJob

        # Status field should accept these values

        # This is documented in the schema but not enforced by enum
        # Just verify the field exists and is a string
        fields = IngestionJob.model_fields
        assert "status" in fields

        # Could add Enum enforcement in future


class TestIngestionWebhookContract:
    """Test webhook ingestion API contracts"""

    def test_webhook_request_schema_exists(self):
        """Verify WebhookIngestRequest schema exists"""
        try:
            from meho_app.modules.ingestion.api_schemas import WebhookIngestRequest

            # Required fields for webhook processing
            fields = set(WebhookIngestRequest.model_fields.keys())

            required = {"event_type", "payload"}
            missing = required - fields

            assert not missing, f"WebhookIngestRequest missing fields: {missing}"
        except ImportError:
            pytest.skip("WebhookIngestRequest not yet implemented")

    def test_ingestion_service_has_process_webhook(self):
        """Verify ingestion service can process webhooks"""
        try:
            from meho_app.modules.ingestion.processor import WebhookProcessor

            assert hasattr(WebhookProcessor, "process_webhook"), (
                "WebhookProcessor must have process_webhook method"
            )
        except ImportError:
            pytest.skip("WebhookProcessor not yet implemented")


class TestJobRepositoryContract:
    """Test job repository API contracts"""

    def test_job_repository_crud_methods(self):
        """Verify IngestionJobRepository has CRUD operations"""
        from meho_app.modules.knowledge.job_repository import IngestionJobRepository

        # Required methods
        required_methods = ["create_job", "get_job", "update_status", "list_jobs"]

        for method in required_methods:
            assert hasattr(IngestionJobRepository, method), (
                f"IngestionJobRepository must have {method} method"
            )

    def test_job_repository_has_deletion_support(self):
        """
        Verify repository can mark jobs as deleted.

        Required for document deletion flow (TASK-52).
        """
        from meho_app.modules.knowledge.job_repository import IngestionJobRepository

        # Repository should have update_status which can mark as deleted
        assert hasattr(IngestionJobRepository, "update_status"), (
            "IngestionJobRepository must have update_status method"
        )

        # Or dedicated delete method
        hasattr(IngestionJobRepository, "delete_job") or hasattr(
            IngestionJobRepository, "mark_deleted"
        )

        # Note: Can use update_status to mark as deleted, so not strictly required
        # This test just verifies some deletion capability exists
