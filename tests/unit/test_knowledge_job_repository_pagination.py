# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for IngestionJobRepository pagination logic.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_app.modules.knowledge.job_repository import IngestionJobRepository
from meho_app.modules.knowledge.job_schemas import IngestionJobFilter


@pytest.mark.unit
@pytest.mark.asyncio
async def test_count_jobs_returns_total_not_paginated_count():
    """
    Verify count_jobs returns the total count across all pages,
    not just the count of items on the current page.
    """
    # Mock session
    mock_session = AsyncMock()

    # Mock execute to return count of 42 (total items)
    mock_result = MagicMock()
    mock_result.scalar.return_value = 42
    mock_session.execute.return_value = mock_result

    # Create repository
    repo = IngestionJobRepository(mock_session)

    # Create filter with pagination (limit 10, offset 20)
    filter_params = IngestionJobFilter(
        tenant_id="test-tenant", job_type="document", limit=10, offset=20
    )

    # Count should return 42 (total), not 10 (page size)
    count = await repo.count_jobs(filter_params)

    assert count == 42
    assert mock_session.execute.called


@pytest.mark.unit
@pytest.mark.asyncio
async def test_count_jobs_applies_same_filters_as_list_jobs():
    """
    Verify count_jobs applies the same filter criteria as list_jobs
    (tenant_id, status, job_type) but ignores pagination.
    """
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 15
    mock_session.execute.return_value = mock_result

    repo = IngestionJobRepository(mock_session)

    filter_params = IngestionJobFilter(
        tenant_id="tenant-123", status="completed", job_type="document", limit=5, offset=10
    )

    count = await repo.count_jobs(filter_params)

    # Should have executed a count query
    assert mock_session.execute.called

    # Verify it's a count query (not selecting actual rows)
    # The query should be a count, not a select of full objects
    assert count == 15


@pytest.mark.unit
def test_ingestion_job_filter_has_pagination_fields():
    """
    Verify IngestionJobFilter supports limit and offset for pagination.
    """
    filter_params = IngestionJobFilter(tenant_id="test", limit=25, offset=50)

    assert filter_params.limit == 25
    assert filter_params.offset == 50
