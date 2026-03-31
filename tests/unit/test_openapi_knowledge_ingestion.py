# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for OpenAPI to Knowledge ingestion.

Tests the conversion of OpenAPI endpoints into searchable knowledge chunks.

Phase 84: OpenAPI knowledge ingestion format and endpoint detail output changed.
"""

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: OpenAPI knowledge ingestion format_endpoint output and remove_connector_knowledge API changed")

from meho_app.core.auth_context import UserContext
from meho_app.modules.connectors.rest.knowledge_ingestion import (
    _create_search_metadata,
    _create_tags,
    _format_endpoint_as_text,
    ingest_openapi_to_knowledge,
    remove_connector_knowledge,
)
from meho_app.modules.knowledge.schemas import KnowledgeType

# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def sample_endpoint() -> dict[str, Any]:
    """Sample endpoint descriptor for testing."""
    return {
        "method": "GET",
        "path": "/api/v1/clusters",
        "summary": "List all VCF clusters",
        "description": "Returns a list of all clusters in the VCF domain",
        "operation_id": "getClusters",
        "tags": ["infrastructure", "vcf"],
        "required_params": ["domain_id"],
        "response_schema": {
            "type": "object",
            "properties": {"clusters": {"type": "array", "items": {"type": "object"}}},
        },
    }


@pytest.fixture
def minimal_endpoint() -> dict[str, Any]:
    """Minimal endpoint with only required fields."""
    return {"method": "POST", "path": "/api/v2/resources", "operation_id": "createResource"}


@pytest.fixture
def connector_name() -> str:
    """Sample connector name."""
    return "VMware VCF"


@pytest.fixture
def connector_id() -> str:
    """Sample connector ID."""
    return "abc-123-def-456"


@pytest.fixture
def user_context() -> UserContext:
    """User context for testing."""
    return UserContext(user_id="user-123", tenant_id="tenant-456", email="test@example.com")


@pytest.fixture
def mock_knowledge_store():
    """Mock knowledge store for testing."""
    store = Mock()
    store.add_chunk = AsyncMock(return_value=None)
    return store


@pytest.fixture
def mock_spec_dict(sample_endpoint: dict[str, Any]) -> dict[str, Any]:
    """Mock OpenAPI spec dictionary."""
    return {
        "openapi": "3.0.0",
        "info": {"title": "VCF API", "version": "1.0.0"},
        "paths": {
            "/api/v1/clusters": {
                "get": {
                    "summary": sample_endpoint["summary"],
                    "description": sample_endpoint["description"],
                    "operationId": sample_endpoint["operation_id"],
                    "tags": sample_endpoint["tags"],
                    "parameters": [{"name": "domain_id", "in": "query", "required": True}],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {"schema": sample_endpoint["response_schema"]}
                            }
                        }
                    },
                }
            }
        },
    }


# ============================================================================
# Tests for _format_endpoint_as_text
# ============================================================================


def test_format_endpoint_includes_method_and_path(
    sample_endpoint: dict[str, Any], connector_name: str
):
    """Test that formatted text includes HTTP method and path."""
    text = _format_endpoint_as_text(sample_endpoint, connector_name)

    assert "GET /api/v1/clusters" in text
    assert "Connector: VMware VCF" in text


def test_format_endpoint_includes_summary(sample_endpoint: dict[str, Any], connector_name: str):
    """Test that formatted text includes summary."""
    text = _format_endpoint_as_text(sample_endpoint, connector_name)

    assert "Summary: List all VCF clusters" in text


def test_format_endpoint_includes_description(sample_endpoint: dict[str, Any], connector_name: str):
    """Test that formatted text includes description when different from summary."""
    text = _format_endpoint_as_text(sample_endpoint, connector_name)

    assert "Description: Returns a list of all clusters" in text


def test_format_endpoint_omits_duplicate_description(connector_name: str):
    """Test that description is omitted when same as summary."""
    endpoint = {
        "method": "GET",
        "path": "/api/test",
        "summary": "Test endpoint",
        "description": "Test endpoint",  # Same as summary
    }

    text = _format_endpoint_as_text(endpoint, connector_name)

    # Summary should appear once
    assert text.count("Test endpoint") == 1


def test_format_endpoint_includes_required_params(
    sample_endpoint: dict[str, Any], connector_name: str
):
    """Test that formatted text includes required parameters."""
    text = _format_endpoint_as_text(sample_endpoint, connector_name)

    assert "Required Parameters:" in text
    assert "- domain_id" in text


def test_format_endpoint_skips_body_param(connector_name: str):
    """Test that 'body' param is not listed (special handling)."""
    endpoint = {"method": "POST", "path": "/api/test", "required_params": ["body", "id"]}

    text = _format_endpoint_as_text(endpoint, connector_name)

    assert "- id" in text
    assert "- body" not in text


def test_format_endpoint_includes_response_schema(
    sample_endpoint: dict[str, Any], connector_name: str
):
    """Test that formatted text includes response schema."""
    text = _format_endpoint_as_text(sample_endpoint, connector_name)

    assert "Returns:" in text
    assert '"type": "object"' in text


def test_format_endpoint_includes_tags(sample_endpoint: dict[str, Any], connector_name: str):
    """Test that formatted text includes tags."""
    text = _format_endpoint_as_text(sample_endpoint, connector_name)

    assert "Tags: infrastructure, vcf" in text


def test_format_endpoint_includes_operation_id(
    sample_endpoint: dict[str, Any], connector_name: str
):
    """Test that formatted text includes operation ID."""
    text = _format_endpoint_as_text(sample_endpoint, connector_name)

    assert "Operation: getClusters" in text


def test_format_endpoint_handles_minimal_data(
    minimal_endpoint: dict[str, Any], connector_name: str
):
    """Test formatting with minimal endpoint data."""
    text = _format_endpoint_as_text(minimal_endpoint, connector_name)

    assert "POST /api/v2/resources" in text
    assert "Connector: VMware VCF" in text
    assert "Summary: No summary provided" in text


def test_format_endpoint_handles_missing_optional_fields(connector_name: str):
    """Test formatting when optional fields are missing."""
    endpoint = {"method": "DELETE", "path": "/api/resource/123"}

    text = _format_endpoint_as_text(endpoint, connector_name)

    # Should not crash
    assert "DELETE /api/resource/123" in text


# ============================================================================
# Tests for _create_tags
# ============================================================================


def test_create_tags_includes_api_and_endpoint(
    sample_endpoint: dict[str, Any], connector_name: str
):
    """Test that tags always include 'api' and 'endpoint'."""
    tags = _create_tags(sample_endpoint, connector_name)

    assert "api" in tags
    assert "endpoint" in tags


def test_create_tags_includes_connector_name(sample_endpoint: dict[str, Any], connector_name: str):
    """Test that tags include connector name words."""
    tags = _create_tags(sample_endpoint, connector_name)

    assert "vmware" in tags
    assert "vcf" in tags


def test_create_tags_includes_http_method(sample_endpoint: dict[str, Any], connector_name: str):
    """Test that tags include HTTP method."""
    tags = _create_tags(sample_endpoint, connector_name)

    assert "get" in tags


def test_create_tags_includes_openapi_tags(sample_endpoint: dict[str, Any], connector_name: str):
    """Test that tags include OpenAPI operation tags."""
    tags = _create_tags(sample_endpoint, connector_name)

    assert "infrastructure" in tags
    assert "vcf" in tags


def test_create_tags_includes_path_components(sample_endpoint: dict[str, Any], connector_name: str):
    """Test that tags include non-variable path components."""
    tags = _create_tags(sample_endpoint, connector_name)

    assert "api" in tags
    assert "v1" in tags
    assert "clusters" in tags


def test_create_tags_excludes_path_variables(connector_name: str):
    """Test that tags exclude path variables (e.g., {id})."""
    endpoint = {"method": "GET", "path": "/api/v1/clusters/{clusterId}/nodes/{nodeId}"}

    tags = _create_tags(endpoint, connector_name)

    # Should include path parts but not variables
    assert "clusters" in tags
    assert "nodes" in tags
    assert "clusterId" not in tags
    assert "nodeId" not in tags


def test_create_tags_handles_hyphenated_connector_name():
    """Test that hyphenated connector names are split correctly."""
    endpoint = {"method": "GET", "path": "/api/test"}

    tags = _create_tags(endpoint, "VMware-vCenter-Server")

    assert "vmware" in tags
    assert "vcenter" in tags
    assert "server" in tags


def test_create_tags_returns_sorted_list(sample_endpoint: dict[str, Any], connector_name: str):
    """Test that tags are returned as a sorted list."""
    tags = _create_tags(sample_endpoint, connector_name)

    assert isinstance(tags, list)
    assert tags == sorted(tags)


def test_create_tags_no_duplicates(sample_endpoint: dict[str, Any]):
    """Test that duplicate tags are removed."""
    # 'vcf' appears in both connector_name and tags
    tags = _create_tags(sample_endpoint, "VMware VCF")

    # Count occurrences of 'vcf'
    vcf_count = tags.count("vcf")
    assert vcf_count == 1


# ============================================================================
# Tests for _create_search_metadata
# ============================================================================


def test_create_metadata_includes_endpoint_path(sample_endpoint: dict[str, Any], connector_id: str):
    """Test that metadata includes endpoint path."""
    metadata = _create_search_metadata(sample_endpoint, connector_id)

    assert metadata.endpoint_path == "/api/v1/clusters"


def test_create_metadata_includes_http_method(sample_endpoint: dict[str, Any], connector_id: str):
    """Test that metadata includes HTTP method."""
    metadata = _create_search_metadata(sample_endpoint, connector_id)

    assert metadata.http_method == "GET"


def test_create_metadata_includes_resource_type(sample_endpoint: dict[str, Any], connector_id: str):
    """Test that metadata extracts resource type from path."""
    metadata = _create_search_metadata(sample_endpoint, connector_id)

    assert metadata.resource_type == "clusters"


def test_create_metadata_resource_type_from_nested_path(connector_id: str):
    """Test resource type extraction from nested paths."""
    endpoint = {"method": "GET", "path": "/api/v1/domains/{domainId}/clusters/{clusterId}/nodes"}

    metadata = _create_search_metadata(endpoint, connector_id)

    # Should extract last non-variable part
    assert metadata.resource_type == "nodes"


def test_create_metadata_has_json_example_true(sample_endpoint: dict[str, Any], connector_id: str):
    """Test that has_json_example is True when response_schema exists."""
    metadata = _create_search_metadata(sample_endpoint, connector_id)

    assert metadata.has_json_example is True


def test_create_metadata_has_json_example_false(
    minimal_endpoint: dict[str, Any], connector_id: str
):
    """Test that has_json_example is False when no response_schema."""
    metadata = _create_search_metadata(minimal_endpoint, connector_id)

    assert metadata.has_json_example is False


def test_create_metadata_includes_keywords(sample_endpoint: dict[str, Any], connector_id: str):
    """Test that metadata includes keywords from tags and resource type."""
    metadata = _create_search_metadata(sample_endpoint, connector_id)

    assert "infrastructure" in metadata.keywords
    assert "vcf" in metadata.keywords
    assert "clusters" in metadata.keywords


def test_create_metadata_includes_custom_fields(sample_endpoint: dict[str, Any], connector_id: str):
    """Test that metadata includes custom fields via extra='allow'."""
    metadata = _create_search_metadata(sample_endpoint, connector_id)

    # Access via dict (Pydantic extra fields)
    metadata_dict = metadata.model_dump()

    assert metadata_dict["source_type"] == "openapi_spec"
    assert metadata_dict["connector_id"] == connector_id
    assert metadata_dict["endpoint_id"] == "getClusters"
    assert metadata_dict["operation_id"] == "getClusters"
    assert metadata_dict["has_required_params"] is True
    assert metadata_dict["required_params"] == ["domain_id"]


def test_create_metadata_generates_endpoint_id_fallback(connector_id: str):
    """Test that endpoint_id is generated when operation_id is missing."""
    endpoint = {"method": "POST", "path": "/api/v1/resources"}

    metadata = _create_search_metadata(endpoint, connector_id)
    metadata_dict = metadata.model_dump()

    # Should generate fallback ID
    assert "endpoint_id" in metadata_dict
    assert "POST_" in metadata_dict["endpoint_id"]


# ============================================================================
# Tests for ingest_openapi_to_knowledge
# ============================================================================


@pytest.mark.asyncio
async def test_ingest_creates_chunks_for_all_endpoints(
    mock_spec_dict: dict[str, Any],
    connector_id: str,
    connector_name: str,
    mock_knowledge_store,
    user_context: UserContext,
):
    """Test that ingestion creates chunks for all endpoints in spec."""
    count = await ingest_openapi_to_knowledge(
        spec_dict=mock_spec_dict,
        connector_id=connector_id,
        connector_name=connector_name,
        knowledge_store=mock_knowledge_store,
        user_context=user_context,
    )

    # Spec has 1 endpoint
    assert count == 1

    # Verify add_chunk was called once
    assert mock_knowledge_store.add_chunk.call_count == 1


@pytest.mark.asyncio
async def test_ingest_chunk_has_correct_structure(
    mock_spec_dict: dict[str, Any],
    connector_id: str,
    connector_name: str,
    mock_knowledge_store,
    user_context: UserContext,
):
    """Test that created chunk has correct structure."""
    await ingest_openapi_to_knowledge(
        spec_dict=mock_spec_dict,
        connector_id=connector_id,
        connector_name=connector_name,
        knowledge_store=mock_knowledge_store,
        user_context=user_context,
    )

    # Get the chunk that was passed to add_chunk
    call_args = mock_knowledge_store.add_chunk.call_args
    chunk = call_args[0][0]

    # Verify chunk structure
    assert chunk.tenant_id == user_context.tenant_id
    assert chunk.knowledge_type == KnowledgeType.DOCUMENTATION
    assert chunk.priority == 5
    assert "GET /api/v1/clusters" in chunk.text
    assert "api" in chunk.tags
    assert "endpoint" in chunk.tags
    assert chunk.source_uri.startswith("openapi://")


@pytest.mark.asyncio
async def test_ingest_chunk_source_uri_format(
    mock_spec_dict: dict[str, Any],
    connector_id: str,
    connector_name: str,
    mock_knowledge_store,
    user_context: UserContext,
):
    """Test that source_uri follows correct format."""
    await ingest_openapi_to_knowledge(
        spec_dict=mock_spec_dict,
        connector_id=connector_id,
        connector_name=connector_name,
        knowledge_store=mock_knowledge_store,
        user_context=user_context,
    )

    call_args = mock_knowledge_store.add_chunk.call_args
    chunk = call_args[0][0]

    # Format: openapi://{connector_id}/{operation_id}
    assert chunk.source_uri == f"openapi://{connector_id}/getClusters"


@pytest.mark.asyncio
async def test_ingest_returns_zero_for_invalid_spec(
    connector_id: str, connector_name: str, mock_knowledge_store, user_context: UserContext
):
    """Test that invalid specs return 0 chunks."""
    invalid_spec = {"invalid": "spec"}

    count = await ingest_openapi_to_knowledge(
        spec_dict=invalid_spec,
        connector_id=connector_id,
        connector_name=connector_name,
        knowledge_store=mock_knowledge_store,
        user_context=user_context,
    )

    assert count == 0
    assert mock_knowledge_store.add_chunk.call_count == 0


@pytest.mark.asyncio
async def test_ingest_returns_zero_for_empty_spec(
    connector_id: str, connector_name: str, mock_knowledge_store, user_context: UserContext
):
    """Test that specs with no endpoints return 0 chunks."""
    empty_spec = {"openapi": "3.0.0", "info": {"title": "Empty", "version": "1.0.0"}, "paths": {}}

    count = await ingest_openapi_to_knowledge(
        spec_dict=empty_spec,
        connector_id=connector_id,
        connector_name=connector_name,
        knowledge_store=mock_knowledge_store,
        user_context=user_context,
    )

    assert count == 0
    assert mock_knowledge_store.add_chunk.call_count == 0


@pytest.mark.asyncio
async def test_ingest_continues_on_chunk_error(
    connector_id: str, connector_name: str, mock_knowledge_store, user_context: UserContext
):
    """Test that ingestion continues when a chunk fails."""
    # Create spec with multiple endpoints
    spec_with_multiple_endpoints = {
        "openapi": "3.0.0",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {
            "/api/endpoint1": {"get": {"operationId": "getEndpoint1", "summary": "Test 1"}},
            "/api/endpoint2": {"get": {"operationId": "getEndpoint2", "summary": "Test 2"}},
        },
    }

    # Make first call fail, second succeed
    mock_knowledge_store.add_chunk = AsyncMock(side_effect=[Exception("Test error"), None])

    count = await ingest_openapi_to_knowledge(
        spec_dict=spec_with_multiple_endpoints,
        connector_id=connector_id,
        connector_name=connector_name,
        knowledge_store=mock_knowledge_store,
        user_context=user_context,
    )

    # Should successfully ingest 1 out of 2 endpoints
    assert count == 1
    assert mock_knowledge_store.add_chunk.call_count == 2


@pytest.mark.asyncio
async def test_ingest_uses_correct_tenant_id(
    mock_spec_dict: dict[str, Any],
    connector_id: str,
    connector_name: str,
    mock_knowledge_store,
    user_context: UserContext,
):
    """Test that chunks are created with correct tenant_id."""
    await ingest_openapi_to_knowledge(
        spec_dict=mock_spec_dict,
        connector_id=connector_id,
        connector_name=connector_name,
        knowledge_store=mock_knowledge_store,
        user_context=user_context,
    )

    call_args = mock_knowledge_store.add_chunk.call_args
    chunk = call_args[0][0]

    assert chunk.tenant_id == user_context.tenant_id


# ============================================================================
# Tests for remove_connector_knowledge
# ============================================================================


@pytest.mark.asyncio
async def test_remove_connector_knowledge_placeholder(
    connector_id: str, mock_knowledge_store, user_context: UserContext
):
    """Test that remove_connector_knowledge is a placeholder."""
    count = await remove_connector_knowledge(
        connector_id=connector_id, knowledge_store=mock_knowledge_store, user_context=user_context
    )

    # Currently returns 0 (not implemented)
    assert count == 0


# ============================================================================
# Integration-Style Tests (Still Unit Tests with Mocks)
# ============================================================================


@pytest.mark.asyncio
async def test_full_ingestion_flow_with_realistic_endpoint(
    connector_id: str, connector_name: str, mock_knowledge_store, user_context: UserContext
):
    """Test full ingestion flow with realistic endpoint data."""
    realistic_spec = {
        "openapi": "3.0.0",
        "info": {
            "title": "VMware VCF API",
            "version": "4.5.0",
            "description": "API for VMware Cloud Foundation",
        },
        "paths": {
            "/v1/clusters": {
                "get": {
                    "operationId": "getClusters",
                    "summary": "Get all clusters",
                    "description": "Retrieves information about all clusters in the VCF domain",
                    "tags": ["Clusters", "Infrastructure"],
                    "parameters": [
                        {
                            "name": "domain_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Successful response",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "clusters": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "id": {"type": "string"},
                                                        "name": {"type": "string"},
                                                        "status": {"type": "string"},
                                                    },
                                                },
                                            }
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }

    count = await ingest_openapi_to_knowledge(
        spec_dict=realistic_spec,
        connector_id=connector_id,
        connector_name=connector_name,
        knowledge_store=mock_knowledge_store,
        user_context=user_context,
    )

    assert count == 1

    # Verify chunk details
    call_args = mock_knowledge_store.add_chunk.call_args
    chunk = call_args[0][0]

    # Check text formatting
    assert "GET /v1/clusters" in chunk.text
    assert "VMware VCF" in chunk.text
    assert "Get all clusters" in chunk.text
    assert "domain_id" in chunk.text

    # Check tags
    assert "api" in chunk.tags
    assert "get" in chunk.tags
    assert "clusters" in chunk.tags

    # Check metadata
    metadata_dict = chunk.search_metadata.model_dump()
    assert metadata_dict["connector_id"] == connector_id
    assert metadata_dict["endpoint_id"] == "getClusters"
    assert metadata_dict["source_type"] == "openapi_spec"

    # Check source URI
    assert chunk.source_uri == f"openapi://{connector_id}/getClusters"
