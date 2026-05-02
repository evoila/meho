# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for agent dependencies with real databases.

Note: Knowledge Service and OpenAPI Service components already have
comprehensive integration tests in their respective test files.
These tests focus on the MEHODependencies integration layer.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.modules.agents.dependencies import MEHODependencies
from meho_app.modules.connectors.repositories import ConnectorRepository
from meho_app.modules.connectors.schemas import ConnectorCreate


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_connectors_integration(db_session):
    """Test list_connectors with real connector repository"""
    # Create test connectors
    connector_repo = ConnectorRepository(db_session)

    await connector_repo.create_connector(
        ConnectorCreate(
            tenant_id="tenant-1",
            name="GitHub API",
            base_url="https://api.github.com",
            description="GitHub REST API",
            auth_type="API_KEY",
            credential_strategy="USER_PROVIDED",
        )
    )

    await connector_repo.create_connector(
        ConnectorCreate(
            tenant_id="tenant-1",
            name="ArgoCD API",
            base_url="https://argocd.example.com",
            description="ArgoCD API",
            auth_type="NONE",
            credential_strategy="SYSTEM",
        )
    )

    # Create dependencies
    user_context = UserContext(user_id="user-1", tenant_id="tenant-1")
    AsyncMock()

    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=connector_repo,
        endpoint_repo=Mock(),
        user_cred_repo=Mock(),
        http_client=Mock(),
        user_context=user_context,
    )

    # List connectors
    results = await deps.list_connectors()

    # Verify results
    assert len(results) == 2
    names = {r["name"] for r in results}
    assert "GitHub API" in names
    assert "ArgoCD API" in names

    github = next(r for r in results if r["name"] == "GitHub API")
    assert github["base_url"] == "https://api.github.com"
    assert github["description"] == "GitHub REST API"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_interpret_results_with_real_llm(db_session):
    """Test interpret_results with real OpenAI LLM call"""
    import os

    from dotenv import load_dotenv
    from openai import AsyncOpenAI

    # Load environment variables
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY not set")

    AsyncOpenAI(api_key=api_key)

    # Create dependencies
    user_context = UserContext(user_id="user-1", tenant_id="tenant-1")

    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=Mock(),
        endpoint_repo=Mock(),
        user_cred_repo=Mock(),
        http_client=Mock(),
        user_context=user_context,
    )

    # Test with realistic diagnostic scenario
    results = [
        {"text": "Application deployed on Kubernetes cluster prod-01"},
        {"data": {"pods_running": 3, "pods_failing": 1}},
        {"data": {"last_deployment": "2 hours ago", "status": "degraded"}},
    ]

    interpretation = await deps.interpret_results(
        context="Diagnosing why my-app is not responding",
        results=results,
        question="What could be causing the issue?",
    )

    # Verify we got a real response
    assert interpretation is not None
    assert len(interpretation) > 50  # Should be substantial analysis
    assert isinstance(interpretation, str)

    # Should mention relevant details (these are probabilistic, so we're lenient)
    # The LLM should analyze the data we provided
    print(f"\n=== LLM Interpretation ===\n{interpretation}\n")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_interpret_results_handles_empty_results(db_session):
    """Test interpret_results gracefully handles edge cases"""
    import os

    from dotenv import load_dotenv
    from openai import AsyncOpenAI

    # Load environment variables
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY not set")

    AsyncOpenAI(api_key=api_key)

    user_context = UserContext(user_id="user-1", tenant_id="tenant-1")

    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=Mock(),
        endpoint_repo=Mock(),
        user_cred_repo=Mock(),
        http_client=Mock(),
        user_context=user_context,
    )

    # Test with empty results
    interpretation = await deps.interpret_results(
        context="No data found", results=[], question="What should I do?"
    )

    # Should still provide useful response
    assert interpretation is not None
    assert len(interpretation) > 0


# Note: Additional integration tests for search_knowledge and call_endpoint
# rely on underlying services which already have comprehensive integration tests:
# - KnowledgeStore: test_vector_store_user_isolation.py (8 tests)
# - ConnectorRepository + EndpointDescriptorRepository: integration tests exist
# The unit tests cover the MEHODependencies integration logic with mocks.
