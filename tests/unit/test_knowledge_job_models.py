# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""

Unit tests for meho_knowledge/job_models.py

Tests the IngestionJob model properties.
"""

import pytest
from meho_app.modules.knowledge.job_models import IngestionJob


class TestIngestionJobProperties:
    """Tests for IngestionJob computed properties"""

    def test_progress_percent_with_chunks(self):
        """Test progress percentage calculation with chunks"""
        job = IngestionJob(
            tenant_id="test-tenant",
            job_type="document",
            status="processing",
            total_chunks=100,
            chunks_processed=25,
        )

        assert job.progress_percent == pytest.approx(25.0)

    def test_progress_percent_zero_total(self):
        """Test progress percentage when total_chunks is zero"""
        job = IngestionJob(
            tenant_id="test-tenant",
            job_type="document",
            status="processing",
            total_chunks=0,
            chunks_processed=0,
        )

        assert job.progress_percent == pytest.approx(0.0)

    def test_progress_percent_none_total(self):
        """Test progress percentage when total_chunks is None"""
        job = IngestionJob(
            tenant_id="test-tenant",
            job_type="document",
            status="processing",
            total_chunks=None,
            chunks_processed=0,
        )

        assert job.progress_percent == pytest.approx(0.0)

    def test_progress_percent_complete(self):
        """Test progress percentage at 100%"""
        job = IngestionJob(
            tenant_id="test-tenant",
            job_type="document",
            status="completed",
            total_chunks=50,
            chunks_processed=50,
        )

        assert job.progress_percent == pytest.approx(100.0)

    def test_is_complete_completed_status(self):
        """Test is_complete returns True for completed status"""
        job = IngestionJob(tenant_id="test-tenant", job_type="document", status="completed")

        assert job.is_complete is True

    def test_is_complete_failed_status(self):
        """Test is_complete returns True for failed status"""
        job = IngestionJob(tenant_id="test-tenant", job_type="document", status="failed")

        assert job.is_complete is True

    def test_is_complete_pending_status(self):
        """Test is_complete returns False for pending status"""
        job = IngestionJob(tenant_id="test-tenant", job_type="document", status="pending")

        assert job.is_complete is False

    def test_is_complete_processing_status(self):
        """Test is_complete returns False for processing status"""
        job = IngestionJob(tenant_id="test-tenant", job_type="document", status="processing")

        assert job.is_complete is False

    def test_repr(self):
        """Test string representation of job"""
        job = IngestionJob(
            tenant_id="test-tenant",
            job_type="text",
            status="processing",
            total_chunks=100,
            chunks_processed=50,
        )

        repr_str = repr(job)

        assert "IngestionJob" in repr_str
        assert "type=text" in repr_str
        assert "status=processing" in repr_str
        assert "50/100" in repr_str  # Progress
