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
5. Session caching - Store full responses for Brain-Muscle architecture (TASK-91)
6. Schema parsing - Extract x-meho-* extensions from OpenAPI specs

Brain-Muscle Architecture (TASK-91):
- MUSCLE stores full responses server-side
- BRAIN receives summaries only (schema + count + sample)
- Schema drives entity lookup (no hardcoded field patterns!)
- Brain can request filter/map/reduce operations on cached data
"""

from __future__ import annotations

import logging
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

import redis.asyncio as redis

from meho_agent.data_reduction import (
    DataQuery,
    DataReductionEngine,
    ReducedData,
    generate_query,
    QueryGeneratorOutput,
)

# Import AgentSessionState for entity side-channel
# Using TYPE_CHECKING to avoid circular imports
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from meho_agent.session_state import AgentSessionState

logger = logging.getLogger(__name__)


# =============================================================================
# Response Analysis
# =============================================================================

@dataclass
class ResponseAnalysis:
    """Analysis of an API response to determine processing strategy."""
    
    # Size metrics
    total_records: int = 0
    estimated_size_bytes: int = 0
    
    # Structure info
    source_path: str = ""
    detected_fields: list[str] = field(default_factory=list)
    
    # Processing recommendation
    needs_reduction: bool = False
    reason: str = ""
    
    @property
    def size_kb(self) -> float:
        return self.estimated_size_bytes / 1024
    
    @property
    def is_large(self) -> bool:
        """Whether response is considered large (>100KB or >100 records)."""
        return self.estimated_size_bytes > 100 * 1024 or self.total_records > 100


def analyze_response(data: Any) -> ResponseAnalysis:
    """
    Analyze an API response to determine processing needs.
    
    Args:
        data: The raw API response
        
    Returns:
        ResponseAnalysis with metrics and recommendations
    """
    analysis = ResponseAnalysis()
    
    # Estimate size
    try:
        json_str = json.dumps(data)
        analysis.estimated_size_bytes = len(json_str.encode('utf-8'))
    except (TypeError, ValueError):
        analysis.estimated_size_bytes = 0
    
    # Find the data source path and count records
    if isinstance(data, list):
        analysis.source_path = ""
        analysis.total_records = len(data)
        if data and isinstance(data[0], dict):
            analysis.detected_fields = list(data[0].keys())
    elif isinstance(data, dict):
        # Look for common array patterns
        for key in ["items", "data", "results", "records", "clusters", "pods", "vms"]:
            if key in data and isinstance(data[key], list):
                analysis.source_path = key
                analysis.total_records = len(data[key])
                if data[key] and isinstance(data[key][0], dict):
                    analysis.detected_fields = list(data[key][0].keys())
                break
        
        # If no array found, check for nested data
        if not analysis.source_path:
            for key, value in data.items():
                if isinstance(value, list) and len(value) > 5:
                    analysis.source_path = key
                    analysis.total_records = len(value)
                    if value and isinstance(value[0], dict):
                        analysis.detected_fields = list(value[0].keys())
                    break
    
    # Determine if reduction is needed
    if analysis.total_records > 50:
        analysis.needs_reduction = True
        analysis.reason = f"Large dataset ({analysis.total_records} records)"
    elif analysis.estimated_size_bytes > 50 * 1024:
        analysis.needs_reduction = True
        analysis.reason = f"Large response ({analysis.size_kb:.1f}KB)"
    
    return analysis


# =============================================================================
# SQL-based Data Query Architecture (DuckDB)
# =============================================================================

@dataclass
class CachedTable:
    """
    A cached API response as a queryable SQL table.
    
    Stored in Redis as Parquet for efficient multi-turn conversations.
    Agent queries with SQL via DuckDB.
    """
    
    table_name: str           # e.g., "virtual_machines" (derived from operation)
    operation_id: str         # Original operation (e.g., "list_virtual_machines")
    connector_id: str         # Source connector
    columns: List[str]        # Column names for schema
    row_count: int            # Number of rows
    cached_at: datetime = field(default_factory=datetime.utcnow)
    
    # In-memory DataFrame (loaded from Redis on demand)
    _df: Optional[Any] = field(default=None, repr=False)  # pd.DataFrame
    
    @property
    def df(self) -> Any:  # pd.DataFrame
        """Get the DataFrame (must be loaded first)."""
        if self._df is None:
            raise ValueError(f"DataFrame not loaded for table '{self.table_name}'")
        return self._df
    
    @df.setter
    def df(self, value: Any) -> None:
        """Set the DataFrame."""
        self._df = value
    
    def to_summary(self) -> Dict[str, Any]:
        """Create a summary for the agent (no data, just schema)."""
        return {
            "table": self.table_name,
            "operation": self.operation_id,
            "connector_id": self.connector_id,
            "columns": self.columns,
            "row_count": self.row_count,
            "cached_at": self.cached_at.isoformat(),
        }


# =============================================================================
# Brain-Muscle Architecture (TASK-91) - Legacy, being replaced by SQL
# =============================================================================

@dataclass
class SchemaSummary:
    """
    Summary of response schema for Brain consumption.
    
    Extracted from OpenAPI response schema, including any x-meho-* extensions.
    This is what the Brain sees - NOT the full schema.
    
    OpenAPI Extensions (operators can add to their specs):
    - x-meho-identifier: true      # Mark field as unique ID
    - x-meho-display-name: true    # Mark field as human-readable name
    - x-meho-entity-type: "resource"  # Define entity type name (optional)
    """
    
    # From x-meho-* extensions (if present in OpenAPI spec)
    identifier_field: Optional[str] = None  # x-meho-identifier
    display_name_field: Optional[str] = None  # x-meho-display-name
    entity_type: Optional[str] = None  # x-meho-entity-type
    
    # Extracted from schema properties
    fields: list[str] = field(default_factory=list)
    field_types: Dict[str, str] = field(default_factory=dict)  # field -> type
    
    def to_brain_format(self) -> Dict[str, Any]:
        """Format for inclusion in Brain prompt."""
        result: Dict[str, Any] = {
            "fields": self.fields,
        }
        if self.identifier_field:
            result["identifier"] = self.identifier_field
        if self.display_name_field:
            result["display_name"] = self.display_name_field
        if self.entity_type:
            result["entity_type"] = self.entity_type
        return result


@dataclass
class CachedResponse:
    """
    Server-side cache of an API response with schema context.
    
    This is what MUSCLE stores. Brain only sees a summary.
    """
    
    # Identity
    cache_key: str  # Unique key for this cached response
    session_id: str  # Session this belongs to
    
    # Source
    endpoint_id: str
    endpoint_path: str
    connector_id: str
    
    # Schema context (from OpenAPI spec)
    schema_summary: SchemaSummary
    response_schema: Dict[str, Any]  # Full schema (for Muscle operations)
    
    # The actual data (STAYS ON SERVER!)
    data: List[Dict[str, Any]]
    count: int
    
    # Metadata
    timestamp: datetime = field(default_factory=datetime.utcnow)
    ttl_seconds: int = 3600  # Default 1 hour TTL
    
    def summarize_for_brain(self, sample_size: int = 3) -> Dict[str, Any]:
        """
        Create a summary for Brain consumption.
        
        Brain sees:
        - cache_key (to reference this data)
        - endpoint info
        - count
        - schema summary (fields, identifier, display name - from x-meho-* extensions)
        - small sample of data
        
        Brain does NOT see:
        - Full data array
        - Full response schema
        """
        sample = self.data[:sample_size] if self.data else []
        
        return {
            "cache_key": self.cache_key,
            "endpoint": self.endpoint_path,
            "connector_id": self.connector_id,
            "count": self.count,
            "schema": self.schema_summary.to_brain_format(),
            "sample": sample,
            "cached_at": self.timestamp.isoformat(),
        }
    
    def lookup_entity(self, match: str) -> Optional[Dict[str, Any]]:
        """
        Find an entity by identifier or display name.
        
        Uses schema to determine which fields to search:
        1. If x-meho-identifier is set, search that field
        2. If x-meho-display-name is set, also search that field
        3. Fall back to searching all string fields
        
        NO HARDCODED FIELD NAMES - schema-driven!
        """
        if not self.data:
            return None
        
        match_lower = match.lower()
        
        # Priority 1: Search identifier field (if defined in schema)
        if self.schema_summary.identifier_field:
            for item in self.data:
                value = item.get(self.schema_summary.identifier_field)
                if value and str(value).lower() == match_lower:
                    return item
        
        # Priority 2: Search display name field (if defined in schema)
        if self.schema_summary.display_name_field:
            for item in self.data:
                value = item.get(self.schema_summary.display_name_field)
                if value and str(value).lower() == match_lower:
                    return item
        
        # Priority 3: Search all string fields (generic fallback)
        for item in self.data:
            for field_name, field_value in item.items():
                if isinstance(field_value, str) and field_value.lower() == match_lower:
                    return item
        
        return None


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
        data_engine: Optional[DataReductionEngine] = None,
        auto_reduce_threshold: int = 50,  # Records
        auto_reduce_size_kb: int = 50,    # KB
        redis_client: Optional[redis.Redis] = None,
        cache_ttl: timedelta = timedelta(hours=1),
    ):
        """
        Initialize the unified executor.
        
        Args:
            data_engine: Data reduction engine (created if not provided)
            auto_reduce_threshold: Record count threshold for auto-reduction
            auto_reduce_size_kb: Size threshold for auto-reduction
            redis_client: Optional Redis client for persistent cache (TASK-93)
            cache_ttl: TTL for cached responses in Redis (default 1 hour)
        """
        self.data_engine = data_engine or DataReductionEngine()
        self.auto_reduce_threshold = auto_reduce_threshold
        self.auto_reduce_size_kb = auto_reduce_size_kb
        
        # Redis for persistent cache (TASK-93: Response Cache Persistence)
        self._redis = redis_client
        self._cache_ttl = cache_ttl
        self._redis_key_prefix = "meho:cache"
        self._tables_key_prefix = "meho:tables"  # SQL tables (new architecture)
        
        # L1 in-memory cache (fast, per-process)
        # L2 Redis cache (persistent, shared across requests)
        self._session_cache: Dict[str, CachedResponse] = {}
        
        # SQL Tables cache: session_id -> table_name -> CachedTable
        self._session_tables: Dict[str, Dict[str, CachedTable]] = {}
    
    async def process_response(
        self,
        question: str,
        api_response: dict[str, Any],
        endpoint_info: Optional[dict[str, Any]] = None,
        force_reduction: bool = False,
        session_state: Optional["AgentSessionState"] = None,
        connector_id: Optional[str] = None,
    ) -> ExecutionResult:
        """
        Process an API response with intelligent data reduction.
        
        This is the main entry point for the unified execution flow.
        
        IMPORTANT: The Entity Side-Channel Pattern
        ------------------------------------------
        When session_state is provided, this method will ALSO populate
        entities from the raw response into the session state BEFORE
        reducing the data. This ensures:
        
        1. The LLM sees only the reduced/summarized data (small context)
        2. The agent's memory has ALL entities for future reference
        3. User can say "delete those VMs" even if LLM only saw aggregates
        
        Args:
            question: The user's original question
            api_response: Raw API response data
            endpoint_info: Optional endpoint metadata (path, method, etc.)
            force_reduction: Force data reduction even for small responses
            session_state: Optional AgentSessionState for entity side-channel
            connector_id: Connector ID for entity attribution
            
        Returns:
            ExecutionResult with reduced data and LLM context
        """
        # Step 1: Analyze the response
        analysis = analyze_response(api_response)
        
        logger.info(
            f"Response analysis: {analysis.total_records} records, "
            f"{analysis.size_kb:.1f}KB, source_path='{analysis.source_path}'"
        )
        
        # Step 2: ENTITY SIDE-CHANNEL - Populate session state with ALL entities
        # This happens BEFORE reduction so we don't lose entity references
        entities_populated = 0
        if session_state and connector_id and analysis.total_records > 0:
            entities_populated = self._populate_entities_side_channel(
                api_response=api_response,
                analysis=analysis,
                session_state=session_state,
                connector_id=connector_id,
                endpoint_info=endpoint_info,
            )
            if entities_populated > 0:
                logger.info(
                    f"📦 Entity Side-Channel: Populated {entities_populated} entities "
                    f"into session state (LLM will only see reduced data)"
                )
        
        # Step 3: Determine if reduction is needed
        should_reduce = (
            force_reduction or
            analysis.needs_reduction or
            analysis.total_records > self.auto_reduce_threshold or
            analysis.estimated_size_bytes > self.auto_reduce_size_kb * 1024
        )
        
        if not should_reduce:
            # Small response - return as-is
            return ExecutionResult(
                reduced_data=None,
                raw_data=api_response,
                analysis=analysis,
                query_generated=None,
                llm_context=self._format_small_response(api_response, question),
                entities_populated=entities_populated,
            )
        
        # Step 4: Generate a query from the question
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
        
        # Step 5: Execute the query
        reduced = self.data_engine.execute(api_response, query)
        
        logger.info(
            f"Data reduction: {reduced.total_source_records} → "
            f"{reduced.returned_records} records ({reduced.reduction_ratio:.1%})"
        )
        
        # Step 6: Format for LLM
        llm_context = self._format_reduced_response(reduced, question, query_output)
        
        return ExecutionResult(
            reduced_data=reduced,
            raw_data=api_response,
            analysis=analysis,
            query_generated=query_output,
            llm_context=llm_context,
            entities_populated=entities_populated,
        )
    
    def _extract_schema(
        self,
        data: dict[str, Any],
        source_path: str,
    ) -> dict[str, Any]:
        """Extract a schema from sample data."""
        # Navigate to source
        if source_path and source_path in data:
            source = data[source_path]
        else:
            source = data
        
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
    
    def _populate_entities_side_channel(
        self,
        api_response: dict[str, Any],
        analysis: ResponseAnalysis,
        session_state: "AgentSessionState",
        connector_id: str,
        endpoint_info: Optional[dict[str, Any]] = None,
    ) -> int:
        """
        Populate entities from API response into session state.
        
        This is the "side-channel" that ensures the agent's memory
        contains all entities even when the LLM only sees aggregates.
        
        Entity type detection:
        - From endpoint path: /api/vms → "vm"
        - From source_path: "clusters" → "cluster"
        - From data structure: items with "vm" field → "vm"
        
        Args:
            api_response: Raw API response
            analysis: Response analysis with source_path
            session_state: State to populate
            connector_id: Source connector
            endpoint_info: Endpoint metadata for type hints
            
        Returns:
            Number of entities populated
        """
        # Extract the items list
        if analysis.source_path and analysis.source_path in api_response:
            items = api_response[analysis.source_path]
        elif isinstance(api_response, list):
            items = api_response
        else:
            # Try to find items in the response
            items = None
            for key in ["items", "data", "results", "records"]:
                if key in api_response and isinstance(api_response[key], list):
                    items = api_response[key]
                    break
        
        if not items or not isinstance(items, list):
            return 0
        
        # Determine entity type
        entity_type = self._detect_entity_type(
            analysis.source_path,
            endpoint_info,
            items[0] if items else None
        )
        
        # Determine ID and name fields
        id_field, name_field = self._detect_id_name_fields(items[0] if items else {})
        
        # Add to session state
        session_state.add_entities_from_response(
            entity_type=entity_type,
            items=items,
            connector_id=connector_id,
            id_field=id_field,
            name_field=name_field,
        )
        
        return len(items)
    
    def _detect_entity_type(
        self,
        source_path: str,
        endpoint_info: Optional[dict[str, Any]],
        sample_item: Optional[dict[str, Any]],
    ) -> str:
        """
        Detect entity type GENERICALLY from endpoint path.
        
        NO hardcoded system-specific logic - works for ANY API!
        """
        # From endpoint path (preferred - most reliable)
        if endpoint_info and endpoint_info.get("path"):
            path: str = str(endpoint_info["path"])
            # Extract last path segment: /api/v1/clusters → clusters
            segments = [s for s in path.split("/") if s and not s.startswith("{")]
            if segments:
                last_segment: str = segments[-1].lower()
                # Singularize common patterns (generic, not system-specific)
                if last_segment.endswith("ies"):
                    return last_segment[:-3] + "y"  # "entries" → "entry"
                elif last_segment.endswith("ses"):
                    return last_segment[:-2]  # "addresses" → "address"
                elif last_segment.endswith("s") and len(last_segment) > 2:
                    return str(last_segment[:-1])  # "vms" → "vm"
                return last_segment
        
        # From source_path (fallback)
        if source_path:
            source_lower = source_path.lower()
            if source_lower.endswith("ies"):
                return source_lower[:-3] + "y"
            elif source_lower.endswith("ses"):
                return source_lower[:-2]
            elif source_lower.endswith("s") and len(source_lower) > 2:
                return source_lower[:-1]
            return source_lower
        
        return "resource"  # Generic fallback
    
    def _detect_id_name_fields(
        self,
        sample_item: dict[str, Any],
    ) -> tuple[str, str]:
        """Detect ID and name fields from sample item."""
        if not sample_item:
            return "id", "name"
        
        # Common ID field patterns
        id_candidates = ["id", "uuid", "uid", "vm", "pod", "node", "cluster"]
        id_field = "id"
        for candidate in id_candidates:
            if candidate in sample_item:
                id_field = candidate
                break
        
        # Common name field patterns
        name_candidates = ["name", "display_name", "displayName", "title", "label"]
        name_field = "name"
        for candidate in name_candidates:
            if candidate in sample_item:
                name_field = candidate
                break
        
        return id_field, name_field

    def _format_small_response(
        self,
        data: Any,
        question: str,
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
        query_output: Optional[QueryGeneratorOutput],
    ) -> str:
        """Format a reduced response for LLM interpretation."""
        lines = [
            f"## Query Results",
            f"",
            f"**Question:** {question}",
            f"",
            f"**Data Summary:**",
            f"- Total records in source: {reduced.total_source_records}",
            f"- Records matching filter: {reduced.total_after_filter}",
            f"- Records returned: {reduced.returned_records}",
        ]
        
        if reduced.is_truncated:
            lines.append(f"- ⚠️ Results truncated (showing top {reduced.returned_records})")
        
        if query_output and query_output.reasoning:
            lines.extend([
                f"",
                f"**Query Reasoning:** {query_output.reasoning}",
            ])
        
        if reduced.aggregates:
            lines.extend([
                f"",
                f"**Aggregates:**",
            ])
            for name, value in reduced.aggregates.items():
                if isinstance(value, float):
                    lines.append(f"- {name}: {value:.2f}")
                else:
                    lines.append(f"- {name}: {value}")
        
        lines.extend([
            f"",
            f"**Records:**",
            f"```json",
        ])
        
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
        response_schema: Dict[str, Any],
        data: List[Dict[str, Any]],
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
        logger.info(f"📦 L1 Cached {len(data)} items from {endpoint_path} (key={cache_key[:40]}...)")
        
        return cached
    
    async def cache_response_async(
        self,
        session_id: str,
        endpoint_id: str,
        endpoint_path: str,
        connector_id: str,
        response_schema: Dict[str, Any],
        data: List[Dict[str, Any]],
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
    
    def get_cached(self, cache_key: str) -> Optional[CachedResponse]:
        """Get cached response by key (L1 in-memory only)."""
        return self._session_cache.get(cache_key)
    
    async def get_cached_async(self, cache_key: str) -> Optional[CachedResponse]:
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
                f"📬 L2 Redis cache hit: {cache_key[:40]}... "
                f"({cached.count} items, populated L1)"
            )
            return cached
            
        except json.JSONDecodeError as e:
            logger.error(f"❌ Invalid JSON in Redis cache for {cache_key}: {e}")
            return None
        except Exception as e:
            logger.warning(f"⚠️ Redis cache read failed: {e}")
            return None
    
    def get_cached_for_session(self, session_id: str) -> List[CachedResponse]:
        """Get all cached responses for a session (L1 in-memory only)."""
        return [
            cached for cached in self._session_cache.values()
            if cached.session_id == session_id
        ]
    
    async def get_cached_for_session_async(self, session_id: str) -> List[CachedResponse]:
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
            c.cache_key: c for c in self._session_cache.values()
            if c.session_id == session_id
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
    ) -> Dict[str, Any]:
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
    ) -> Dict[str, Any]:
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
    ) -> Dict[str, Any]:
        """Execute a DataQuery on a CachedResponse (internal helper)."""
        try:
            # Wrap data in dict if needed for DataReductionEngine
            source_data = {"data": cached.data} if cached.data else {}
            query.source_path = "data"  # Point to our wrapper
            
            reduced = self.data_engine.execute(source_data, query)
            
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
    ) -> Dict[str, Any]:
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
    ) -> Dict[str, Any]:
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
                
                logger.info(f"🗑️ Cleared {redis_count} Redis cache keys for session {session_id[:8]}...")
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
        for prefix in ['list_', 'get_all_', 'get_', 'fetch_', 'retrieve_']:
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        
        # Convert camelCase to snake_case
        name = re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()
        
        # Clean up any double underscores
        name = re.sub(r'_+', '_', name)
        
        return name
    
    async def cache_as_table_async(
        self,
        session_id: str,
        operation_id: str,
        connector_id: str,
        data: List[Dict[str, Any]],
    ) -> CachedTable:
        """
        Cache API response as a named SQL table in Redis.
        
        Uses Parquet format for efficient storage (5x smaller than JSON).
        Agent can query with SQL via reduce_data tool.
        
        Args:
            session_id: Session identifier
            operation_id: Operation that produced this data (e.g., "list_virtual_machines")
            connector_id: Source connector
            data: The response data as list of dicts
            
        Returns:
            CachedTable with metadata (data stays in Redis)
        """
        import io
        import pandas as pd
        
        # Derive table name from operation
        table_name = self._derive_table_name(operation_id)
        
        # Convert to DataFrame
        df = pd.DataFrame(data)
        columns = list(df.columns)
        row_count = len(df)
        
        # Create CachedTable
        cached = CachedTable(
            table_name=table_name,
            operation_id=operation_id,
            connector_id=connector_id,
            columns=columns,
            row_count=row_count,
            cached_at=datetime.utcnow(),
        )
        cached.df = df
        
        # Store in L1 (in-memory)
        if session_id not in self._session_tables:
            self._session_tables[session_id] = {}
        self._session_tables[session_id][table_name] = cached
        
        # Store in L2 (Redis) as Parquet
        if self._redis:
            try:
                # Serialize DataFrame as Parquet (compact, type-safe)
                buffer = io.BytesIO()
                df.to_parquet(buffer, compression='snappy', index=False)
                parquet_bytes = buffer.getvalue()
                
                # Store metadata + data in Redis hash
                redis_key = f"{self._tables_key_prefix}:{session_id}:{table_name}"
                meta: Dict[str, str] = {
                    "table_name": table_name,
                    "operation_id": operation_id,
                    "connector_id": connector_id,
                    "columns": ",".join(columns),  # Store as comma-separated
                    "row_count": str(row_count),
                    "cached_at": cached.cached_at.isoformat(),
                }
                
                # Use pipeline for atomic write
                pipe = self._redis.pipeline()
                pipe.hset(redis_key, mapping=meta)  # type: ignore[arg-type]
                pipe.hset(redis_key, "data", parquet_bytes)
                pipe.expire(redis_key, int(self._cache_ttl.total_seconds()))
                await pipe.execute()
                
                logger.info(
                    f"💾 SQL Table '{table_name}' cached "
                    f"({row_count} rows, {len(parquet_bytes)} bytes Parquet)"
                )
            except Exception as e:
                logger.warning(f"⚠️ Redis table cache failed (L1 still valid): {e}")
        
        return cached
    
    async def get_session_tables_async(self, session_id: str) -> Dict[str, CachedTable]:
        """
        Load all SQL tables for a session from Redis.
        
        Required for multi-turn conversations - loads cached data
        from previous requests.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dict of table_name -> CachedTable (with DataFrames loaded)
        """
        import io
        import pandas as pd
        
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
            loaded_tables: Dict[str, CachedTable] = {}
            
            async for key in self._redis.scan_iter(match=pattern):
                try:
                    # Get all fields from hash
                    data = await self._redis.hgetall(key)
                    if not data:
                        continue
                    
                    # Parse metadata (string fields)
                    table_name = data.get("table_name", "").decode() if isinstance(data.get("table_name"), bytes) else data.get("table_name", "")
                    if not table_name:
                        continue
                    
                    # Get Parquet data
                    parquet_data = data.get("data")
                    if not parquet_data:
                        continue
                    
                    # Deserialize DataFrame
                    if isinstance(parquet_data, str):
                        parquet_data = parquet_data.encode('latin-1')
                    df = pd.read_parquet(io.BytesIO(parquet_data))
                    
                    # Parse other metadata
                    columns_str = data.get("columns", "")
                    if isinstance(columns_str, bytes):
                        columns_str = columns_str.decode()
                    columns = columns_str.split(",") if columns_str else list(df.columns)
                    
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
                        cached_at = datetime.utcnow()
                    
                    # Create CachedTable
                    cached = CachedTable(
                        table_name=table_name,
                        operation_id=operation_id,
                        connector_id=connector_id,
                        columns=columns,
                        row_count=row_count,
                        cached_at=cached_at,
                    )
                    cached.df = df
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
    
    async def execute_sql_async(
        self,
        session_id: str,
        sql: str,
    ) -> Dict[str, Any]:
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
        import duckdb
        
        # Load tables for this session
        tables = await self.get_session_tables_async(session_id)
        
        if not tables:
            return {
                "error": "No cached data for this session. Call an API first to cache data.",
                "hint": "Use call_operation to fetch data, then query with SQL."
            }
        
        # Register all tables with DuckDB
        conn = duckdb.connect()
        table_names = []
        for table_name, cached in tables.items():
            conn.register(table_name, cached.df)
            table_names.append(table_name)
        
        logger.info(f"🦆 DuckDB: Registered {len(table_names)} tables: {table_names}")
        
        try:
            # Execute SQL query
            result_df = conn.execute(sql).df()
            
            # Convert to list of dicts
            rows = result_df.to_dict('records')
            
            logger.info(f"✅ SQL executed: {len(rows)} rows returned")
            
            return {
                "success": True,
                "rows": rows,
                "count": len(rows),
                "columns": list(result_df.columns),
            }
            
        except duckdb.CatalogException as e:
            # Table not found - provide helpful error
            available = ", ".join(table_names) if table_names else "none"
            return {
                "error": f"Table not found: {str(e)}",
                "available_tables": available,
                "hint": f"Available tables: {available}. Check your table name in the SQL."
            }
            
        except duckdb.ParserException as e:
            return {
                "error": f"SQL syntax error: {str(e)}",
                "hint": "Check your SQL syntax. Common issues: missing quotes around strings, typos in column names."
            }
            
        except Exception as e:
            logger.error(f"❌ SQL execution failed: {e}", exc_info=True)
            return {"error": f"SQL execution failed: {str(e)}"}
        
        finally:
            conn.close()
    
    def get_session_table_info(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Get info about cached tables for a session (L1 only, no Redis load).
        
        Used for prompt injection to tell agent what tables are available.
        """
        tables = self._session_tables.get(session_id, {})
        return [cached.to_summary() for cached in tables.values()]
    
    async def get_session_table_info_async(self, session_id: str) -> List[Dict[str, Any]]:
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
        self, 
        response_schema: Dict[str, Any],
        endpoint_path: str
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
    reduced_data: Optional[ReducedData]
    raw_data: Any
    
    # Analysis and query
    analysis: ResponseAnalysis
    query_generated: Optional[QueryGeneratorOutput]
    
    # For LLM
    llm_context: str
    
    # Entity Side-Channel: How many entities were populated into session state
    # These entities are in memory even if the LLM only sees aggregates
    entities_populated: int = 0
    
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
    endpoint_path: Optional[str] = None,
    session_state: Optional["AgentSessionState"] = None,
    connector_id: Optional[str] = None,
) -> str:
    """
    Convenience function to process an API response for LLM consumption.
    
    This is the simplest integration point - just pass the question and
    response, get back formatted text for the LLM.
    
    IMPORTANT: Pass session_state and connector_id to enable the Entity
    Side-Channel pattern, which populates entities into memory even when
    the LLM only sees summarized data.
    
    Args:
        question: The user's question
        api_response: Raw API response
        endpoint_path: Optional endpoint path for context
        session_state: Optional session state for entity side-channel
        connector_id: Optional connector ID for entity attribution
        
    Returns:
        Formatted string for LLM context
    """
    executor = UnifiedExecutor()
    result = await executor.process_response(
        question=question,
        api_response=api_response,
        endpoint_info={"path": endpoint_path} if endpoint_path else None,
        session_state=session_state,
        connector_id=connector_id,
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

_unified_executor_instance: Optional[UnifiedExecutor] = None


def get_unified_executor(redis_client: Optional[redis.Redis] = None) -> UnifiedExecutor:
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

