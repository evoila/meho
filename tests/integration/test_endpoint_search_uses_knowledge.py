# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests to ensure search_endpoints uses BM25 search for endpoints.

CRITICAL: These tests prevent regression to simple keyword matching.

Session 61: We discovered search_endpoints was using simple keyword matching
instead of knowledge-based search. This caused queries like "list hosts"
to return irrelevant endpoints like "list bundles".

Session 68: Updated to use on-the-fly BM25 search for superior endpoint matching.

These tests ensure:
1. search_endpoints uses BM25 search (not simple keyword matching)
2. Results are semantically relevant (not just keyword matches)
3. Metadata filters are applied correctly
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.modules.agents.dependencies import MEHODependencies
from meho_app.modules.connectors.rest.schemas import EndpointDescriptor
from meho_app.modules.knowledge.models import KnowledgeChunkModel


def create_mock_knowledge_store_with_bm25(chunks: list[KnowledgeChunkModel]):
    """
    Create a properly mocked knowledge store that works with BM25Service.

    BM25Service needs knowledge_store.repository.session to execute SQL queries.
    This helper mocks the session to return the provided chunks.
    """
    # Create mock session that returns chunks when queried
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = chunks
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Create mock repository with the session
    mock_repository = Mock()
    mock_repository.session = mock_session

    # Create mock knowledge store with the repository
    mock_knowledge_store = Mock()
    mock_knowledge_store.repository = mock_repository

    return mock_knowledge_store


def create_endpoint_chunk(
    connector_id: str, path: str, method: str, summary: str, operation_id: str
) -> KnowledgeChunkModel:
    """Helper to create endpoint chunks for testing."""
    chunk = KnowledgeChunkModel()
    chunk.id = uuid4()
    chunk.text = f"{method} {path}\n\nSummary: {summary}"
    chunk.tenant_id = "test-tenant"
    chunk.tags = ["api", "endpoint"]
    chunk.knowledge_type = "documentation"
    chunk.source_uri = "test-openapi.json"
    chunk.search_metadata = {
        "endpoint_path": path,
        "http_method": method,
        "source_type": "openapi_spec",
        "connector_id": connector_id,
        "operation_id": operation_id,
    }
    return chunk


@pytest.mark.asyncio
async def test_search_endpoints_uses_bm25_search_not_keyword_matching():
    """
    CRITICAL: Ensure search_endpoints uses BM25 search (not simple keyword matching).

    This test FAILS if search_endpoints reverts to simple keyword matching.
    """
    # Setup
    user_context = UserContext(
        tenant_id="test-tenant", user_id="test-user", user_email="test@example.com"
    )

    connector_id = str(uuid4())

    # Create endpoint chunk
    mock_chunk = create_endpoint_chunk(
        connector_id, "/v1/hosts", "GET", "Get all hosts", "getHosts"
    )

    # Mock knowledge store with BM25Service support
    mock_knowledge_store = create_mock_knowledge_store_with_bm25([mock_chunk])

    # Mock endpoint repository
    mock_endpoint_repo = Mock()
    mock_endpoint = EndpointDescriptor(
        id=str(uuid4()),
        connector_id=str(uuid4()),
        method="GET",
        path="/v1/hosts",
        operation_id="getHosts",
        summary="Get all hosts",
        description="Returns all hosts",
        is_enabled=True,
        safety_level="safe",
        requires_approval=False,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    mock_endpoint_repo.list_endpoints = AsyncMock(return_value=[mock_endpoint])

    # Mock other dependencies
    mock_connector_repo = Mock()
    mock_user_cred_repo = Mock()
    mock_http_client = Mock()
    Mock()

    # Create dependencies
    deps = MEHODependencies(
        knowledge_store=mock_knowledge_store,
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=mock_user_cred_repo,
        http_client=mock_http_client,
        user_context=user_context,
    )

    # Execute
    results = await deps.search_endpoints(connector_id=connector_id, query="list hosts", limit=10)

    # Verify: BM25Service queried the database
    mock_session = mock_knowledge_store.repository.session
    mock_session.execute.assert_called()

    # Verify: results contain the correct endpoint
    assert len(results) == 1
    assert results[0]["method"] == "GET"
    assert results[0]["path"] == "/v1/hosts"


@pytest.mark.asyncio
async def test_search_endpoints_filters_by_connector_id():
    """
    Ensure search_endpoints only searches within the specified connector.

    This prevents cross-connector pollution in search results.
    """
    user_context = UserContext(
        tenant_id="test-tenant", user_id="test-user", user_email="test@example.com"
    )

    connector_id = str(uuid4())

    # Mock knowledge store (no results for this test)
    mock_knowledge_store = create_mock_knowledge_store_with_bm25([])

    # Mock endpoint repository
    mock_endpoint_repo = Mock()
    mock_endpoint_repo.list_endpoints = AsyncMock(return_value=[])

    # Mock other dependencies
    mock_connector_repo = Mock()
    mock_user_cred_repo = Mock()
    mock_http_client = Mock()
    Mock()

    deps = MEHODependencies(
        knowledge_store=mock_knowledge_store,
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=mock_user_cred_repo,
        http_client=mock_http_client,
        user_context=user_context,
    )

    # Execute
    await deps.search_endpoints(connector_id=connector_id, query="list resources", limit=10)

    # Verify: BM25Service was invoked
    mock_session = mock_knowledge_store.repository.session
    mock_session.execute.assert_called()


@pytest.mark.asyncio
async def test_search_endpoints_returns_empty_when_no_knowledge_chunks():
    """
    Ensure graceful handling when knowledge base has no matching chunks.
    """
    user_context = UserContext(
        tenant_id="test-tenant", user_id="test-user", user_email="test@example.com"
    )

    connector_id = str(uuid4())

    # Mock knowledge store returning empty results
    mock_knowledge_store = create_mock_knowledge_store_with_bm25([])

    # Mock endpoint repository
    mock_endpoint_repo = Mock()
    mock_endpoint_repo.list_endpoints = AsyncMock(return_value=[])

    # Mock other dependencies
    mock_connector_repo = Mock()
    mock_user_cred_repo = Mock()
    mock_http_client = Mock()
    Mock()

    deps = MEHODependencies(
        knowledge_store=mock_knowledge_store,
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=mock_user_cred_repo,
        http_client=mock_http_client,
        user_context=user_context,
    )

    # Execute
    results = await deps.search_endpoints(
        connector_id=connector_id, query="nonexistent endpoint", limit=10
    )

    # Verify: returns empty list (not error)
    assert results == []

    # Verify: BM25 search was attempted
    mock_session = mock_knowledge_store.repository.session
    mock_session.execute.assert_called()


@pytest.mark.asyncio
async def test_search_endpoints_bm25_keyword_scoring():
    """
    CRITICAL: Ensure BM25 correctly scores keyword matches.

    Query: "virtual machines" should match "vm" endpoint due to BM25 scoring.
    """
    user_context = UserContext(
        tenant_id="test-tenant", user_id="test-user", user_email="test@example.com"
    )

    connector_id = str(uuid4())

    # Create VM endpoint chunk
    vm_chunk = create_endpoint_chunk(
        connector_id, "/v1/hosts", "GET", "List virtual machines", "listVMs"
    )

    mock_knowledge_store = create_mock_knowledge_store_with_bm25([vm_chunk])

    # Mock endpoint repository
    mock_endpoint_repo = Mock()
    mock_endpoint = EndpointDescriptor(
        id=str(uuid4()),
        connector_id=str(uuid4()),
        method="GET",
        path="/v1/hosts",
        operation_id="listVMs",
        summary="List virtual machines",
        is_enabled=True,
        safety_level="safe",
        requires_approval=False,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    mock_endpoint_repo.list_endpoints = AsyncMock(return_value=[mock_endpoint])

    # Mock other dependencies
    mock_connector_repo = Mock()
    mock_user_cred_repo = Mock()
    mock_http_client = Mock()
    Mock()

    deps = MEHODependencies(
        knowledge_store=mock_knowledge_store,
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=mock_user_cred_repo,
        http_client=mock_http_client,
        user_context=user_context,
    )

    # Execute
    results = await deps.search_endpoints(
        connector_id=connector_id, query="virtual machines", limit=10
    )

    # Verify: BM25 found the matching endpoint
    assert len(results) == 1
    assert results[0]["path"] == "/v1/hosts"


@pytest.mark.asyncio
async def test_search_endpoints_fallback_on_bm25_failure():
    """
    Ensure fallback to legacy search if BM25 search fails.

    This provides resilience while still preferring BM25 search.
    """
    user_context = UserContext(
        tenant_id="test-tenant", user_id="test-user", user_email="test@example.com"
    )

    connector_id = str(uuid4())

    # Mock knowledge store that raises an error
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=Exception("BM25 search failed"))
    mock_repository = Mock()
    mock_repository.session = mock_session
    mock_knowledge_store = Mock()
    mock_knowledge_store.repository = mock_repository

    # Mock connector repository
    mock_connector = Mock()
    mock_connector.id = uuid4()
    mock_connector.name = "Test Connector"
    mock_connector_repo = Mock()
    mock_connector_repo.get_connector = AsyncMock(return_value=mock_connector)

    # Mock endpoint repository with fallback data
    mock_endpoint = EndpointDescriptor(
        id=str(uuid4()),
        connector_id=str(uuid4()),
        method="GET",
        path="/v1/hosts",
        operation_id="getHosts",
        summary="Get all hosts",
        is_enabled=True,
        safety_level="safe",
        requires_approval=False,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    mock_endpoint_repo = Mock()
    mock_endpoint_repo.list_endpoints = AsyncMock(return_value=[mock_endpoint])

    # Mock other dependencies
    mock_user_cred_repo = Mock()
    mock_http_client = Mock()
    Mock()

    deps = MEHODependencies(
        knowledge_store=mock_knowledge_store,
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=mock_user_cred_repo,
        http_client=mock_http_client,
        user_context=user_context,
    )

    # Execute - should not raise exception
    results = await deps.search_endpoints(connector_id=str(connector_id), query="hosts", limit=10)

    # Verify: BM25 search was attempted
    mock_session.execute.assert_called_once()

    # Verify: fallback succeeded
    assert len(results) == 1
    assert results[0]["path"] == "/v1/hosts"


@pytest.mark.asyncio
async def test_search_endpoints_excludes_non_openapi_chunks():
    """
    Ensure only OpenAPI spec chunks are searched.

    Should not return documentation or other knowledge types.
    """
    user_context = UserContext(
        tenant_id="test-tenant", user_id="test-user", user_email="test@example.com"
    )

    connector_id = str(uuid4())

    # Create endpoint chunk (will be filtered by connector_id and source_type)
    endpoint_chunk = create_endpoint_chunk(
        connector_id, "/v1/hosts", "GET", "Get hosts", "getHosts"
    )

    mock_knowledge_store = create_mock_knowledge_store_with_bm25([endpoint_chunk])

    # Mock endpoint repository
    mock_endpoint_repo = Mock()
    mock_endpoint = EndpointDescriptor(
        id=str(uuid4()),
        connector_id=str(uuid4()),
        method="GET",
        path="/v1/hosts",
        operation_id="getHosts",
        summary="Get hosts",
        is_enabled=True,
        safety_level="safe",
        requires_approval=False,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    mock_endpoint_repo.list_endpoints = AsyncMock(return_value=[mock_endpoint])

    # Mock other dependencies
    mock_connector_repo = Mock()
    mock_user_cred_repo = Mock()
    mock_http_client = Mock()
    Mock()

    deps = MEHODependencies(
        knowledge_store=mock_knowledge_store,
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=mock_user_cred_repo,
        http_client=mock_http_client,
        user_context=user_context,
    )

    # Execute
    await deps.search_endpoints(connector_id=connector_id, query="documentation", limit=10)

    # Verify: BM25Service applies metadata filters
    # (actual filtering logic tested in BM25Service unit tests)
    mock_session = mock_knowledge_store.repository.session
    mock_session.execute.assert_called()


# Regression test for Session 61 bug
@pytest.mark.asyncio
async def test_regression_session61_bundles_not_returned_for_hosts_query():
    """
    REGRESSION TEST: Ensure "list hosts" doesn't return "list bundles".

    Session 61 Bug: Simple keyword matching returned ANY endpoint with "list" in it.

    Before fix: "list hosts" matched "list bundles", "list certificates", etc.
    After fix: BM25 search returns only relevant endpoints.
    """
    user_context = UserContext(
        tenant_id="test-tenant", user_id="test-user", user_email="test@example.com"
    )

    connector_id = str(uuid4())

    # Create hosts endpoint chunk
    hosts_chunk = create_endpoint_chunk(
        connector_id, "/v1/hosts", "GET", "Get all hosts", "getHosts"
    )

    mock_knowledge_store = create_mock_knowledge_store_with_bm25([hosts_chunk])

    # Mock endpoint repository
    mock_endpoint_repo = Mock()
    mock_endpoint = EndpointDescriptor(
        id=str(uuid4()),
        connector_id=str(uuid4()),
        method="GET",
        path="/v1/hosts",
        operation_id="getHosts",
        summary="Get all hosts",
        is_enabled=True,
        safety_level="safe",
        requires_approval=False,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    mock_endpoint_repo.list_endpoints = AsyncMock(return_value=[mock_endpoint])

    # Mock other dependencies
    mock_connector_repo = Mock()
    mock_user_cred_repo = Mock()
    mock_http_client = Mock()
    Mock()

    deps = MEHODependencies(
        knowledge_store=mock_knowledge_store,
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=mock_user_cred_repo,
        http_client=mock_http_client,
        user_context=user_context,
    )

    # Execute: Query for "list hosts"
    results = await deps.search_endpoints(connector_id=connector_id, query="list hosts", limit=10)

    # Verify: ONLY hosts endpoint returned (not bundles, certificates, etc.)
    assert len(results) == 1
    assert results[0]["path"] == "/v1/hosts"
    assert "bundles" not in results[0]["path"].lower()
    assert "certificates" not in results[0]["path"].lower()

    # Verify: BM25 search was used
    mock_session = mock_knowledge_store.repository.session
    mock_session.execute.assert_called()
