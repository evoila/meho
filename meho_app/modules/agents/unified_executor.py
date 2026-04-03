# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unified Execution Orchestrator.

This module bridges the Data Reduction Engine with the existing agent
execution flow, providing intelligent data processing for all API responses.

The key insight is that API responses should be processed BEFORE being
sent to the LLM for interpretation. This module handles:

1. Response analysis - Determine if reduction is needed
2. Query generation - Generate a DataQuery from user's question
3. Execution - Run the query on the API response
4. Result formatting - Format results for LLM interpretation
5. Session caching - Store full responses for Brain-Muscle architecture
6. Schema parsing - Extract x-meho-* extensions from OpenAPI specs

Brain-Muscle Architecture:
- MUSCLE stores full responses server-side
- BRAIN receives summaries only (schema + count + sample)
- Brain can request filter/map/reduce operations on cached data
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import redis.asyncio as redis

from meho_app.core.otel import get_logger
from meho_app.modules.agents.data_reduction import (
    DataQuery,
    QueryGeneratorOutput,
    ReducedData,
    generate_query,
)
from meho_app.modules.agents.data_reduction.adapter import execute_data_query
from meho_app.modules.agents.execution.analysis import (
    ResponseAnalysis,
    analyze_response,
)
from meho_app.modules.agents.execution.cache import (
    CachedData,
    CachedResponse,
    CachedTable,
    ResponseTier,
    SchemaSummary,
    determine_response_tier,
    estimate_tokens,
)

logger = get_logger(__name__)


# =============================================================================
# Unified Executor
# =============================================================================


class UnifiedExecutor:
    """
    Unified execution orchestrator for data processing.

    This integrates with the agent's execution flow to provide
    intelligent data reduction based on the user's question.

    Usage:
        ```python
        executor = UnifiedExecutor()

        # After API call returns
        result = await executor.process_response(
            question="Show clusters with high memory usage",
            api_response=api_data,
            endpoint_info={"path": "/clusters", "method": "GET"}
        )

        # result.reduced_data has the processed records
        # result.llm_context has formatted text for the LLM
        ```
    """

    def __init__(
        self,
        auto_reduce_threshold: int = 50,  # Records
        auto_reduce_size_kb: int = 50,  # KB
        redis_client: redis.Redis | None = None,
        cache_ttl: timedelta = timedelta(hours=1),
    ) -> None:
        """
        Initialize the unified executor.

        Args:
            auto_reduce_threshold: Record count threshold for auto-reduction
            auto_reduce_size_kb: Size threshold for auto-reduction
            redis_client: Optional Redis client for persistent cache (TASK-93)
            cache_ttl: TTL for cached responses in Redis (default 1 hour)
        """
        self.auto_reduce_threshold = auto_reduce_threshold
        self.auto_reduce_size_kb = auto_reduce_size_kb

        # Redis for persistent cache (TASK-93: Response Cache Persistence)
        self._redis = redis_client
        self._cache_ttl = cache_ttl
        self._redis_key_prefix = "meho:cache"
        self._tables_key_prefix = "meho:tables"  # SQL tables (new architecture)

        # L1 in-memory cache (fast, per-process)
        # L2 Redis cache (persistent, shared across requests)
        self._session_cache: dict[str, CachedResponse] = {}

        # SQL Tables cache: session_id -> table_name -> CachedTable
        self._session_tables: dict[str, dict[str, CachedTable]] = {}

    async def process_response(
        self,
        question: str,
        api_response: dict[str, Any],
        endpoint_info: dict[str, Any] | None = None,
        force_reduction: bool = False,
    ) -> ExecutionResult:
        """
        Process an API response with intelligent data reduction.

        This is the main entry point for the unified execution flow.

        Args:
            question: The user's original question
            api_response: Raw API response data
            endpoint_info: Optional endpoint metadata (path, method, etc.)
            force_reduction: Force data reduction even for small responses

        Returns:
            ExecutionResult with reduced data and LLM context
        """
        # Step 1: Analyze the response
        analysis = analyze_response(api_response)

        logger.info(
            f"Response analysis: {analysis.total_records} records, "
            f"{analysis.size_kb:.1f}KB, source_path='{analysis.source_path}'"
        )

        # Step 2: Determine if reduction is needed
        should_reduce = (
            force_reduction
            or analysis.needs_reduction
            or analysis.total_records > self.auto_reduce_threshold
            or analysis.estimated_size_bytes > self.auto_reduce_size_kb * 1024
        )

        if not should_reduce:
            # Small response - return as-is
            return ExecutionResult(
                reduced_data=None,
                raw_data=api_response,
                analysis=analysis,
                query_generated=None,
                llm_context=self._format_small_response(api_response, question),
            )

        # Step 3: Generate a query from the question
        try:
            schema = self._extract_schema(api_response, analysis.source_path)
            query_output = await generate_query(
                question=question,
                response_schema=schema,
                endpoint_path=endpoint_info.get("path") if endpoint_info else None,
                max_records=100,
            )
            query = query_output.query

            logger.info(
                f"Generated query with confidence {query_output.confidence:.2f}: "
                f"{query_output.reasoning}"
            )
        except Exception as e:
            logger.warning(f"Query generation failed: {e}, using default query")
            # Fall back to a simple query
            query = DataQuery(
                source_path=analysis.source_path,
                limit=50,
            )
            query_output = None

        # Step 4: Execute the query
        reduced = execute_data_query(api_response, query)

        logger.info(
            f"Data reduction: {reduced.total_source_records} → "
            f"{reduced.returned_records} records ({reduced.reduction_ratio:.1%})"
        )

        # Step 5: Format for LLM
        llm_context = self._format_reduced_response(reduced, question, query_output)

        return ExecutionResult(
            reduced_data=reduced,
            raw_data=api_response,
            analysis=analysis,
            query_generated=query_output,
            llm_context=llm_context,
        )

    def _extract_schema(  # NOSONAR (cognitive complexity)
        self,
        data: dict[str, Any],
        source_path: str,
    ) -> dict[str, Any]:
        """Extract a schema from sample data."""
        # Navigate to source
        source = data[source_path] if source_path and source_path in data else data

        if isinstance(source, list) and source:
            sample = source[0]
        elif isinstance(source, dict):
            sample = source
        else:
            return {}

        # Build schema from sample
        def build_schema(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {k: build_schema(v) for k, v in list(obj.items())[:20]}
            elif isinstance(obj, list):
                if obj and isinstance(obj[0], dict):
                    return [build_schema(obj[0])]
                return ["item"]
            elif isinstance(obj, bool):
                return "boolean"
            elif isinstance(obj, int):
                return "integer"
            elif isinstance(obj, float):
                return "number"
            else:
                return "string"

        if source_path:
            return {source_path: [build_schema(sample)]}
        result = build_schema(sample)
        return result if isinstance(result, dict) else {"schema": result}

    def _format_small_response(
        self,
        data: Any,
        _question: str,
    ) -> str:
        """Format a small response for LLM context."""
        try:
            json_str = json.dumps(data, indent=2)
            if len(json_str) > 5000:
                json_str = json_str[:5000] + "\n... (truncated)"
            return f"API Response:\n```json\n{json_str}\n```"
        except (TypeError, ValueError):
            return f"API Response: {str(data)[:5000]}"

    def _format_reduced_response(
        self,
        reduced: ReducedData,
        question: str,
        query_output: QueryGeneratorOutput | None,
    ) -> str:
        """Format a reduced response for LLM interpretation."""
        lines = [
            "## Query Results",
            "",
            f"**Question:** {question}",
            "",
            "**Data Summary:**",
            f"- Total records in source: {reduced.total_source_records}",
            f"- Records matching filter: {reduced.total_after_filter}",
            f"- Records returned: {reduced.returned_records}",
        ]

        if reduced.is_truncated:
            lines.append(f"- ⚠️ Results truncated (showing top {reduced.returned_records})")

        if query_output and query_output.reasoning:
            lines.extend(
                [
                    "",
                    f"**Query Reasoning:** {query_output.reasoning}",
                ]
            )

        if reduced.aggregates:
            lines.extend(
                [
                    "",
                    "**Aggregates:**",
                ]
            )
            for name, value in reduced.aggregates.items():
                if isinstance(value, float):
                    lines.append(f"- {name}: {value:.2f}")
                else:
                    lines.append(f"- {name}: {value}")

        lines.extend(
            [
                "",
                "**Records:**",
                "```json",
            ]
        )

        # Add records (limited)
        records_json = json.dumps(reduced.records[:20], indent=2)
        lines.append(records_json)

        if len(reduced.records) > 20:
            lines.append(f"... and {len(reduced.records) - 20} more records")

        lines.append("```")

        return "\n".join(lines)

    # =========================================================================
    # Brain-Muscle Session Caching (TASK-91)
    # =========================================================================

    def cache_response(
        self,
        session_id: str,
        endpoint_id: str,
        endpoint_path: str,
        connector_id: str,
        response_schema: dict[str, Any],
        data: list[dict[str, Any]],
    ) -> CachedResponse:
        """
        Cache an API response for Brain-Muscle architecture (sync, L1 only).

        Stores the full response in in-memory cache. For Redis persistence,
        use cache_response_async() instead.

        Args:
            session_id: Session identifier
            endpoint_id: Endpoint that produced this data
            endpoint_path: API path (e.g., /api/resources)
            connector_id: Source connector
            response_schema: OpenAPI response schema (may contain x-meho-* extensions)
            data: The full response data to cache

        Returns:
            CachedResponse with summary ready for Brain
        """
        cache_key = f"{session_id}:{connector_id}:{endpoint_path}"

        # Extract schema summary (including x-meho-* extensions)
        schema_summary = self._extract_schema_summary(response_schema, endpoint_path)

        cached = CachedResponse(
            cache_key=cache_key,
            session_id=session_id,
            endpoint_id=endpoint_id,
            endpoint_path=endpoint_path,
            connector_id=connector_id,
            schema_summary=schema_summary,
            response_schema=response_schema,
            data=data,
            count=len(data),
        )

        # L1: In-memory cache
        self._session_cache[cache_key] = cached
        logger.info(
            f"📦 L1 Cached {len(data)} items from {endpoint_path} (key={cache_key[:40]}...)"
        )

        return cached

    async def cache_response_async(
        self,
        session_id: str,
        endpoint_id: str,
        endpoint_path: str,
        connector_id: str,
        response_schema: dict[str, Any],
        data: list[dict[str, Any]],
    ) -> CachedResponse:
        """
        Cache an API response with Redis persistence (TASK-93).

        Two-tier caching:
        - L1: In-memory (fast, per-process)
        - L2: Redis (persistent, shared across requests)

        Args:
            session_id: Session identifier
            endpoint_id: Endpoint that produced this data
            endpoint_path: API path (e.g., /api/resources)
            connector_id: Source connector
            response_schema: OpenAPI response schema
            data: The full response data to cache

        Returns:
            CachedResponse with summary ready for Brain
        """
        # First, cache in L1 (in-memory)
        cached = self.cache_response(
            session_id=session_id,
            endpoint_id=endpoint_id,
            endpoint_path=endpoint_path,
            connector_id=connector_id,
            response_schema=response_schema,
            data=data,
        )

        # Then persist to L2 (Redis) if available
        if self._redis:
            try:
                redis_key = f"{self._redis_key_prefix}:{cached.cache_key}"

                # Serialize CachedResponse for Redis
                redis_data = {
                    "cache_key": cached.cache_key,
                    "session_id": cached.session_id,
                    "endpoint_id": cached.endpoint_id,
                    "endpoint_path": cached.endpoint_path,
                    "connector_id": cached.connector_id,
                    "schema_summary": {
                        "identifier_field": cached.schema_summary.identifier_field,
                        "display_name_field": cached.schema_summary.display_name_field,
                        "entity_type": cached.schema_summary.entity_type,
                        "fields": cached.schema_summary.fields,
                        "field_types": cached.schema_summary.field_types,
                    },
                    "response_schema": cached.response_schema,
                    "data": cached.data,
                    "count": cached.count,
                    "timestamp": cached.timestamp.isoformat(),
                    "ttl_seconds": cached.ttl_seconds,
                }

                json_data = json.dumps(redis_data)
                ttl_seconds = int(self._cache_ttl.total_seconds())

                await self._redis.setex(redis_key, ttl_seconds, json_data)

                logger.info(
                    f"💾 L2 Redis cached {len(data)} items from {endpoint_path} "
                    f"(key={redis_key[:50]}..., TTL={ttl_seconds}s, size={len(json_data)} bytes)"
                )
            except Exception as e:
                # Don't fail if Redis write fails - L1 cache still works
                logger.warning(f"⚠️ Redis cache write failed (L1 still valid): {e}")

        return cached

    def get_cached(self, cache_key: str) -> CachedResponse | None:
        """Get cached response by key (L1 in-memory only)."""
        return self._session_cache.get(cache_key)

    async def get_cached_async(self, cache_key: str) -> CachedResponse | None:
        """
        Get cached response with Redis fallback (TASK-93).

        Checks L1 (in-memory) first, falls back to L2 (Redis).
        If found in Redis, populates L1 for future requests.

        Args:
            cache_key: The cache key to lookup

        Returns:
            CachedResponse if found, None otherwise
        """
        # L1: Check in-memory first (fast path)
        cached = self._session_cache.get(cache_key)
        if cached:
            logger.debug(f"📦 L1 cache hit: {cache_key[:40]}...")
            return cached

        # L2: Check Redis if available
        if not self._redis:
            return None

        try:
            redis_key = f"{self._redis_key_prefix}:{cache_key}"
            redis_data = await self._redis.get(redis_key)

            if not redis_data:
                logger.debug(f"📭 Cache miss (L1+L2): {cache_key[:40]}...")
                return None

            # Deserialize from Redis
            data = json.loads(redis_data)

            schema_summary = SchemaSummary(
                identifier_field=data["schema_summary"].get("identifier_field"),
                display_name_field=data["schema_summary"].get("display_name_field"),
                entity_type=data["schema_summary"].get("entity_type"),
                fields=data["schema_summary"].get("fields", []),
                field_types=data["schema_summary"].get("field_types", {}),
            )

            cached = CachedResponse(
                cache_key=data["cache_key"],
                session_id=data["session_id"],
                endpoint_id=data["endpoint_id"],
                endpoint_path=data["endpoint_path"],
                connector_id=data["connector_id"],
                schema_summary=schema_summary,
                response_schema=data["response_schema"],
                data=data["data"],
                count=data["count"],
                timestamp=datetime.fromisoformat(data["timestamp"]),
                ttl_seconds=data.get("ttl_seconds", 3600),
            )

            # Populate L1 cache for future requests
            self._session_cache[cache_key] = cached

            logger.info(
                f"📬 L2 Redis cache hit: {cache_key[:40]}... ({cached.count} items, populated L1)"
            )
            return cached

        except json.JSONDecodeError as e:
            logger.error(f"❌ Invalid JSON in Redis cache for {cache_key}: {e}")
            return None
        except Exception as e:
            logger.warning(f"⚠️ Redis cache read failed: {e}")
            return None

    def get_cached_for_session(self, session_id: str) -> list[CachedResponse]:
        """Get all cached responses for a session (L1 in-memory only)."""
        return [
            cached for cached in self._session_cache.values() if cached.session_id == session_id
        ]

    async def get_cached_for_session_async(
        self, session_id: str
    ) -> list[CachedResponse]:  # NOSONAR (cognitive complexity)
        """
        Get all cached responses for a session including Redis (TASK-93).

        Scans Redis for all keys matching the session pattern.

        Args:
            session_id: Session identifier

        Returns:
            List of CachedResponse objects for the session
        """
        # Start with L1 cache
        results = {
            c.cache_key: c for c in self._session_cache.values() if c.session_id == session_id
        }

        # Add from Redis if available
        if self._redis:
            try:
                # Scan for keys matching session pattern
                pattern = f"{self._redis_key_prefix}:{session_id}:*"
                cursor = 0

                while True:
                    cursor, keys = await self._redis.scan(cursor, match=pattern, count=100)

                    for redis_key in keys:
                        # Extract cache_key from redis_key
                        cache_key = redis_key.replace(f"{self._redis_key_prefix}:", "", 1)

                        # Skip if already in L1
                        if cache_key in results:
                            continue

                        # Load from Redis
                        cached = await self.get_cached_async(cache_key)
                        if cached:
                            results[cache_key] = cached

                    if cursor == 0:
                        break

                logger.info(
                    f"📋 Found {len(results)} cached responses for session {session_id[:8]}..."
                )
            except Exception as e:
                logger.warning(f"⚠️ Redis scan failed: {e}")

        return list(results.values())

    def execute_from_brain(
        self,
        cache_key: str,
        query: DataQuery,
    ) -> dict[str, Any]:
        """
        Execute a DataQuery from Brain on cached data (L1 only).

        This is the Brain→Muscle communication for data reduction.
        Brain sends a DataQuery, Muscle executes it and returns results.

        For Redis-backed execution, use execute_from_brain_async().

        Args:
            cache_key: Key of cached response to query
            query: DataQuery specifying filter/select/sort/aggregate

        Returns:
            Reduced data or error
        """
        cached = self._session_cache.get(cache_key)
        if not cached:
            return {"error": f"Cache key not found: {cache_key}"}

        return self._execute_query_on_cached(cached, query)

    async def execute_from_brain_async(
        self,
        cache_key: str,
        query: DataQuery,
    ) -> dict[str, Any]:
        """
        Execute a DataQuery from Brain on cached data with Redis fallback (TASK-93).

        Checks L1 first, falls back to L2 Redis.

        Args:
            cache_key: Key of cached response to query
            query: DataQuery specifying filter/select/sort/aggregate

        Returns:
            Reduced data or error
        """
        # Try to get cached data (L1 + L2)
        cached = await self.get_cached_async(cache_key)
        if not cached:
            return {"error": f"Cache key not found: {cache_key}"}

        return self._execute_query_on_cached(cached, query)

    def _execute_query_on_cached(
        self,
        cached: CachedResponse,
        query: DataQuery,
    ) -> dict[str, Any]:
        """Execute a DataQuery on a CachedResponse (internal helper)."""
        try:
            # Wrap data in dict if needed for execute_data_query
            source_data = {"data": cached.data} if cached.data else {}
            query.source_path = "data"  # Point to our wrapper

            reduced = execute_data_query(source_data, query)

            return {
                "success": True,
                "records": reduced.records,
                "count": reduced.returned_records,
                "total_matched": reduced.total_after_filter,
                "aggregates": reduced.aggregates,
            }
        except Exception as e:
            logger.error(f"Brain→Muscle reduction failed: {e}", exc_info=True)
            return {"error": str(e)}

    def lookup_entity(
        self,
        cache_key: str,
        match: str,
    ) -> dict[str, Any]:
        """
        Look up a specific entity in cached data (L1 only).

        Uses schema-driven lookup (x-meho-identifier, x-meho-display-name).
        NO hardcoded field patterns!

        For Redis-backed lookup, use lookup_entity_async().

        Args:
            cache_key: Key of cached response
            match: Entity ID or name to find

        Returns:
            Entity data or error
        """
        cached = self._session_cache.get(cache_key)
        if not cached:
            return {"error": f"Cache key not found: {cache_key}"}

        entity = cached.lookup_entity(match)
        if entity:
            return {"success": True, "entity": entity}
        else:
            return {"success": False, "error": f"Entity not found: {match}"}

    async def lookup_entity_async(
        self,
        cache_key: str,
        match: str,
    ) -> dict[str, Any]:
        """
        Look up a specific entity with Redis fallback (TASK-93).

        Args:
            cache_key: Key of cached response
            match: Entity ID or name to find

        Returns:
            Entity data or error
        """
        # Try to get cached data (L1 + L2)
        cached = await self.get_cached_async(cache_key)
        if not cached:
            return {"error": f"Cache key not found: {cache_key}"}

        entity = cached.lookup_entity(match)
        if entity:
            return {"success": True, "entity": entity}
        else:
            return {"success": False, "error": f"Entity not found: {match}"}

    def clear_session_cache(self, session_id: str) -> int:
        """Clear all cached data for a session (L1 only)."""
        keys_to_delete = [k for k, v in self._session_cache.items() if v.session_id == session_id]
        for key in keys_to_delete:
            del self._session_cache[key]
        return len(keys_to_delete)

    async def clear_session_cache_async(self, session_id: str) -> int:
        """
        Clear all cached data for a session including Redis (TASK-93).

        Args:
            session_id: Session to clear cache for

        Returns:
            Number of keys deleted
        """
        # Clear L1
        count = self.clear_session_cache(session_id)

        # Clear L2 (Redis)
        if self._redis:
            try:
                pattern = f"{self._redis_key_prefix}:{session_id}:*"
                cursor = 0
                redis_count = 0

                while True:
                    cursor, keys = await self._redis.scan(cursor, match=pattern, count=100)

                    if keys:
                        await self._redis.delete(*keys)
                        redis_count += len(keys)

                    if cursor == 0:
                        break

                logger.info(
                    f"🗑️ Cleared {redis_count} Redis cache keys for session {session_id[:8]}..."
                )
                count += redis_count
            except Exception as e:
                logger.warning(f"⚠️ Redis cache clear failed: {e}")

        return count

    # =========================================================================
    # SQL-based Data Query Methods (DuckDB)
    # =========================================================================

    def _derive_table_name(self, operation_id: str) -> str:
        """
        Derive a clean table name from operation ID.

        Examples:
            list_virtual_machines → virtual_machines
            get_all_clusters → clusters
            ListVMs → vms
        """
        import re

        # Remove common prefixes
        name = operation_id.lower()
        for prefix in ["list_", "get_all_", "get_", "fetch_", "retrieve_"]:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break

        # Convert camelCase to snake_case
        name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()

        # Clean up any double underscores
        name = re.sub(r"_+", "_", name)

        return name

    async def cache_as_table_async(
        self,
        session_id: str,
        operation_id: str,
        connector_id: str,
        data: list[dict[str, Any]],
    ) -> CachedTable:
        """
        Cache API response as a named SQL table in Redis.

        Uses Arrow-native Parquet serialization for efficient storage.
        Agent can query with SQL via reduce_data tool.

        Args:
            session_id: Session identifier
            operation_id: Operation that produced this data (e.g., "list_virtual_machines")
            connector_id: Source connector
            data: The response data as list of dicts

        Returns:
            CachedTable with metadata (data stays in Redis)
        """
        from meho_app.jsonflux.query.engine import QueryEngine

        # Derive table name from operation
        table_name = self._derive_table_name(operation_id)

        # Use QueryEngine for Arrow table creation (handles schema, normalization)
        engine = QueryEngine()
        engine.register(table_name, data, unwrap="auto")

        # Extract the Arrow table
        # Safe (sqlalchemy-execute-raw-query): DuckDB in-memory query, table name derived from _derive_table_name() using server-controlled operation metadata
        result = engine.conn.execute(f'SELECT * FROM "{table_name}"').arrow()
        arrow_table = result.read_all() if hasattr(result, "read_all") else result
        columns = arrow_table.column_names
        # Filter out placeholder columns
        columns = [c for c in columns if c != "_empty"]
        row_count = len(arrow_table)

        # Create CachedTable with Arrow table
        cached = CachedTable(
            table_name=table_name,
            operation_id=operation_id,
            connector_id=connector_id,
            columns=columns,
            row_count=row_count,
            cached_at=datetime.now(tz=UTC),
        )
        cached._df = arrow_table

        # Store in L1 (in-memory)
        if session_id not in self._session_tables:
            self._session_tables[session_id] = {}
        self._session_tables[session_id][table_name] = cached

        # Store in L2 (Redis) as Parquet (Arrow-native)
        cached_data = CachedData(
            cache_key=f"{session_id}:{table_name}",
            session_id=session_id,
            table_name=table_name,
            source_id=operation_id,
            source_path=operation_id,
            connector_id=connector_id,
            connector_type="typed",
            columns=columns,
            row_count=row_count,
            cached_at=cached.cached_at,
        )
        await self._persist_arrow_to_redis(session_id, table_name, cached_data, arrow_table)

        return cached

    async def cache_data_async(
        self,
        session_id: str,
        source_id: str,
        source_path: str,
        connector_id: str,
        connector_type: str,
        data: dict | list,
        entity_type: str | None = None,
        identifier_field: str | None = None,
        display_name_field: str | None = None,
    ) -> tuple[CachedData, ResponseTier]:
        """
        Unified cache for ALL connector types with token-aware tiering.

        Uses QueryEngine.register() for shape detection and Arrow tables
        for Parquet serialization -- pandas-free data path.

        Accepts raw API response data (dict or list).  Shape detection via
        register(unwrap='auto') handles wrapped collections, single objects,
        multi-collection responses, and flat arrays automatically.

        Args:
            session_id: Session identifier
            source_id: Endpoint ID or operation ID that produced this data
            source_path: Endpoint path or operation ID
            connector_id: Source connector UUID
            connector_type: Type of connector ("rest", "kubernetes", "vmware", etc.)
            data: Raw API response data (dict or list) -- shape detected automatically
            entity_type: Entity type name (e.g., "Namespace", "VirtualMachine")
            identifier_field: Field containing unique identifier (e.g., "uid")
            display_name_field: Field containing human-readable name (e.g., "name")

        Returns:
            Tuple of (CachedData, ResponseTier) where:
            - CachedData contains the cached data and metadata
            - ResponseTier indicates how much data should be returned to the LLM
        """
        from meho_app.jsonflux.query.engine import QueryEngine

        # Estimate tokens BEFORE processing
        estimated_tokens = estimate_tokens(data)
        tier = determine_response_tier(estimated_tokens)

        # Derive table name from source_id
        table_name = self._derive_table_name(source_id)

        # Use QueryEngine for shape detection and Arrow table creation
        engine = QueryEngine()
        engine.register(table_name, data, unwrap="auto")

        # Check tier_hint from register() -- single flat objects get inline
        tier_hint = None
        if table_name in engine.tables:
            tier_hint = engine.tables[table_name].get("tier_hint")
        if tier_hint == "inline":
            tier = ResponseTier.INLINE
            logger.info("📊 Cache: single object, tier_hint=inline -> forcing INLINE tier")

        # Extract the main Arrow table
        # Safe (sqlalchemy-execute-raw-query): DuckDB in-memory query, table name derived from _derive_table_name()
        result = engine.conn.execute(f'SELECT * FROM "{table_name}"').arrow()
        arrow_table = result.read_all() if hasattr(result, "read_all") else result
        columns = arrow_table.column_names
        # Filter out placeholder columns
        columns = [c for c in columns if c != "_empty"]
        row_count = len(arrow_table)

        data_len = len(data) if isinstance(data, list) else 1
        logger.info(f"📊 Cache: {data_len} items, ~{estimated_tokens:,} tokens -> {tier.value}")

        # Create cache key
        cache_key = f"{session_id}:{connector_id}:{source_id}"

        # Create CachedData with Arrow table
        cached = CachedData(
            cache_key=cache_key,
            session_id=session_id,
            table_name=table_name,
            source_id=source_id,
            source_path=source_path,
            connector_id=connector_id,
            connector_type=connector_type,
            entity_type=entity_type,
            identifier_field=identifier_field,
            display_name_field=display_name_field,
            columns=columns,
            row_count=row_count,
            estimated_tokens=estimated_tokens,
            cached_at=datetime.now(tz=UTC),
        )
        cached._df = arrow_table

        # Persist main table to L1 + L2
        compat_table = CachedTable(
            table_name=table_name,
            operation_id=source_id,
            connector_id=connector_id,
            columns=columns,
            row_count=row_count,
            cached_at=cached.cached_at,
        )
        compat_table._df = arrow_table

        if session_id not in self._session_tables:
            self._session_tables[session_id] = {}
        self._session_tables[session_id][table_name] = compat_table

        await self._persist_arrow_to_redis(session_id, table_name, cached, arrow_table)

        # Persist companion tables (e.g. _meta, scalar list tables)
        for tname in engine.tables:
            if tname != table_name:
                # Safe (sqlalchemy-execute-raw-query): DuckDB in-memory query, table names from server-controlled operation metadata
                tres = engine.conn.execute(f'SELECT * FROM "{tname}"').arrow()
                companion_arrow = tres.read_all() if hasattr(tres, "read_all") else tres
                comp_cols = [c for c in companion_arrow.column_names if c != "_empty"]
                comp_table = CachedTable(
                    table_name=tname,
                    operation_id=source_id,
                    connector_id=connector_id,
                    columns=comp_cols,
                    row_count=len(companion_arrow),
                    cached_at=cached.cached_at,
                )
                comp_table._df = companion_arrow
                self._session_tables[session_id][tname] = comp_table
                await self._persist_arrow_to_redis(session_id, tname, cached, companion_arrow)

        return cached, tier

    async def _persist_arrow_to_redis(
        self,
        session_id: str,
        table_name: str,
        cached: CachedData,
        arrow_table: pa.Table,
    ) -> None:
        """
        Persist Arrow table to Redis as Parquet bytes.

        Uses Arrow-native Parquet serialization (no pandas).
        """
        import io

        if not self._redis:
            return

        try:
            # Serialize Arrow table to Parquet directly (no pandas)
            buffer = io.BytesIO()
            pq.write_table(arrow_table, buffer, compression="snappy")
            parquet_bytes = buffer.getvalue()

            columns = [c for c in arrow_table.column_names if c != "_empty"]

            # Store metadata + data in Redis hash
            redis_key = f"{self._tables_key_prefix}:{session_id}:{table_name}"
            meta: dict[str, str] = {
                "table_name": table_name,
                "operation_id": cached.source_id,
                "connector_id": cached.connector_id,
                "connector_type": cached.connector_type,
                "columns": ",".join(columns),
                "row_count": str(len(arrow_table)),
                "cached_at": cached.cached_at.isoformat(),
                # Schema hints for Brain-Muscle architecture
                "entity_type": cached.entity_type or "",
                "identifier_field": cached.identifier_field or "",
                "display_name_field": cached.display_name_field or "",
            }

            # Use pipeline for atomic write
            pipe = self._redis.pipeline()
            pipe.hset(redis_key, mapping=meta)  # type: ignore[arg-type]  # redis-py stubs overly strict on mapping type
            pipe.hset(redis_key, "data", parquet_bytes)
            pipe.expire(redis_key, int(self._cache_ttl.total_seconds()))
            await pipe.execute()

            logger.info(
                f"💾 Arrow cache '{table_name}' persisted "
                f"({len(arrow_table)} rows, {len(parquet_bytes)} bytes Parquet)"
            )
        except Exception as e:
            logger.warning(f"⚠️ Redis cache failed (L1 still valid): {e}")

    async def get_session_tables_async(
        self, session_id: str
    ) -> dict[str, CachedTable]:  # NOSONAR (cognitive complexity)
        """
        Load all SQL tables for a session from Redis.

        Required for multi-turn conversations - loads cached data
        from previous requests.  Deserializes Parquet bytes to Arrow
        tables (no pandas).

        Args:
            session_id: Session identifier

        Returns:
            Dict of table_name -> CachedTable (with Arrow tables loaded)
        """
        import io

        # Check L1 first
        if session_id in self._session_tables:
            l1_tables = self._session_tables[session_id]
            if l1_tables:
                logger.debug(f"📦 L1 table cache hit: {len(l1_tables)} tables")
                return l1_tables

        # Load from L2 (Redis)
        if not self._redis:
            return {}

        try:
            pattern = f"{self._tables_key_prefix}:{session_id}:*"
            loaded_tables: dict[str, CachedTable] = {}

            async for key in self._redis.scan_iter(match=pattern):
                try:
                    # Get all fields from hash
                    data = await self._redis.hgetall(key)
                    if not data:
                        continue

                    # Parse metadata (string fields)
                    table_name = (
                        data.get("table_name", "").decode()
                        if isinstance(data.get("table_name"), bytes)
                        else data.get("table_name", "")
                    )
                    if not table_name:
                        continue

                    # Get Parquet data
                    parquet_data = data.get("data")
                    if not parquet_data:
                        continue

                    # Deserialize Arrow table from Parquet (no pandas)
                    if isinstance(parquet_data, str):
                        parquet_data = parquet_data.encode("latin-1")
                    arrow_table = pq.read_table(io.BytesIO(parquet_data))

                    # Parse other metadata
                    columns_str = data.get("columns", "")
                    if isinstance(columns_str, bytes):
                        columns_str = columns_str.decode()
                    columns = columns_str.split(",") if columns_str else arrow_table.column_names

                    row_count_str = data.get("row_count", "0")
                    if isinstance(row_count_str, bytes):
                        row_count_str = row_count_str.decode()
                    row_count = int(row_count_str)

                    operation_id = data.get("operation_id", "")
                    if isinstance(operation_id, bytes):
                        operation_id = operation_id.decode()

                    connector_id = data.get("connector_id", "")
                    if isinstance(connector_id, bytes):
                        connector_id = connector_id.decode()

                    cached_at_str = data.get("cached_at", "")
                    if isinstance(cached_at_str, bytes):
                        cached_at_str = cached_at_str.decode()
                    try:
                        cached_at = datetime.fromisoformat(cached_at_str)
                    except (ValueError, TypeError):
                        cached_at = datetime.now(tz=UTC)

                    # Create CachedTable with Arrow table
                    cached = CachedTable(
                        table_name=table_name,
                        operation_id=operation_id,
                        connector_id=connector_id,
                        columns=columns,
                        row_count=row_count,
                        cached_at=cached_at,
                    )
                    cached._df = arrow_table
                    loaded_tables[table_name] = cached

                except Exception as e:
                    logger.warning(f"⚠️ Failed to load table from {key}: {e}")
                    continue

            # Populate L1 cache
            if loaded_tables:
                self._session_tables[session_id] = loaded_tables
                logger.info(
                    f"📬 L2 Redis loaded {len(loaded_tables)} tables for session {session_id[:8]}..."
                )

            return loaded_tables

        except Exception as e:
            logger.error(f"❌ Failed to load session tables: {e}")
            return {}

    async def execute_sql_async(  # NOSONAR (cognitive complexity)
        self,
        session_id: str,
        sql: str,
    ) -> dict[str, Any]:
        """
        Execute SQL query on cached tables using DuckDB.

        Loads tables from Redis if not in memory, registers them
        with DuckDB, and executes the query.

        Args:
            session_id: Session identifier (scopes the tables)
            sql: SQL query to execute

        Returns:
            Dict with:
                - success: True/False
                - rows: List of result dicts
                - count: Number of results
                - columns: Column names
            Or:
                - error: Error message
        """
        from meho_app.jsonflux import QueryEngine

        # Load tables for this session
        tables = await self.get_session_tables_async(session_id)

        if not tables:
            return {
                "error": "No cached data for this session. Call an API first to cache data.",
                "hint": "Use call_operation to fetch data, then query with SQL.",
            }

        # Use QueryEngine for SQL execution (single SQL path)
        engine = QueryEngine()
        table_names = []
        try:
            for table_name, cached in tables.items():
                if cached.arrow_table is not None:
                    # Register Arrow tables directly with the DuckDB connection
                    # (no re-analysis via engine.register -- data is already Arrow)
                    engine.conn.register(table_name, cached.arrow_table)
                    engine.tables[table_name] = {
                        "source": "cache",
                        "row_count": cached.row_count,
                    }
                    table_names.append(table_name)

            logger.info(f"🦆 QueryEngine: Registered {len(table_names)} tables: {table_names}")

            # Execute SQL query via QueryEngine connection
            result = engine.conn.execute(sql)
            columns = [desc[0] for desc in result.description]
            raw_rows = result.fetchall()

            # Build list of dicts from fetchall result
            rows = [dict(zip(columns, row, strict=False)) for row in raw_rows]

            logger.info(f"✅ SQL executed: {len(rows)} rows returned")

            return {
                "success": True,
                "rows": rows,
                "count": len(rows),
                "columns": columns,
            }

        except Exception as e:
            err_str = str(e)

            # Build structured table/column info with types for error recovery
            table_column_info: dict[str, list[str]] = {}
            table_column_types: dict[str, dict[str, str]] = {}
            table_sample_rows: dict[str, dict[str, Any]] = {}
            for t, cached in tables.items():
                if t not in table_names:
                    continue
                table_column_info[t] = cached.columns
                # Extract column types from Arrow schema
                try:
                    arrow_tbl = cached.arrow_table
                    table_column_types[t] = {
                        name: str(arrow_tbl.schema.field(i).type)
                        for i, name in enumerate(arrow_tbl.schema.names)
                    }
                    # Include 1 sample row to help the LLM understand data shape
                    if len(arrow_tbl) > 0:
                        table_sample_rows[t] = arrow_tbl.slice(0, 1).to_pylist()[0]
                except Exception:
                    pass  # Arrow table may not be loaded; degrade gracefully

            if "Catalog Error" in err_str or ("Table" in err_str and "not found" in err_str):
                return {
                    "error": f"Table not found: {err_str}",
                    "available_tables": table_names,
                    "hint": f"Available tables: {', '.join(table_names)}. Check your table name in the SQL.",
                }
            if "Binder Error" in err_str:
                error_result: dict[str, Any] = {
                    "error": f"Column not found: {err_str}",
                    "available_tables": table_names,
                    "table_columns": table_column_info,
                    "hint": "Use only the columns listed in table_columns.",
                }
                if table_column_types:
                    error_result["table_column_types"] = table_column_types
                if table_sample_rows:
                    error_result["sample_rows"] = table_sample_rows
                return error_result
            if "Parser Error" in err_str or "syntax error" in err_str.lower():
                return {
                    "error": f"SQL syntax error: {err_str}",
                    "hint": "Check your SQL syntax. Common issues: missing quotes around strings, typos in column names.",
                }
            logger.error(f"❌ SQL execution failed: {e}", exc_info=True)
            fallback_result: dict[str, Any] = {
                "error": f"SQL execution failed: {err_str}",
                "available_tables": table_names,
                "table_columns": table_column_info,
            }
            if table_column_types:
                fallback_result["table_column_types"] = table_column_types
            if table_sample_rows:
                fallback_result["sample_rows"] = table_sample_rows
            return fallback_result

        finally:
            engine.close()

    def get_session_table_info(self, session_id: str) -> list[dict[str, Any]]:
        """
        Get info about cached tables for a session (L1 only, no Redis load).

        Used for prompt injection to tell agent what tables are available.
        """
        tables = self._session_tables.get(session_id, {})
        return [cached.to_summary() for cached in tables.values()]

    async def get_session_table_info_async(self, session_id: str) -> list[dict[str, Any]]:
        """
        Get info about cached tables for a session (loads from Redis if needed).

        Used for prompt injection to tell agent what tables are available.
        """
        tables = await self.get_session_tables_async(session_id)
        return [cached.to_summary() for cached in tables.values()]

    # =========================================================================
    # Legacy Schema Methods (kept for backward compatibility)
    # =========================================================================

    def _extract_schema_summary(
        self, response_schema: dict[str, Any], endpoint_path: str
    ) -> SchemaSummary:
        """
        Extract schema summary including x-meho-* extensions.

        Looks for OpenAPI extensions:
        - x-meho-identifier: true
        - x-meho-display-name: true
        - x-meho-entity-type: "resource"
        """
        summary = SchemaSummary()

        # Handle array responses (most common)
        items_schema = response_schema
        if response_schema.get("type") == "array":
            items_schema = response_schema.get("items", {})

        # Check for entity type at schema level
        summary.entity_type = items_schema.get("x-meho-entity-type")

        # If no entity type defined, derive from endpoint path (GENERIC!)
        if not summary.entity_type:
            summary.entity_type = self._derive_entity_type_from_path(endpoint_path)

        # Extract field info from properties
        properties = items_schema.get("properties", {})
        for prop_name, prop_def in properties.items():
            summary.fields.append(prop_name)
            if isinstance(prop_def, dict):
                summary.field_types[prop_name] = prop_def.get("type", "unknown")

                # Check for x-meho-* extensions
                if prop_def.get("x-meho-identifier"):
                    summary.identifier_field = prop_name
                if prop_def.get("x-meho-display-name"):
                    summary.display_name_field = prop_name

        return summary

    def _derive_entity_type_from_path(self, endpoint_path: str) -> str:
        """
        Derive entity type from endpoint path.

        Example: /api/resources → "resource"
                 /api/v1/items → "item"

        This is a GENERIC approach - not hardcoded to specific systems.
        """
        segments = [s for s in endpoint_path.split("/") if s and not s.startswith("{")]
        if segments:
            last = segments[-1].lower()
            # Simple singularization
            if last.endswith("ies"):
                return last[:-3] + "y"  # "entries" → "entry"
            elif last.endswith("ses"):
                return last[:-2]  # "addresses" → "address"
            elif last.endswith("s") and len(last) > 2:
                return last[:-1]  # "resources" → "resource"
            return last
        return "resource"


@dataclass
class ExecutionResult:
    """Result of unified execution."""

    # Processed data
    reduced_data: ReducedData | None
    raw_data: Any

    # Analysis and query
    analysis: ResponseAnalysis
    query_generated: QueryGeneratorOutput | None

    # For LLM
    llm_context: str

    @property
    def was_reduced(self) -> bool:
        """Whether data reduction was applied."""
        return self.reduced_data is not None

    @property
    def record_count(self) -> int:
        """Number of records in the result."""
        if self.reduced_data:
            return self.reduced_data.returned_records
        elif isinstance(self.raw_data, list):
            return len(self.raw_data)
        return 0


# =============================================================================
# Integration Helpers
# =============================================================================


async def process_api_response_for_llm(
    question: str,
    api_response: dict[str, Any],
    endpoint_path: str | None = None,
) -> str:
    """
    Convenience function to process an API response for LLM consumption.

    This is the simplest integration point - just pass the question and
    response, get back formatted text for the LLM.

    Args:
        question: The user's question
        api_response: Raw API response
        endpoint_path: Optional endpoint path for context

    Returns:
        Formatted string for LLM context
    """
    executor = UnifiedExecutor()
    result = await executor.process_response(
        question=question,
        api_response=api_response,
        endpoint_info={"path": endpoint_path} if endpoint_path else None,
    )
    return result.llm_context


def should_reduce_response(data: Any) -> bool:
    """
    Quick check if a response should be reduced.

    Args:
        data: API response data

    Returns:
        True if reduction is recommended
    """
    analysis = analyze_response(data)
    return analysis.needs_reduction


# =============================================================================
# Singleton Accessor
# =============================================================================

_unified_executor_instance: UnifiedExecutor | None = None


def get_unified_executor(redis_client: redis.Redis | None = None) -> UnifiedExecutor:
    """
    Get the global UnifiedExecutor instance (singleton).

    Args:
        redis_client: Optional Redis client for persistent cache (TASK-93).
                     If provided and instance doesn't have Redis, it will be set.

    Returns:
        UnifiedExecutor instance (singleton)
    """
    global _unified_executor_instance
    if _unified_executor_instance is None:
        _unified_executor_instance = UnifiedExecutor(redis_client=redis_client)
    elif redis_client and not _unified_executor_instance._redis:
        # Upgrade existing instance with Redis
        _unified_executor_instance._redis = redis_client
        logger.info("📦 UnifiedExecutor upgraded with Redis cache support")
    return _unified_executor_instance


def reset_unified_executor() -> None:
    """Reset the singleton (for testing)."""
    global _unified_executor_instance
    _unified_executor_instance = None
