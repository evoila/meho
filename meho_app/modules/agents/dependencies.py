# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Agent dependencies - provides tools and resources for PydanticAI agents.

This module defines the MEHODependencies class which holds all the tools
and resources that agents need to execute workflows.
"""
# mypy: disable-error-code="no-untyped-def,var-annotated,arg-type,return,no-any-return"

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from redis.asyncio import Redis

from meho_app.core.auth_context import UserContext
from meho_app.core.errors import LLMError
from meho_app.core.otel import get_logger
from meho_app.modules.agents.agent_factories import AgentManager
from meho_app.modules.agents.execution.json_extraction import (
    extract_preferred_keywords,
    extract_verbatim_snippet,
    requires_verbatim_example,
)
from meho_app.modules.agents.execution.schema_helpers import (
    format_optional_params,
    format_required_params,
    generate_usage_example,
    summarize_request_body_schema,
    summarize_response_schema,
)
from meho_app.modules.agents.execution.search_utils import (
    boost_code_containing_chunks as _boost_code_containing_chunks,
)
from meho_app.modules.agents.execution.search_utils import (
    build_metadata_filters as _build_metadata_filters,
)
from meho_app.modules.agents.execution.search_utils import (
    detect_metadata_filters as _detect_metadata_filters,
)
from meho_app.modules.agents.execution.search_utils import (
    estimate_size as _estimate_size,
)
from meho_app.modules.agents.execution.search_utils import (
    format_result as _format_result,
)
from meho_app.modules.agents.execution.search_utils import (
    is_example_request as _is_example_request,
)
from meho_app.modules.agents.output_schemas import ConnectorDetermination, DataSummary
from meho_app.modules.agents.session_state import AgentSessionState
from meho_app.modules.connectors.repositories import ConnectorRepository
from meho_app.modules.connectors.repositories.credential_repository import UserCredentialRepository
from meho_app.modules.connectors.rest.http_client import GenericHTTPClient
from meho_app.modules.connectors.rest.repository import EndpointDescriptorRepository
from meho_app.modules.connectors.rest.schemas import EndpointFilter
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

logger = get_logger(__name__)


@dataclass
class MEHODependencies:
    """
    Dependencies for MEHO agents.

    Provides access to:
    - Knowledge store for semantic search
    - OpenAPI connectors and endpoints
    - User credentials for external systems
    - HTTP client for API calls
    - PydanticAI agents for LLM tasks (via AgentManager)
    - Session state for conversation context and memory
    - Approval store for dangerous operation approval
    - Usage limits (optional, for controlling LLM usage)

    Note: All LLM interactions use PydanticAI for type safety and consistency.
    """

    # Core services
    knowledge_store: KnowledgeStore
    connector_repo: ConnectorRepository
    endpoint_repo: EndpointDescriptorRepository
    user_cred_repo: UserCredentialRepository
    http_client: GenericHTTPClient

    # User context
    user_context: UserContext

    # Database session (for topology and other direct operations)
    db_session: Any | None = field(default=None)  # AsyncSession

    # Session state (conversation memory)
    session_state: AgentSessionState = field(default_factory=AgentSessionState)

    # Current user question (for data reduction context)
    current_question: str = field(default="")

    # Session ID for approval flow
    session_id: str | None = field(default=None)

    # Approval store for dangerous operations
    approval_store: Any | None = field(default=None)  # ApprovalStore

    # Redis (for caching)
    redis: Redis | None = field(default=None)

    # Usage limits (optional - for E2E tests or production control)
    usage_limits: Any = field(default=None)  # UsageLimits or None to disable

    # Automation identity (Phase 74)
    session_type: str = field(default="interactive")  # "interactive", "automated_event", "automated_scheduler"
    created_by_user_id: str | None = field(default=None)  # JWT user_id of event/task creator
    allowed_connector_ids: list[str] | None = field(default=None)  # null = all connectors
    trigger_type: str | None = field(default=None)  # "event" or "scheduler"
    trigger_id: str | None = field(default=None)  # registration_id or task_id
    delegation_active: bool = field(default=True)  # current delegation_active flag from trigger model
    delegation_flag_callback: Any = field(default=None)  # DelegationFlagCallback or None

    # Phase 75: notification targets for approval alerts
    notification_targets: list[dict[str, str]] | None = field(default=None)

    # =========================================================================
    # Agent Access (via AgentManager)
    # =========================================================================

    def _get_classifier_agent(self) -> Agent:
        """Get the connector classifier agent (via AgentManager)."""
        return AgentManager.get_classifier_agent()

    def _get_interpreter_agent(self) -> Agent:
        """Get the results interpreter agent (via AgentManager)."""
        return AgentManager.get_interpreter_agent()

    def _get_data_extractor_agent(self) -> Agent:
        """Get the data extraction agent (via AgentManager)."""
        return AgentManager.get_data_extractor_agent()

    def detect_metadata_filters(self, query: str) -> dict[str, Any] | None:
        """Detect metadata filters to apply based on query intent."""
        return _detect_metadata_filters(query)

    async def search_apis(
        self,
        query: str | None = None,
        queries: str | list[str] | None = None,
        top_k: int = 15,
        score_threshold: float = 0.7,
    ) -> list[dict[str, Any]]:
        """
        Search ONLY OpenAPI specification chunks (API endpoints, parameters, schemas).

        Use this when you need to find specific API endpoints to call.
        Only returns chunks from OpenAPI specs, filtering out general documentation.

        Args:
            query: Single search query (backward compatible)
            queries: Single query or list of queries for batch search (preferred)
            top_k: Maximum number of results per query
            score_threshold: Starting similarity score (will auto-decrease if no results)

        Returns:
            List of OpenAPI spec chunks (endpoints, parameters, schemas)
        """
        return await self._search_with_filter(
            query=query,
            queries=queries,
            top_k=top_k,
            score_threshold=score_threshold,
            metadata_filter={"source_type": "openapi_spec"},
        )

    async def search_docs(
        self,
        query: str | None = None,
        queries: str | list[str] | None = None,
        top_k: int = 15,
        score_threshold: float = 0.7,
        connector_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search ONLY documentation chunks (concepts, architecture, procedures).

        Use this when you need conceptual understanding or troubleshooting guides.
        Filters out OpenAPI specs AND connector operations, returning only general documentation.

        Args:
            query: Single search query (backward compatible)
            queries: Single query or list of queries for batch search (preferred)
            top_k: Maximum number of results per query
            score_threshold: Starting similarity score (will auto-decrease if no results)
            connector_id: If provided, scope search to this connector only (specialist agent)

        Returns:
            List of documentation chunks (excluding API specs and connector operations)
        """
        # Exclude both openapi_spec AND connector_operation source types
        return await self._search_with_filter(
            query=query,
            queries=queries,
            top_k=top_k,
            score_threshold=score_threshold,
            exclude_metadata={"source_type": ["openapi_spec", "connector_operation"]},
            connector_id=connector_id,
        )

    async def search_knowledge(
        self,
        query: str | None = None,
        queries: str | list[str] | None = None,
        top_k: int = 15,  # Focused results - provides relevant context without overwhelming
        score_threshold: float = 0.7,  # Start with high quality
        connector_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search ALL knowledge base content (APIs + documentation).

        Combines:
        - BM25: Exact keyword matching (great for endpoints, constants, role names like "ADMIN")
        - Semantic: Conceptual similarity (great for natural language questions)

        Uses ADAPTIVE THRESHOLD: Starts at 0.7 (high quality) and automatically
        falls back to lower thresholds if no results found. This ensures we find
        relevant results without sacrificing quality.

        Retrieves top_k=15 chunks by default for focused, relevant context.
        This provides sufficient information for the LLM to understand the topic
        without overwhelming the session or causing performance issues.

        NOTE: Prefer search_apis() for finding API endpoints or search_docs() for conceptual info.
        Only use this when you need both types of content.

        Supports both single query (backward compatible) and batch queries (new).

        Args:
            query: Single search query (backward compatible, deprecated - use queries)
            queries: Single query or list of queries for batch search (preferred)
            top_k: Maximum number of results per query
            score_threshold: Starting similarity score (will auto-decrease if no results)
            connector_id: If provided, scope search to this connector only (specialist agent)

        Returns:
            List of knowledge chunks with text and metadata (combined from all queries)
        """
        return await self._search_with_filter(
            query=query,
            queries=queries,
            top_k=top_k,
            score_threshold=score_threshold,
            metadata_filter=None,  # No filter - search everything
            exclude_metadata=None,
            connector_id=connector_id,
        )

    async def _search_with_filter(
        self,
        query: str | None = None,
        queries: str | list[str] | None = None,
        top_k: int = 15,
        score_threshold: float = 0.7,
        metadata_filter: dict[str, Any] | None = None,
        exclude_metadata: dict[str, Any] | None = None,
        connector_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Internal method to search with optional metadata filtering.

        Args:
            query: Single search query
            queries: Single query or list of queries
            top_k: Maximum results per query
            score_threshold: Starting similarity score
            metadata_filter: Include only chunks matching this metadata
            exclude_metadata: Exclude chunks matching this metadata
            connector_id: If provided, scope to this connector (specialist search)

        Returns:
            List of matching knowledge chunks
        """
        # Handle both parameter names for backward compatibility
        if queries is not None:
            # New batch search mode
            query_list = [queries] if isinstance(queries, str) else queries
        elif query is not None:
            # Old single query mode (backward compatible)
            query_list = [query]
        else:
            raise ValueError("Either 'query' or 'queries' parameter must be provided")

        # Apply source type filtering if requested
        if metadata_filter:
            # Include only chunks matching this metadata (e.g., source_type="openapi_spec")
            metadata_filters = metadata_filter
        elif exclude_metadata:
            # Exclude chunks matching this metadata
            # Note: This requires special handling in the search logic
            metadata_filters = None  # We'll filter results after retrieval
        else:
            # No filtering - search everything
            metadata_filters = None

        # OPTIMIZED: Use single threshold for better performance
        # Hybrid search (PostgreSQL FTS + semantic) is robust enough that we don't need
        # adaptive fallback. Using 0.6 as sweet spot: good recall without too much noise.
        # This eliminates retry searches, saving 100-300ms per query.
        threshold = 0.6
        all_chunks_list = []
        seen_ids: set = set()

        # --- Connector-scoped search path ---
        if connector_id:
            # Specialist agent: strict scoping to one connector, no fallback
            for q in query_list:
                chunks = await self.knowledge_store.search_by_connector(
                    query=q,
                    user_context=self.user_context,
                    connector_id=connector_id,
                    top_k=top_k,
                    score_threshold=threshold,
                    metadata_filters=metadata_filters if metadata_filters else None,
                )

                # Apply exclusion filter if needed
                if exclude_metadata:
                    chunks = self._apply_exclusion_filter(chunks, exclude_metadata)

                for chunk in chunks:
                    if chunk.id not in seen_ids:
                        seen_ids.add(chunk.id)
                        all_chunks_list.append(chunk)

            return [
                {
                    "text": chunk.text,
                    "source_uri": chunk.source_uri,
                    "tags": chunk.tags,
                    "connector_id": str(getattr(chunk, "connector_id", None) or ""),
                }
                for chunk in all_chunks_list
            ]

        # --- Cross-connector search path (orchestrator / no connector context) ---
        # Try cross-connector search with attribution for the first query
        # and fall back to hybrid search for additional queries
        for q in query_list:
            # Use cross-connector search which returns dicts with attribution
            cross_results = await self.knowledge_store.search_cross_connector(
                query=q,
                user_context=self.user_context,
                top_k=top_k,
                score_threshold=threshold,
                metadata_filters=metadata_filters if metadata_filters else None,
            )

            for r in cross_results:
                rid = r.get("id", "")
                if rid not in seen_ids:
                    seen_ids.add(rid)
                    all_chunks_list.append(r)

        # Apply exclusion filter on cross-connector dicts
        if exclude_metadata and all_chunks_list:
            exclude_key = next(iter(exclude_metadata.keys()))
            exclude_value = exclude_metadata[exclude_key]
            exclude_values = exclude_value if isinstance(exclude_value, list) else [exclude_value]

            # For cross-connector results, check the knowledge_type or tags
            # Exclusion based on search_metadata isn't available in cross-connector dicts,
            # so this is a best-effort filter
            all_chunks_list = [
                r for r in all_chunks_list if r.get(exclude_key) not in exclude_values
            ]

        return [
            {
                "text": r.get("text", ""),
                "source_uri": r.get("source_uri"),
                "tags": r.get("tags"),
                "connector_id": r.get("connector_id"),
                "connector_name": r.get("connector_name"),
                "connector_type": r.get("connector_type"),
            }
            for r in all_chunks_list
        ]

    @staticmethod
    def _apply_exclusion_filter(chunks: list, exclude_metadata: dict[str, Any]) -> list:
        """Apply exclusion filter to chunk objects."""
        exclude_key = next(iter(exclude_metadata.keys()))
        exclude_value = exclude_metadata[exclude_key]
        exclude_values = exclude_value if isinstance(exclude_value, list) else [exclude_value]

        filtered = []
        for chunk in chunks:
            metadata_value = None
            if isinstance(chunk.search_metadata, dict):
                metadata_value = chunk.search_metadata.get(exclude_key)
            elif chunk.search_metadata is not None and hasattr(chunk.search_metadata, exclude_key):
                metadata_value = getattr(chunk.search_metadata, exclude_key)
            if metadata_value not in exclude_values:
                filtered.append(chunk)
        return filtered

    def _build_metadata_filters(self, queries: list[str]) -> dict[str, Any] | None:
        """Automatically build metadata filters from user queries."""
        return _build_metadata_filters(queries)

    @staticmethod
    def _is_example_request(queries: list[str]) -> bool:
        """Detect if user is asking for examples, samples, or response formats."""
        return _is_example_request(queries)

    @staticmethod
    def _boost_code_containing_chunks(chunks: list[Any]) -> list[Any]:
        """Reorder chunks to prioritize those containing code/JSON examples."""
        return _boost_code_containing_chunks(chunks)

    async def determine_connector(self, query: str) -> dict[str, Any]:
        """
        Determine which connector (external system) to use based on user query.

        Analyzes the query and matches it to an available connector using LLM reasoning.
        Returns connector_id if confident, "unknown" if ambiguous.

        This is the FIRST step in the new planning flow - determines target system
        BEFORE searching for endpoints.

        Args:
            query: User query (e.g., "get hosts from VCF", "list k8s pods")

        Returns:
            Dict with:
            - connector_id: UUID of matched connector or "unknown"
            - connector_name: Name of connector (if found)
            - confidence: "high" | "medium" | "low"
            - reason: Why this connector was chosen

        Examples:
            "get hosts from VCF" → {connector_id: "uuid", name: "VCF", confidence: "high"}
            "list pods in kubernetes" → {connector_id: "uuid", name: "K8s", confidence: "high"}
            "do something" → {connector_id: "unknown", confidence: "low"}
        """
        logger.info(f"🎯 DETERMINE_CONNECTOR: Analyzing query: '{query}'")

        # Get available connectors for this tenant
        connectors = await self.list_connectors()

        if not connectors:
            logger.info("📭 DETERMINE_CONNECTOR: No connectors available")
            return {
                "connector_id": "unknown",
                "connector_name": None,
                "confidence": "low",
                "reason": "No connectors configured for this tenant",
            }

        logger.info(f"📋 DETERMINE_CONNECTOR: Found {len(connectors)} connectors")
        for conn in connectors:
            logger.info(
                f"   - {conn['name']} ({conn['id']}): {conn.get('description', 'No description')}"
            )

        # Use LLM to match query to connector
        connector_list = "\n".join(
            [
                f"- {conn['name']} (ID: {conn['id']}): {conn.get('description', 'No description')}"
                for conn in connectors
            ]
        )

        prompt = f"""Given this user query:
"{query}"

And these available connectors/systems:
{connector_list}

Which connector should be used? Analyze the query for system/service mentions.

Rules:
- If query clearly mentions a system name (VCF, VMware, Kubernetes, K8s, GitHub, etc.), match it
- If query is ambiguous or doesn't mention a system, return connector_id="unknown"
- confidence="high" only if system is explicitly mentioned
- confidence="low" if guessing or unclear"""

        try:
            # Use PydanticAI for type-safe structured output
            agent = self._get_classifier_agent()
            result = await agent.run(
                prompt,
                model_settings={"temperature": 0.1},  # Low temperature for consistent matching
            )

            # PydanticAI returns AgentRunResult with typed .output
            determination: ConnectorDetermination = result.output  # type: ignore[assignment]

            logger.info("✅ DETERMINE_CONNECTOR: Result:")
            logger.info(f"   Connector: {determination.connector_name or 'unknown'}")
            logger.info(f"   ID: {determination.connector_id}")
            logger.info(f"   Confidence: {determination.confidence}")
            logger.info(f"   Reason: {determination.reason}")

            # Return as dict for backward compatibility
            return {
                "connector_id": determination.connector_id,
                "connector_name": determination.connector_name,
                "confidence": determination.confidence,
                "reason": determination.reason,
            }

        except Exception as e:
            logger.error(f"❌ DETERMINE_CONNECTOR: Error: {e}")
            return {
                "connector_id": "unknown",
                "connector_name": None,
                "confidence": "low",
                "reason": f"Error determining connector: {e!s}",
            }

    async def search_endpoints(
        self, connector_id: str, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Search for endpoints within a SPECIFIC connector.

        TASK-126: Supports both BM25-only and hybrid (BM25 + semantic) search.
        Controlled by config.endpoint_search_algorithm feature flag.

        Session 60: Takes connector_id FIRST, then searches only that connector's endpoints.
        This is more efficient than searching ALL endpoints across ALL connectors.

        Args:
            connector_id: UUID of the connector to search within
            query: What you want to do (e.g., "list hosts", "get cluster details")
            limit: Maximum number of results to return

        Returns:
            List of matching endpoints with full details (id, method, path, parameters, etc.)

        Example:
            search_endpoints("vcf-uuid", "list virtual machines")
            → [{endpoint_id: "uuid-123", method: "GET", path: "/api/vcenter/vm", ...}]
        """
        logger.info(f"🔍 SEARCH_ENDPOINTS: Searching connector {connector_id}")
        logger.info(f"🔍 SEARCH_ENDPOINTS: Query: '{query}'")

        # TASK-126: Check config to determine search algorithm
        from meho_app.core.config import get_config

        config = get_config()
        use_hybrid = config.endpoint_search_algorithm == "bm25_hybrid"

        logger.info(f"🔍 SEARCH_ENDPOINTS: Using algorithm: {config.endpoint_search_algorithm}")

        try:
            # Search with metadata filters
            # If connector_id is provided, filter to that connector only
            # If not provided, search ALL connectors' endpoints
            metadata_filters: dict[str, Any] = {"source_type": "openapi_spec"}
            if connector_id:  # Only add filter if connector_id is actually provided
                metadata_filters["connector_id"] = connector_id

            if use_hybrid:
                # TASK-126: Use BM25HybridService for better semantic matching
                # "list VMs" will match "virtual_machines", "show health" matches "status"
                from meho_app.modules.knowledge.bm25_hybrid_service import BM25HybridService

                hybrid_service = BM25HybridService(
                    session=self.knowledge_store.repository.session,
                    embedding_provider=self.knowledge_store.embedding_provider,
                    redis=self.redis,
                )

                search_results_dicts = await hybrid_service.search(
                    tenant_id=self.user_context.tenant_id,
                    query=query,
                    top_k=limit * 3,  # Get more candidates for better ranking
                    metadata_filters=metadata_filters,
                )

                logger.info(
                    f"🔍 SEARCH_ENDPOINTS: Hybrid search returned {len(search_results_dicts)} chunks"
                )
            else:
                # Original BM25-only search (fast, keyword-focused)
                from meho_app.modules.knowledge.bm25_service import BM25Service

                bm25_service = BM25Service(
                    self.knowledge_store.repository.session,
                    redis=self.redis,  # Enable caching for 18x speedup!
                )

                search_results_dicts = await bm25_service.search(
                    tenant_id=self.user_context.tenant_id,
                    query=query,
                    top_k=limit * 3,  # Get more candidates for better ranking
                    metadata_filters=metadata_filters,
                )

                logger.info(
                    f"🔍 SEARCH_ENDPOINTS: BM25 search returned {len(search_results_dicts)} chunks"
                )

            # DEBUG: Show what search actually returned
            algorithm_name = "Hybrid" if use_hybrid else "BM25"
            logger.info(f"🔍 SEARCH_ENDPOINTS: Top 10 chunks from {algorithm_name} search:")
            for i, result_dict in enumerate(search_results_dicts[:10]):
                text_preview = result_dict.get("text", "N/A")[:100].replace("\n", " ")
                metadata = result_dict.get("metadata", {})
                # Hybrid uses rrf_score, BM25-only uses bm25_score
                # Use existence check, not falsy check (0.0 is a valid score)
                score = (
                    result_dict.get("rrf_score")
                    if "rrf_score" in result_dict
                    else result_dict.get("bm25_score", 0)
                )
                if metadata:
                    logger.info(
                        f"   {i + 1}. Score: {score:.4f} - {metadata.get('http_method')} {metadata.get('endpoint_path')}"
                    )
                    logger.info(f"      Preview: {text_preview}...")
                else:
                    logger.info(f"   {i + 1}. Score: {score:.4f} - No metadata: {text_preview}...")

            # Extract endpoint operation IDs from search results
            # The BM25 results are dicts with metadata
            endpoint_identifiers = []
            for result_dict in search_results_dicts:
                metadata = result_dict.get("metadata")
                if not metadata:
                    continue

                operation_id = metadata.get("operation_id")
                endpoint_path = metadata.get("endpoint_path")
                http_method = metadata.get("http_method")

                if operation_id or (endpoint_path and http_method):
                    endpoint_identifiers.append(
                        {"operation_id": operation_id, "path": endpoint_path, "method": http_method}
                    )

            logger.info(
                f"🔍 SEARCH_ENDPOINTS: Extracted {len(endpoint_identifiers)} endpoint identifiers"
            )

            # Fetch full endpoint details from database
            # We need the complete schema with parameters, safety_level, etc.
            # If connector_id is provided, filter to that connector; otherwise get all
            endpoint_filter = EndpointFilter(is_enabled=True, limit=500)
            if connector_id:
                endpoint_filter.connector_id = connector_id

            endpoints = await self.endpoint_repo.list_endpoints(endpoint_filter)

            logger.info(f"🔍 SEARCH_ENDPOINTS: Fetched {len(endpoints)} enabled endpoints from DB")

            # Match endpoints by path+method (operation_id is often duplicated!)
            # CRITICAL: Preserve BM25 ranking order!
            # Create lookup dict for fast endpoint matching
            # Use path+method as PRIMARY key (unique), not operation_id (often duplicate!)
            endpoint_lookup_by_path = {}
            for endpoint in endpoints:
                # Index by path+method (UNIQUE identifier)
                key = f"{endpoint.method}:{endpoint.path}"
                endpoint_lookup_by_path[key] = endpoint

            # Match identifiers IN BM25 RANK ORDER
            matched_endpoints = []
            seen_keys = set()

            for i, identifier in enumerate(endpoint_identifiers, 1):
                # Match by path+method (most reliable - unique identifier)
                matched = None
                matched_key = None

                if identifier["path"] and identifier["method"]:
                    lookup_key = f"{identifier['method']}:{identifier['path']}"
                    if lookup_key in endpoint_lookup_by_path:
                        matched = endpoint_lookup_by_path[lookup_key]
                        matched_key = lookup_key

                # Add to results if matched and not duplicate
                if matched and matched_key not in seen_keys:
                    seen_keys.add(matched_key)
                    matched_endpoints.append(matched)
                    logger.info(f"   {i:2}. ✅ Matched: {matched.method} {matched.path}")
                elif not matched:
                    logger.warning(
                        f"   {i:2}. ❌ No match for: {identifier['method']} {identifier['path']} (op_id: {identifier['operation_id']})"
                    )
                elif matched_key in seen_keys:
                    logger.info(
                        f"   {i:2}. ⏭️  Duplicate: {identifier['method']} {identifier['path']}"
                    )

            logger.info(
                f"🔍 SEARCH_ENDPOINTS: Matched {len(matched_endpoints)} unique endpoints (deduplicated, in BM25 rank order)"
            )
            logger.info("🔍 SEARCH_ENDPOINTS: Final top 10 endpoints (in BM25 score order):")
            for i, ep in enumerate(matched_endpoints[:10], 1):
                logger.info(f"   {i:2}. {ep.method} {ep.path}")

            # Format endpoints for return
            # NOTE: No hardcoded filtering - let semantic search and LLM handle relevance
            # MEHO must stay GENERIC and work with ANY system, not just VCF/VMware
            results = []
            for endpoint in matched_endpoints[:limit]:
                # Task 22: Merge custom description with original
                merged_description = endpoint.description or ""
                if endpoint.custom_description:
                    merged_description = f"{endpoint.custom_description}\n\nOriginal: {endpoint.description or 'No description'}"

                endpoint_data = {
                    "endpoint_id": str(endpoint.id),
                    "method": endpoint.method,
                    "path": endpoint.path,
                    "summary": endpoint.summary,
                    "description": merged_description,
                    "safety_level": endpoint.safety_level,
                    "requires_approval": endpoint.requires_approval,
                    "required_params": self._format_required_params(endpoint),
                    "optional_params": self._format_optional_params(endpoint),
                    "usage_example": endpoint.usage_examples
                    or self._generate_usage_example(endpoint),
                    # TASK-90: Include response schema so agent can verify endpoint returns what's needed
                    "response_schema": self._summarize_response_schema(endpoint.response_schema),
                }

                # For write operations, include request body schema summary
                # If POST/PUT/PATCH has a body schema, the body is required
                if endpoint.method in ("POST", "PUT", "PATCH") and endpoint.body_schema:
                    endpoint_data["request_body_schema"] = self._summarize_request_body_schema(
                        endpoint.body_schema
                    )

                # TASK-81: Include LLM instructions for write operations
                # These guide the agent in helping users through complex parameter collection
                if endpoint.method in ("POST", "PUT", "PATCH") and endpoint.llm_instructions:
                    endpoint_data["llm_instructions"] = endpoint.llm_instructions
                    endpoint_data["_guidance_hint"] = (
                        "This is a write operation with guidance available. "
                        "Use the llm_instructions to help the user provide parameters step-by-step."
                    )

                results.append(endpoint_data)

            logger.info(f"✅ SEARCH_ENDPOINTS: Returning {len(results)} endpoints")
            if results:
                logger.info("✅ SEARCH_ENDPOINTS: Top results:")
                for r in results[:5]:
                    logger.info(f"   - {r['method']} {r['path']}")
            else:
                logger.warning(f"⚠️ SEARCH_ENDPOINTS: No results for query: '{query}'")

            return results

        except Exception as e:
            logger.error(f"❌ SEARCH_ENDPOINTS: BM25 search failed: {e}", exc_info=True)
            # Re-raise - don't silently fail, let caller handle it
            raise

    async def get_endpoint_details(
        self, connector_id: str, search_query: str
    ) -> list[dict[str, Any]]:
        """
        Get detailed information about endpoints including parameter requirements.

        NOTE: This is now primarily used internally by search_endpoints().
        For new code, prefer search_endpoints() which has clearer semantics.

        Helps planner understand what parameters are needed before calling an endpoint.

        Args:
            connector_id: Connector UUID
            search_query: Search query to match endpoints (e.g., "list pods", "get VM")

        Returns:
            List of endpoint details with parameter schemas and examples
        """
        logger.info("🔍 ENDPOINT_SEARCH: get_endpoint_details called")
        logger.info(f"🔍 ENDPOINT_SEARCH: connector_id={connector_id}")
        logger.info(f"🔍 ENDPOINT_SEARCH: search_query='{search_query}'")

        # SECURITY: Verify connector belongs to current tenant
        # Prevents cross-tenant endpoint enumeration
        connector = await self.connector_repo.get_connector(
            connector_id, tenant_id=self.user_context.tenant_id
        )
        if not connector:
            raise ValueError(f"Connector {connector_id} not found")

        logger.info(f"✅ Connector found: {connector.name}")

        # Get all ENABLED endpoints for this connector (Task 22: respect is_enabled flag)
        # Try with search_text first for efficiency
        endpoints = await self.endpoint_repo.list_endpoints(
            EndpointFilter(
                connector_id=connector_id,
                is_enabled=True,
                search_text=search_query,  # Pre-filter by search query at DB level
                limit=500,  # Increase limit to avoid missing endpoints
            )
        )

        # If search_text returns nothing, fall back to ALL endpoints for this connector
        # This prevents empty results when search query is too specific
        if not endpoints:
            logger.info(
                "🔍 ENDPOINT_SEARCH: search_text returned 0 results, trying broader search..."
            )
            endpoints = await self.endpoint_repo.list_endpoints(
                EndpointFilter(
                    connector_id=connector_id,
                    is_enabled=True,
                    # No search_text - get all endpoints
                    limit=500,
                )
            )
            logger.info(f"🔍 ENDPOINT_SEARCH: Broader search found {len(endpoints)} endpoints")

        logger.info(
            f"🔍 ENDPOINT_SEARCH: Found {len(endpoints)} enabled endpoints before keyword filtering"
        )

        # Filter by search query (simple keyword matching for now)
        search_lower = search_query.lower()
        logger.info(f"🔍 Search (lowercase): '{search_lower}'")
        logger.info(f"🔍 Search words: {search_lower.split()}")
        matches = []

        for endpoint in endpoints:
            # Match on summary, description, operation_id, or path
            # Use 'or ""' to avoid None becoming literal "None" string
            searchable = f"{endpoint.summary or ''} {endpoint.description or ''} {endpoint.operation_id or ''} {endpoint.path}".lower()

            # Log first 5 endpoints for debugging
            if len(matches) < 5 or endpoint.path == "/v1/hosts":
                logger.info(f"   Checking: {endpoint.method} {endpoint.path}")
                logger.info(f"      Searchable: '{searchable[:100]}'")

            if any(word in searchable for word in search_lower.split()):
                logger.info(f"   ✅ MATCHED: {endpoint.method} {endpoint.path}")

                # Task 22: Merge custom description with original
                merged_description = endpoint.description or ""
                if endpoint.custom_description:
                    merged_description = f"{endpoint.custom_description}\n\nOriginal: {endpoint.description or 'No description'}"

                matches.append(
                    {
                        "endpoint_id": str(endpoint.id),
                        "method": endpoint.method,
                        "path": endpoint.path,
                        "summary": endpoint.summary,
                        "description": merged_description,  # Enhanced with custom description
                        "safety_level": endpoint.safety_level,  # Task 22
                        "requires_approval": endpoint.requires_approval,  # Task 22
                        "required_params": self._format_required_params(endpoint),
                        "optional_params": self._format_optional_params(endpoint),
                        "usage_example": endpoint.usage_examples
                        or self._generate_usage_example(endpoint),  # Prefer admin examples
                    }
                )

        logger.info(f"🔍 ENDPOINT_SEARCH: Returning {len(matches)} matches after filtering")
        if matches:
            logger.info("🔍 ENDPOINT_SEARCH: First few matches:")
            for m in matches[:5]:
                logger.info(f"   - {m['method']} {m['path']}")
        else:
            logger.warning(f"🔍 ENDPOINT_SEARCH: ⚠️ No matches found for query '{search_query}'")

        return matches

    def _format_required_params(self, endpoint) -> dict[str, Any]:
        """Format required parameters with details."""
        return format_required_params(endpoint)

    def _format_optional_params(self, endpoint) -> dict[str, Any]:
        """Format optional parameters."""
        return format_optional_params(endpoint)

    def _generate_usage_example(self, endpoint) -> dict[str, Any]:
        """Generate usage example for endpoint."""
        return generate_usage_example(endpoint)

    def _summarize_response_schema(self, response_schema: dict[str, Any]) -> dict[str, Any]:
        """Summarize response schema so the agent can verify endpoint returns what's needed."""
        return summarize_response_schema(response_schema)

    def _summarize_request_body_schema(self, body_schema: dict[str, Any]) -> dict[str, Any]:
        """Summarize request body schema for POST/PUT/PATCH endpoints."""
        return summarize_request_body_schema(body_schema)

    async def list_connectors(self):
        """
        List available connectors (external systems).

        Returns tenant-scoped connectors that the user has access to.
        Useful for the planner to discover what systems are available.

        Returns:
            List of connector info (id, name, base_url, description, auth_type)
        """
        connectors = await self.connector_repo.list_connectors(
            tenant_id=self.user_context.tenant_id
        )

        # Convert Pydantic models to plain dicts with ALL string values
        # This prevents PydanticAI from trying to validate against Connector schema
        result: list[dict[str, str]] = []

        for conn in connectors:
            # Use model_dump to convert Pydantic model to dict
            conn_dict = conn.model_dump(mode="python")

            # Create new dict with only the fields we need, all as strings
            result.append(
                {
                    "id": str(conn_dict["id"]),
                    "name": str(conn_dict.get("name", "")),
                    "base_url": str(conn_dict.get("base_url", "")),
                    "description": str(conn_dict.get("description") or ""),
                    "auth_type": str(conn_dict.get("auth_type", "NONE")),
                }
            )

        return result

    async def batch_get_endpoint(
        self, connector_id: str, endpoint_id: str, parameter_sets: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        Call the same GET endpoint multiple times with different parameters.

        This tool is useful when you need to retrieve information about multiple
        resources (e.g., details for multiple VMs, multiple users, etc.) where
        the endpoint requires an ID or parameter that changes per call.

        IMPORTANT: Only works with GET methods! This is a safety measure to prevent
        accidental bulk modifications.

        Args:
            connector_id: Connector UUID
            endpoint_id: Endpoint descriptor UUID
            parameter_sets: List of parameter combinations, each containing:
                - path_params: Path parameters (e.g., {"vm_id": "vm-123"})
                - query_params: Query parameters (e.g., {"include": "metrics"})

        Returns:
            Dict with:
            - results: List of responses, each with:
                - parameters: The parameters used for this call
                - status_code: HTTP status code
                - data: Response data (if successful)
                - error: Error message (if failed)
                - success: Boolean indicating success/failure
            - summary:
                - total: Total number of calls attempted
                - successful: Number of successful calls
                - failed: Number of failed calls

        Example:
            parameter_sets = [
                {"path_params": {"vm_id": "vm-1"}, "query_params": {"details": "full"}},
                {"path_params": {"vm_id": "vm-2"}, "query_params": {"details": "full"}},
                {"path_params": {"vm_id": "vm-3"}, "query_params": {"details": "full"}}
            ]

            result = await batch_get_endpoint(connector_id, endpoint_id, parameter_sets)

            # Result structure:
            # {
            #   "results": [
            #     {
            #       "parameters": {"path_params": {"vm_id": "vm-1"}, ...},
            #       "status_code": 200,
            #       "data": {"name": "VM-1", ...},
            #       "success": True
            #     },
            #     ...
            #   ],
            #   "summary": {"total": 3, "successful": 3, "failed": 0}
            # }
        """
        logger = get_logger(__name__)

        logger.info(f"\n{'=' * 80}")
        logger.info("🔄 BATCH_GET_ENDPOINT: Starting batch operation")
        logger.info(f"   Connector ID: {connector_id}")
        logger.info(f"   Endpoint ID: {endpoint_id}")
        logger.info(f"   Parameter sets: {len(parameter_sets)}")
        logger.info(f"{'=' * 80}")

        # SECURITY: Fetch endpoint and validate it's a GET method
        endpoint = await self.endpoint_repo.get_endpoint(endpoint_id)
        if not endpoint:
            raise ValueError(f"Endpoint {endpoint_id} not found")

        if endpoint.method.upper() != "GET":
            raise ValueError(
                f"batch_get_endpoint only works with GET methods. "
                f"Endpoint {endpoint.path} uses {endpoint.method}. "
                f"Use call_endpoint for non-GET methods."
            )

        logger.info(f"✅ Validated endpoint: GET {endpoint.path}")
        logger.info(f"   Summary: {endpoint.summary}")

        # Execute calls sequentially (could be parallelized later if needed)
        results = []
        successful = 0
        failed = 0

        for i, param_set in enumerate(parameter_sets, 1):
            path_params = param_set.get("path_params")
            query_params = param_set.get("query_params")

            logger.info(f"\n📞 Call {i}/{len(parameter_sets)}")
            logger.info(f"   Path params: {path_params}")
            logger.info(f"   Query params: {query_params}")

            try:
                response = await self.call_endpoint(
                    connector_id=connector_id,
                    endpoint_id=endpoint_id,
                    path_params=path_params,
                    query_params=query_params,
                    body=None,  # GET requests don't have body
                )

                # Success case
                results.append(
                    {
                        "parameters": {
                            "path_params": path_params or {},
                            "query_params": query_params or {},
                        },
                        "status_code": response.get("status_code"),
                        "data": response.get("data"),
                        "success": True,
                    }
                )
                successful += 1
                logger.info(f"   ✅ Success: {response.get('status_code')}")

            except Exception as e:
                # Failure case - record error but continue with remaining calls
                logger.warning(f"   ❌ Failed: {e!s}")
                results.append(
                    {
                        "parameters": {
                            "path_params": path_params or {},
                            "query_params": query_params or {},
                        },
                        "error": str(e),
                        "success": False,
                    }
                )
                failed += 1

        logger.info(f"\n{'=' * 80}")
        logger.info("🏁 BATCH_GET_ENDPOINT: Completed")
        logger.info(f"   Total: {len(parameter_sets)}")
        logger.info(f"   Successful: {successful}")
        logger.info(f"   Failed: {failed}")
        logger.info(f"{'=' * 80}\n")

        return {
            "results": results,
            "summary": {"total": len(parameter_sets), "successful": successful, "failed": failed},
        }

    async def call_endpoint(
        self,
        connector_id: str,
        endpoint_id: str,
        path_params: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Call an external API endpoint via OpenAPI connector.

        Automatically fetches user-specific credentials if the connector
        requires them (credential_strategy = 'user_provided').

        Args:
            connector_id: Connector UUID
            endpoint_id: Endpoint descriptor UUID
            path_params: Path parameters (e.g., {id: "123"})
            query_params: Query parameters (e.g., {limit: 10})
            body: Request body for POST/PUT/PATCH

        Returns:
            API response as dictionary
        """
        logger = get_logger(__name__)

        logger.info(f"\n{'=' * 80}")
        logger.info("🌐 DEPENDENCIES: call_endpoint starting")
        logger.info(f"   Connector ID: {connector_id}")
        logger.info(f"   Endpoint ID: {endpoint_id}")
        logger.info(f"   User: {self.user_context.user_id}")
        logger.info(f"   Tenant: {self.user_context.tenant_id}")
        logger.info(f"{'=' * 80}")

        # Fetch endpoint descriptor first (we need it anyway, and it tells us the connector_id)
        logger.info("🔍 DEPENDENCIES: Fetching endpoint descriptor...")
        endpoint = await self.endpoint_repo.get_endpoint(endpoint_id)
        if not endpoint:
            logger.error(f"❌ DEPENDENCIES: Endpoint {endpoint_id} not found")
            raise ValueError(f"Endpoint {endpoint_id} not found")

        logger.info("✅ DEPENDENCIES: Endpoint found")
        logger.info(f"   Method: {endpoint.method}")
        logger.info(f"   Path: {endpoint.path}")
        logger.info(f"   Summary: {endpoint.summary}")

        # If connector_id is not provided, use the endpoint's connector_id
        # This allows the LLM to just pass endpoint_id without needing to track connector_id
        if not connector_id:
            connector_id = endpoint.connector_id
            logger.info(f"✅ DEPENDENCIES: Using connector_id from endpoint: {connector_id}")

        # Fetch connector
        # SECURITY: Pass tenant_id to enforce tenant isolation
        logger.info("🔍 DEPENDENCIES: Fetching connector...")
        connector = await self.connector_repo.get_connector(
            connector_id, tenant_id=self.user_context.tenant_id
        )
        if not connector:
            logger.error(
                f"❌ DEPENDENCIES: Connector {connector_id} not found for tenant {self.user_context.tenant_id}"
            )
            raise ValueError(f"Connector {connector_id} not found")

        logger.info(f"✅ DEPENDENCIES: Connector found: {connector.name}")
        logger.info(f"   Base URL: {connector.base_url}")
        logger.info(f"   Auth type: {connector.auth_type}")
        logger.info(f"   Credential strategy: {connector.credential_strategy}")

        # Phase 74: Unified credential resolution via CredentialResolver
        credentials = None
        stale_credential_warnings: list[dict] = []  # SECU-07: stale credential warnings
        try:
            from meho_app.modules.connectors.credential_resolver import (
                CredentialResolver, SessionType, CredentialScopeError, CredentialNotFoundError,
            )
            from meho_app.modules.connectors.keycloak_user_checker import KeycloakUserChecker

            session_type_enum = SessionType(self.session_type) if self.session_type != "interactive" else SessionType.INTERACTIVE
            config = get_api_config()
            keycloak_checker = KeycloakUserChecker(
                keycloak_url=config.keycloak_url,
                admin_username=config.keycloak_admin_username,
                admin_password=config.keycloak_admin_password,
            )
            credential_resolver = CredentialResolver(
                cred_repo=self.user_cred_repo,
                keycloak_checker=keycloak_checker,
                delegation_flag_callback=self.delegation_flag_callback,
            )
            resolved = await credential_resolver.resolve(
                session_type=session_type_enum,
                user_id=self.user_context.user_id,
                connector_id=connector_id,
                created_by_user_id=self.created_by_user_id,
                allowed_connector_ids=self.allowed_connector_ids,
                trigger_type=self.trigger_type,
                trigger_id=self.trigger_id,
                tenant_id=self.user_context.tenant_id,
                delegation_active=self.delegation_active,
            )
            credentials = resolved.credentials
            logger.info(f"✅ DEPENDENCIES: Credentials resolved (source={resolved.source.value})")
            logger.info(f"   Credential keys: {list(credentials.keys())}")

            # SECU-07: Check credential age for stale warning
            try:
                # For user_own or delegated, check via cred_repo
                check_user_id = (
                    resolved.delegated_by_user_id
                    if resolved.delegated_by_user_id
                    else self.user_context.user_id
                )
                credential_age_days = await self.user_cred_repo.get_credential_age_days(
                    user_id=check_user_id,
                    connector_id=connector_id,
                )
                if credential_age_days is not None and credential_age_days > 90:
                    warning_msg = (
                        f"Warning: {connector.name} connector credentials are "
                        f"{credential_age_days} days old. Consider rotating credentials for security."
                    )
                    stale_credential_warnings.append(
                        {
                            "message": warning_msg,
                            "details": {
                                "connector_id": str(connector.id),
                                "connector_name": connector.name,
                                "credential_age_days": credential_age_days,
                                "warning_type": "stale_credentials",
                            },
                        }
                    )
                    logger.warning(f"SECU-07: {warning_msg}")
            except Exception as e:
                logger.warning(
                    f"Failed to check credential age for connector {connector_id}: {e}"
                )
        except CredentialScopeError as e:
            logger.error(f"❌ DEPENDENCIES: Connector {connector_id} not in allowed scope: {e.allowed}")
            raise ValueError(f"Connector {connector_id} not in allowed scope: {e.allowed}") from e
        except CredentialNotFoundError as e:
            logger.error(f"❌ DEPENDENCIES: {e}")
            raise ValueError(str(e)) from e

        # SECURITY: Validate endpoint belongs to the connector
        if endpoint.connector_id != str(connector.id):
            logger.error(
                "❌ DEPENDENCIES: Security violation - endpoint belongs to different connector"
            )
            logger.error(f"   Endpoint connector_id: {endpoint.connector_id}")
            logger.error(f"   Requested connector_id: {connector.id}")
            raise ValueError(f"Endpoint {endpoint_id} does not belong to connector {connector_id}")

        # Get session state for SESSION auth
        session_token = None
        session_expires_at = None
        refresh_token = None
        refresh_expires_at = None

        if connector.auth_type == "SESSION":
            logger.info("🔐 DEPENDENCIES: SESSION auth - fetching session state...")
            session_state = await self.user_cred_repo.get_session_state(
                user_id=self.user_context.user_id, connector_id=connector_id
            )
            if session_state:
                session_token = session_state.get("session_token")
                session_expires_at = session_state.get("session_expires_at")
                refresh_token = session_state.get("refresh_token")
                refresh_expires_at = session_state.get("refresh_expires_at")
                logger.info("✅ DEPENDENCIES: Session state retrieved")
                logger.info(f"   Has token: {bool(session_token)}")
                logger.info(f"   Expires at: {session_expires_at}")
                logger.info(f"   Session state: {session_state.get('session_state')}")
            else:
                logger.info("ℹ️  DEPENDENCIES: No existing session state (will login)")  # noqa: RUF001 -- intentional unicode character

        # Callback to update session state after HTTP call
        async def on_session_update(
            token: str,
            expires_at,
            state: str,
            refresh: str | None = None,
            refresh_expires: datetime | None = None,
        ):
            logger.info("💾 DEPENDENCIES: Updating session state in database...")
            await self.user_cred_repo.update_session_state(
                user_id=self.user_context.user_id,
                connector_id=connector_id,
                session_token=token,
                session_expires_at=expires_at,
                session_state=state,
                refresh_token=refresh,
                refresh_expires_at=refresh_expires,
            )
            logger.info("✅ DEPENDENCIES: Session state updated")

        # Call the API
        logger.info("🚀 DEPENDENCIES: Calling HTTP client...")
        logger.info(f"   Path params: {path_params}")
        logger.info(f"   Query params: {query_params}")
        logger.info(f"   Body: {body}")

        try:
            status_code, data = await self.http_client.call_endpoint(
                connector=connector,
                endpoint=endpoint,
                path_params=path_params or {},
                query_params=query_params or {},
                body=body,
                user_credentials=credentials,
                session_token=session_token,
                session_expires_at=session_expires_at,
                refresh_token=refresh_token,
                refresh_expires_at=refresh_expires_at,
                on_session_update=on_session_update,
            )

            logger.info("✅ DEPENDENCIES: HTTP call completed")
            logger.info(f"   Status code: {status_code}")
            logger.info(f"   Response type: {type(data).__name__}")
            if isinstance(data, dict):
                logger.info(f"   Response keys: {list(data.keys())}")
            elif isinstance(data, list):
                logger.info(f"   Response items: {len(data)}")

        except Exception as e:
            logger.error("❌ DEPENDENCIES: HTTP call failed")
            logger.error(f"   Error: {e!s}")
            logger.error(f"   Error type: {type(e).__name__}")
            import traceback

            logger.error(f"   Traceback:\n{traceback.format_exc()}")
            raise

        # INTELLIGENT RESPONSE HANDLING
        # Check if response is too large for LLM context
        data_size = self._estimate_size(data)

        if data_size > 500 * 1024:  # 500KB threshold
            # Automatically summarize large responses
            data = await self._summarize_large_response(
                data=data,
                endpoint_summary=endpoint.summary or "API call",
                data_size_kb=data_size / 1024,
            )

        result = {
            "status_code": status_code,
            "data": data,
            "success": status_code < 400,
            "endpoint_path": endpoint.path,  # For generic entity type detection
        }

        # SECU-07: Propagate stale credential warnings to caller
        if stale_credential_warnings:
            result["warnings"] = stale_credential_warnings

        return result

    async def interpret_results(
        self, context: str, results: list[dict[str, Any]], question: str | None = None
    ) -> str:
        """
        Use LLM to interpret and synthesize results from multiple sources.

        This is the "thinking" step where the agent analyzes data from
        multiple API calls and knowledge searches to form conclusions.

        Args:
            context: Context about what was being investigated
            results: List of results from API calls or searches
            question: Optional specific question to answer

        Returns:
            LLM interpretation as text
        """
        # Count total items
        total_items = 0
        for result_set in results:
            if isinstance(result_set, list):
                total_items += len(result_set)

        logger.debug("interpret_results called", result_sets=len(results), total_items=total_items)

        # Build prompt for LLM
        results_text = "\n\n".join(
            [f"Result {i + 1}:\n{self._format_result(result)}" for i, result in enumerate(results)]
        )

        prompt_size = len(results_text)
        logger.debug("interpret_results formatted prompt", prompt_size=prompt_size)

        needs_verbatim = self._requires_verbatim_example(context, question)
        if needs_verbatim:
            # Extract preferred keywords from user query (quoted strings, explicit terms)
            preferred_keywords = self._extract_preferred_keywords(context, question)
            snippet = self._extract_verbatim_snippet(results, preferred_keywords)
            if snippet:
                return (
                    "## Example Response\n\n"
                    "Here's the documented sample response from the knowledge base:\n\n"
                    f"```json\n{snippet.strip()}\n```"
                )
        verbatim_instruction = ""
        if needs_verbatim:
            verbatim_instruction = (
                "\nIMPORTANT: The user explicitly asked for an example/sample response. "
                "Locate the exact JSON/text snippet from the knowledge results and include it verbatim "
                "inside a code block. Do NOT fabricate or paraphrase the example."
            )

        prompt = f"""You are MEHO, a helpful AI assistant. Answer the user's question naturally based on the search results.{verbatim_instruction}

User asked: {context}

Here's what I found:
{results_text}

{f"Specifically: {question}" if question else ""}

**IMPORTANT: Format your response using markdown for better readability:**
- Use **## Headers** for main sections
- Use **bullet lists** (- or •) for enumerating items
- Use **`code`** for technical terms, field names, or values
- Use **```json``` or ```yaml``` code blocks** for data structures, configurations, or examples
- Use **tables** (| column |) when presenting structured data
- Use **bold** for emphasis on key findings or important points
- Keep it conversational and clear - like explaining to a colleague, not writing a formal report

**If searches returned no results or wrong results:**
- Be honest and explain what you searched for
- Explain why you couldn't find what was needed
- Suggest practical next steps (different search terms, check API docs, verify endpoint exists)
- Offer alternatives (manual endpoint specification, documentation search, system verification)
- Stay positive and helpful - this is a common situation when exploring new APIs

**If you found relevant information:**
- Present it in a well-structured, visually appealing format
- Highlight key findings
- Provide actionable next steps"""

        # Use PydanticAI for consistent LLM interaction (with built-in retries)
        try:
            agent = self._get_interpreter_agent()
            result = await agent.run(
                prompt,
                model_settings={"temperature": 0.3},  # Lower temperature for consistent analysis
            )

            # PydanticAI returns AgentRunResult with .output (str in this case)
            return result.output or ""

        except (ModelHTTPError, ModelAPIError) as e:
            status_code = getattr(e, "status_code", None)
            logger.error(f"LLM error during interpretation (HTTP {status_code}): {e}")
            if isinstance(e, ModelHTTPError) and status_code == 429:
                raise LLMError(
                    "rate_limit",
                    "transient",
                    "AI service rate limited during data interpretation",
                    remediation="Wait a moment and retry the query",
                ) from e
            raise LLMError(
                "connection",
                "transient",
                "AI service unavailable during data interpretation",
                remediation="Check Claude API connectivity",
            ) from e
        except Exception as e:
            # PydanticAI handles retries internally, so if we get here it's a real failure
            logger.error(f"INTERPRET_RESULTS: Error: {e}")
            raise ValueError(f"LLM error during interpretation: {e}") from e

    @staticmethod
    def _requires_verbatim_example(context: str | None, question: str | None) -> bool:
        """Detect if the user explicitly requested an example/sample response/payload."""
        return requires_verbatim_example(context, question)

    @staticmethod
    def _extract_preferred_keywords(context: str | None, question: str | None) -> list[str]:
        """Extract preferred keywords from user query for snippet matching."""
        return extract_preferred_keywords(context, question)

    @staticmethod
    def _extract_verbatim_snippet(
        results: list[dict[str, Any]], preferred_keywords: list[str] | None = None
    ) -> str | None:
        """Attempt to pull a JSON snippet directly from knowledge search results."""
        return extract_verbatim_snippet(results, preferred_keywords)

    async def _summarize_large_response(
        self, data: Any, endpoint_summary: str, data_size_kb: float
    ) -> dict[str, Any]:
        """
        Intelligently summarize large API responses using LLM.

        Args:
            data: Large response data
            endpoint_summary: What the endpoint does
            data_size_kb: Size in KB

        Returns:
            Summarized response (much smaller)
        """
        prompt = f"""You are analyzing a large API response ({data_size_kb:.1f}KB) that's too big for context.

Endpoint: {endpoint_summary}

Your task: Extract ONLY the most relevant information for diagnosing issues.

Focus on:
1. Items with errors, failures, or anomalies
2. Statistical summary (totals, counts by status)
3. Key identifying fields (names, IDs)
4. Critical metrics (CPU, memory, status)

Strategies:
- If it's a list: filter to items with issues only
- If all items are similar: provide statistical summary
- Extract minimal fields per item (name, status, key metrics)
- Omit verbose/redundant data

Response data (first 5000 chars):
{str(data)[:5000]}

Return a condensed JSON object (max 50KB) with:
- summary: Text description
- critical_items: Array of items with issues (max 50)
- statistics: Counts and percentages
"""

        # Use PydanticAI for type-safe data extraction (with built-in retries)
        try:
            agent = self._get_data_extractor_agent()
            result = await agent.run(
                prompt,
                model_settings={"temperature": 0.1},  # Low temperature for consistent extraction
            )

            # PydanticAI returns AgentRunResult with typed .output
            data_summary: DataSummary = result.output  # type: ignore[assignment]

            # Convert to dict for backward compatibility
            return_dict: dict[str, Any] = {"summary": data_summary.summary}

            if data_summary.critical_items is not None:
                return_dict["critical_items"] = data_summary.critical_items

            if data_summary.statistics is not None:
                return_dict["statistics"] = data_summary.statistics

            if data_summary.note is not None:
                return_dict["note"] = data_summary.note

            return return_dict

        except (ModelHTTPError, ModelAPIError) as e:
            status_code = getattr(e, "status_code", None)
            logger.warning(f"LLM summarization error (HTTP {status_code}): {e}")
            return {
                "summary": f"Large response ({data_size_kb:.1f}KB) - AI summarization unavailable",
                "data_sample": str(data)[:10000],
                "note": f"AI service error ({status_code or 'connection'}), showing truncated sample",
            }
        except Exception as e:
            # Fallback to truncation (existing behavior preserved)
            logger.warning(f"LLM summarization error: {type(e).__name__}: {e}")
            return {
                "summary": f"Large response ({data_size_kb:.1f}KB) - truncated",
                "data_sample": str(data)[:10000],
                "note": f"LLM error ({type(e).__name__}), showing truncated sample",
            }

    def _estimate_size(self, obj: Any) -> int:
        """Estimate size of object in bytes."""
        return _estimate_size(obj)

    def _format_result(self, result: dict[str, Any]) -> str:
        """Format a result dictionary for LLM consumption."""
        return _format_result(result)

    # =========================================================================
    # DATA REDUCTION (TASK-83 Unified Execution)
    # =========================================================================

    async def reduce_data(
        self,
        question: str,
        data: dict[str, Any],
        endpoint_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Intelligently reduce large API response data based on the user's question.

        Uses the Data Reduction Engine (TASK-83) to:
        1. Generate a query from the natural language question
        2. Execute filtering, sorting, and aggregation
        3. Return reduced, LLM-ready data

        This should be used when:
        - API response has many records (>50)
        - User is asking about specific subsets
        - Data needs aggregation or analysis

        Args:
            question: The user's question about the data
            data: Raw API response data
            endpoint_path: Optional endpoint path for context

        Returns:
            Reduced data with:
            - records: Filtered/sorted records
            - aggregates: Computed summary stats
            - context: Formatted text for LLM
            - metadata: Processing information
        """
        from meho_app.modules.agents.unified_executor import UnifiedExecutor, analyze_response

        logger.info(f"📊 REDUCE_DATA: Processing data for question: {question[:50]}...")

        # Analyze the response
        analysis = analyze_response(data)
        logger.info(
            f"📊 REDUCE_DATA: {analysis.total_records} records, "
            f"{analysis.size_kb:.1f}KB, needs_reduction={analysis.needs_reduction}"
        )

        # If small response, return as-is
        if not analysis.needs_reduction and analysis.total_records < 50:
            logger.info("📊 REDUCE_DATA: Small response, no reduction needed")
            return {
                "records": data.get(analysis.source_path, data) if analysis.source_path else data,
                "aggregates": {},
                "context": f"Returned {analysis.total_records} records",
                "metadata": {
                    "total_records": analysis.total_records,
                    "reduced": False,
                },
            }

        # Use unified executor for intelligent reduction
        executor = UnifiedExecutor()

        try:
            result = await executor.process_response(
                question=question,
                api_response=data,
                endpoint_info={"path": endpoint_path} if endpoint_path else None,
            )

            if result.reduced_data:
                logger.info(
                    f"📊 REDUCE_DATA: Reduced {result.analysis.total_records} → "
                    f"{result.reduced_data.returned_records} records"
                )
                return {
                    "records": result.reduced_data.records,
                    "aggregates": result.reduced_data.aggregates,
                    "context": result.llm_context,
                    "metadata": {
                        "total_records": result.reduced_data.total_source_records,
                        "filtered_records": result.reduced_data.total_after_filter,
                        "returned_records": result.reduced_data.returned_records,
                        "reduced": True,
                        "processing_time_ms": result.reduced_data.processing_time_ms,
                    },
                }
            else:
                # No reduction performed
                return {
                    "records": data.get(analysis.source_path, data)
                    if analysis.source_path
                    else data,
                    "aggregates": {},
                    "context": result.llm_context,
                    "metadata": {
                        "total_records": analysis.total_records,
                        "reduced": False,
                    },
                }

        except Exception as e:
            logger.warning(f"⚠️  REDUCE_DATA: Error during reduction: {e}")
            # Fallback to raw data
            return {
                "records": data.get(analysis.source_path, data) if analysis.source_path else data,
                "aggregates": {},
                "context": f"Data reduction failed: {e}",
                "metadata": {
                    "total_records": analysis.total_records,
                    "reduced": False,
                    "error": str(e),
                },
            }
