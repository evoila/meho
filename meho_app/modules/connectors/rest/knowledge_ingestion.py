# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
OpenAPI to Knowledge Ingestion

Converts OpenAPI specification endpoints into searchable knowledge chunks.
This enables natural language discovery of API endpoints.

Example:
    User: "Get all resources from API"
    → Searches knowledge base
    → Finds: GET /api/v1/resources endpoint
"""

from typing import Any

from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.modules.connectors.rest.spec_parser import OpenAPIParser
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.schemas import ChunkMetadata, KnowledgeChunkCreate, KnowledgeType

logger = get_logger(__name__)


async def ingest_openapi_to_knowledge(
    spec_dict: dict[str, Any],
    connector_id: str,
    connector_name: str,
    knowledge_store: KnowledgeStore,
    user_context: UserContext,
) -> int:
    """
    Convert OpenAPI spec endpoints into searchable knowledge chunks.

    For each endpoint in the spec:
    1. Create rich text description (method, path, summary, params, response)
    2. Add metadata (connector_id, endpoint_id, method, path)
    3. Tag appropriately (api, endpoint, + operation tags)
    4. Store in knowledge base with embeddings

    Args:
        spec_dict: Parsed OpenAPI specification dictionary
        connector_id: UUID of the connector this spec belongs to
        connector_name: Human-readable name of the connector
        knowledge_store: Knowledge store instance for ingestion
        user_context: User context for ACL

    Returns:
        Number of endpoints ingested as knowledge chunks

    Example:
        >>> spec = yaml.safe_load(open("my-api.yaml"))
        >>> count = await ingest_openapi_to_knowledge(
        ...     spec_dict=spec,
        ...     connector_id="abc-123",
        ...     connector_name="My API Connector",
        ...     knowledge_store=store,
        ...     user_context=user
        ... )
        >>> print(f"Ingested {count} endpoints")
        Ingested 50 endpoints
    """
    parser = OpenAPIParser()

    # Validate spec
    try:
        parser.validate_spec(spec_dict)
    except ValueError as e:
        logger.warning(
            f"Invalid OpenAPI spec for connector {connector_name}: {e}, skipping ingestion"
        )
        return 0

    # Extract endpoints
    endpoints = parser.extract_endpoints(spec_dict)

    if not endpoints:
        logger.warning(f"No endpoints found in OpenAPI spec for {connector_name}")
        return 0

    logger.info(f"Ingesting {len(endpoints)} endpoints from {connector_name} to knowledge base")

    # Convert each endpoint to a knowledge chunk
    chunks_created = 0

    for endpoint in endpoints:
        try:
            # Create rich text description
            text = _format_endpoint_as_text(endpoint, connector_name)

            # Create tags
            tags = _create_tags(endpoint, connector_name)

            # Create metadata with API-specific fields
            search_metadata = _create_search_metadata(endpoint, connector_id)

            # Create chunk
            chunk_create = KnowledgeChunkCreate(
                text=text,
                tenant_id=user_context.tenant_id,
                connector_id=str(connector_id),
                tags=tags,
                knowledge_type=KnowledgeType.DOCUMENTATION,
                priority=5,
                search_metadata=search_metadata,
                source_uri=f"openapi://{connector_id}/{endpoint.get('operation_id', 'unknown')}",
            )

            # Add to knowledge store
            await knowledge_store.add_chunk(chunk_create)
            chunks_created += 1

        except Exception as e:
            logger.error(
                f"Failed to ingest endpoint {endpoint.get('method')} {endpoint.get('path')}: {e}"
            )
            continue

    logger.info(
        f"✅ Successfully ingested {chunks_created} endpoint chunks "
        f"from {connector_name} (connector_id: {connector_id})"
    )

    return chunks_created


def _format_endpoint_as_text(endpoint: dict[str, Any], connector_name: str) -> str:
    """
    Format endpoint information as rich searchable text.

    Creates a comprehensive description that includes:
    - HTTP method and path
    - Summary and description
    - Required and optional parameters
    - Response schema (abbreviated)
    - Tags

    This text is what gets embedded and searched semantically.
    """
    method = endpoint["method"]
    path = endpoint["path"]
    summary = endpoint.get("summary", "No summary provided")
    description = endpoint.get("description", "")
    tags = endpoint.get("tags", [])
    required_params = endpoint.get("required_params", [])

    # Build the text description
    parts = []

    # Header: Method and path
    parts.append(f"{method} {path}")
    parts.append("")  # Blank line

    # Connector context
    parts.append(f"Connector: {connector_name}")
    parts.append("")

    # Summary
    parts.append(f"Summary: {summary}")

    # Description (if different from summary)
    if description and description != summary:
        parts.append(f"Description: {description}")

    parts.append("")

    # Parameters
    if required_params:
        parts.append("Required Parameters:")
        for param in required_params:
            if param != "body":  # Body is special
                parts.append(f"  - {param}")
        parts.append("")

    # Response (human-readable summary)
    response_schema = endpoint.get("response_schema", {})
    if response_schema:
        parts.append("Returns:")
        summary = _summarize_schema(response_schema)
        parts.append(f"  {summary}")
        parts.append("")

    # Tags
    if tags:
        parts.append(f"Tags: {', '.join(tags)}")

    # Operation ID (for searchability)
    if endpoint.get("operation_id"):
        parts.append(f"Operation: {endpoint['operation_id']}")

    # SEARCH KEYWORDS: Add abbreviations, synonyms, and path terms
    # This improves BM25 matching for common abbreviations (VM, K8s, API, etc.)
    search_keywords = _generate_search_keywords(endpoint, summary, tags)
    if search_keywords:
        parts.append("")
        parts.append(f"Search: {search_keywords}")

    return "\n".join(parts)


def _generate_search_keywords(endpoint: dict[str, Any], summary: str, tags: list[str]) -> str:
    """
    Generate search-optimized keywords for better BM25 matching.

    Session 80: Adds abbreviations, synonyms, and repeated path terms
    to improve discoverability with common search patterns.

    Makes queries like "list VMs" match "virtual machines" text.

    Args:
        endpoint: Endpoint data
        summary: Endpoint summary text
        tags: Endpoint tags

    Returns:
        Space-separated search keywords

    Examples:
        >>> _generate_search_keywords(
        ...     {"path": "/api/v1/resources", "method": "GET"},
        ...     "Returns information about resources",
        ...     ["resource"]
        ... )
        "resource resources list-resource get-resource all-resources"
    """
    keywords = set()
    path = endpoint.get("path", "")
    method = endpoint.get("method", "GET")
    endpoint.get("operation_id", "")

    # Extract resource name from path (last non-param segment)
    # /api/v1/items → "items"
    # /api/namespaces/{ns}/resources → "resources"
    path_parts = [p for p in path.split("/") if p and not p.startswith("{")]
    if path_parts:
        resource = path_parts[-1].rstrip("s")  # Singular form
        keywords.add(resource)
        keywords.add(resource + "s")  # Plural form

        # Common abbreviations (generic patterns)
        abbrevs = _get_common_abbreviations(resource, summary, tags)
        keywords.update(abbrevs)

    # Add operation patterns based on HTTP method
    if path_parts and method == "GET" and path.count("{") == 0:
        # Collection endpoint (no params) - add list patterns
        # IMPORTANT: Add BOTH hyphenated and space-separated versions
        # so BM25 can match both "list VMs" and "list-vm"
        resource = path_parts[-1]
        # Hyphenated (for exact match)
        keywords.add(f"list-{resource}")
        keywords.add(f"get-all-{resource}")
        keywords.add(f"all-{resource}")
        # Space-separated (for token matching!) - CRITICAL for BM25
        keywords.add("list")
        keywords.add("all")
        keywords.add("get")
        keywords.add(resource)

    return " ".join(sorted(keywords))


def _get_common_abbreviations(resource: str, summary: str, tags: list[str]) -> set:
    """
    Get common abbreviations for a resource term.

    Generic approach - checks summary/tags for hints about abbreviations.

    Args:
        resource: Resource name (e.g., "vm", "pod")
        summary: Endpoint summary
        tags: Endpoint tags

    Returns:
        Set of common abbreviations
    """
    abbrevs = set()
    summary_lower = summary.lower()
    tags_lower = [t.lower() for t in tags]

    # If tags or summary mention abbreviations, include them
    summary_lower + " ".join(tags_lower)

    # Look for parenthetical abbreviations: "virtual machine (VM)"
    import re

    parens = re.findall(r"\(([A-Z][A-Z0-9]{1,5})\)", " ".join(tags))
    abbrevs.update([p.lower() for p in parens])

    return abbrevs


def _summarize_schema(
    schema: dict[str, Any], max_depth: int = 3, current_depth: int = 0
) -> str:  # NOSONAR (cognitive complexity)
    """
    Create a concise, human-readable summary of a JSON schema.

    Instead of dumping verbose JSON, creates natural language descriptions like:
    - "Array of cluster objects with id, name, status, domain, capacity"
    - "Object containing user details (email, name, roles)"
    - "String value"

    This is much more useful for LLM-based endpoint discovery than raw JSON.

    Args:
        schema: JSON Schema object
        max_depth: Maximum nesting depth to describe
        current_depth: Current recursion depth

    Returns:
        Human-readable schema summary

    Examples:
        >>> _summarize_schema({"type": "string"})
        "String value"

        >>> _summarize_schema({"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}})
        "Array of objects with id"
    """
    if current_depth >= max_depth:
        return "nested structure"

    schema_type = schema.get("type", "unknown")

    if schema_type == "object":
        properties = schema.get("properties", {})
        if not properties:
            return "Object"

        # List property names and types
        prop_summaries = []
        for prop_name, prop_schema in list(properties.items())[:8]:  # Limit to first 8 properties
            prop_type = prop_schema.get("type", "unknown")
            if prop_type == "object":
                prop_summaries.append(f"{prop_name} (object)")
            elif prop_type == "array":
                prop_summaries.append(f"{prop_name} (array)")
            else:
                prop_summaries.append(prop_name)

        if len(properties) > 8:
            prop_summaries.append(f"...+{len(properties) - 8} more")

        return f"Object with {', '.join(prop_summaries)}"

    elif schema_type == "array":
        items_schema = schema.get("items", {})
        items_summary = _summarize_schema(items_schema, max_depth, current_depth + 1)
        return f"Array of {items_summary}"

    elif schema_type == "string":
        enum_values = schema.get("enum")
        if enum_values:
            return f"String (one of: {', '.join(str(v) for v in enum_values[:3])})"
        return "String"

    elif schema_type == "integer":
        return "Integer number"

    elif schema_type == "number":
        return "Numeric value"

    elif schema_type == "boolean":
        return "Boolean (true/false)"

    else:
        return schema_type or "value"


def _create_tags(endpoint: dict[str, Any], connector_name: str) -> list[str]:
    """
    Create searchable tags for the endpoint.

    Tags help with keyword-based search (BM25) and filtering.
    """
    tags = set()

    # Always include these
    tags.add("api")
    tags.add("endpoint")

    # Add connector name (lowercase, split words)
    connector_words = connector_name.lower().replace("-", " ").split()
    tags.update(connector_words)

    # Add OpenAPI tags
    for tag in endpoint.get("tags", []):
        tags.add(tag.lower())

    # Add method
    tags.add(endpoint["method"].lower())

    # Add path components (without slashes)
    path = endpoint["path"]
    path_parts = [p for p in path.split("/") if p and not p.startswith("{")]
    tags.update([p.lower() for p in path_parts])

    return sorted(tags)


def _create_search_metadata(endpoint: dict[str, Any], connector_id: str) -> ChunkMetadata:
    """
    Create ChunkMetadata for filtering and retrieval.

    Uses existing ChunkMetadata fields where possible,
    and adds custom fields via extra="allow" config.

    Standard fields used:
    - endpoint_path: The API path
    - http_method: GET, POST, etc.
    - resource_type: Extracted from path or tags

    Custom fields (via extra="allow"):
    - connector_id: For retrieving connector details
    - endpoint_id: For calling the endpoint
    - source_type: Identifies this as OpenAPI documentation
    """
    # Determine resource type from path or tags
    path = endpoint["path"]
    resource_type = None

    # Extract resource from path (e.g., /api/v1/clusters → clusters)
    path_parts = [p for p in path.split("/") if p and not p.startswith("{")]
    if path_parts:
        resource_type = path_parts[-1]  # Last non-variable part

    # Build metadata using ChunkMetadata schema
    metadata = ChunkMetadata(
        # Standard API fields
        endpoint_path=path,
        http_method=endpoint["method"],
        resource_type=resource_type,
        # Content classification
        has_json_example=bool(endpoint.get("response_schema")),
        # Keywords for BM25
        keywords=[*endpoint.get("tags", []), resource_type]
        if resource_type
        else endpoint.get("tags", []),
        # Custom fields (allowed via extra="allow")
        **{
            "source_type": "openapi_spec",
            "connector_id": connector_id,
            "endpoint_id": endpoint.get(
                "operation_id", f"{endpoint['method']}_{path.replace('/', '_')}"
            ),
            "operation_id": endpoint.get("operation_id"),
            "has_required_params": bool(endpoint.get("required_params")),
            "required_params": endpoint.get("required_params", []),
        },
    )

    return metadata


async def remove_connector_knowledge(
    connector_id: str, knowledge_store: KnowledgeStore, user_context: UserContext
) -> int:
    """
    Remove all knowledge chunks associated with a connector.

    Call this when a connector is deleted to clean up its endpoint documentation.
    Also called before re-ingesting OpenAPI spec to prevent duplicates.

    Args:
        connector_id: UUID of the connector to remove
        knowledge_store: Knowledge store instance
        user_context: User context for ACL

    Returns:
        Number of chunks deleted

    Example:
        >>> deleted = await remove_connector_knowledge("abc-123", store, user)
        >>> print(f"Deleted {deleted} chunks")
        Deleted 766 chunks
    """
    from sqlalchemy import delete, func, select

    from meho_app.modules.knowledge.models import KnowledgeChunkModel

    # First, count how many we'll delete (for logging)
    count_stmt = (
        select(func.count())
        .select_from(KnowledgeChunkModel)
        .where(
            KnowledgeChunkModel.tenant_id == user_context.tenant_id,
            KnowledgeChunkModel.search_metadata["connector_id"].astext == connector_id,
        )
    )
    count_result = await knowledge_store.repository.session.execute(count_stmt)
    deleted_count = count_result.scalar() or 0

    # Delete all chunks where search_metadata.connector_id matches
    stmt = delete(KnowledgeChunkModel).where(
        KnowledgeChunkModel.tenant_id == user_context.tenant_id,
        KnowledgeChunkModel.search_metadata["connector_id"].astext == connector_id,
    )

    await knowledge_store.repository.session.execute(stmt)

    logger.info(f"Deleted {deleted_count} knowledge chunks for connector {connector_id}")

    return deleted_count
