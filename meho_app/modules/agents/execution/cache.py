# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Response caching for API responses.

Provides data structures for caching API responses and summaries
for the Brain-Muscle architecture.

TASK-161: Token-aware caching with intelligent tiering to prevent
LLM context overflow.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import pyarrow as pa

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


# =============================================================================
# Token Tier Thresholds (TASK-161)
# =============================================================================
# Configurable via environment variables for different deployment scenarios.
# Defaults are tuned for GPT-4 context window management.
#
# Simple 2-tier system:
#   INLINE: < 2K tokens (~8KB JSON) - return everything
#   CACHED: >= 2K tokens - metadata only, LLM must use SQL
#
# This binary approach prevents LLM hallucination by never giving
# partial data - either the LLM has all data or must query for it.

TOKEN_TIER_INLINE = int(os.getenv("MEHO_TOKEN_TIER_INLINE", "2000"))


class ResponseTier(Enum):
    """
    How much data to return to LLM based on token cost.

    Simple 2-tier system:
    - INLINE: Return everything (small data, no hallucination risk)
    - CACHED: Return metadata only (LLM must use SQL, no hallucination)
    """

    INLINE = "inline"  # < 2K tokens - return everything
    CACHED = "cached"  # >= 2K tokens - metadata only, use SQL


# =============================================================================
# Token Estimation Functions (TASK-161)
# =============================================================================


def estimate_tokens(data: Any) -> int:
    """
    Fast token estimation for JSON-serializable data.

    Uses character count / 4 as approximation, which is the industry
    standard for estimating GPT token counts from text. This is much
    faster than using tiktoken for exact counts and sufficient for
    tiering decisions.

    The approximation works well because:
    - Average English word is ~4-5 characters
    - JSON structure adds predictable overhead
    - We're making tiering decisions, not billing calculations

    Args:
        data: Any JSON-serializable data (dict, list, primitive)

    Returns:
        Estimated token count (integer)

    Examples:
        >>> estimate_tokens({"name": "default"})
        5  # ~20 chars / 4
        >>> estimate_tokens([{"id": i} for i in range(100)])
        ~300  # Depends on actual serialization
    """
    try:
        # Serialize to JSON string for accurate character count
        json_str = json.dumps(data, default=str)
        return len(json_str) // 4
    except (TypeError, ValueError) as e:
        # Fallback for non-serializable data
        logger.warning(f"JSON serialization failed, using str() fallback: {e}")
        return len(str(data)) // 4


def determine_response_tier(estimated_tokens: int) -> ResponseTier:
    """
    Determine appropriate response tier based on token count.

    Simple binary decision:
    - INLINE: Return full data (small enough for LLM context)
    - CACHED: Return metadata only (LLM must use SQL to get data)

    This prevents LLM hallucination by never giving partial data.
    Either the LLM has everything or it must query for it.

    Threshold configurable via MEHO_TOKEN_TIER_INLINE env var (default 2000).

    Args:
        estimated_tokens: Token count from estimate_tokens()

    Returns:
        ResponseTier enum value

    Examples:
        >>> determine_response_tier(500)
        ResponseTier.INLINE
        >>> determine_response_tier(5000)
        ResponseTier.CACHED
    """
    if estimated_tokens < TOKEN_TIER_INLINE:
        return ResponseTier.INLINE
    return ResponseTier.CACHED


@dataclass
class CachedTable:
    """
    A cached API response as a queryable SQL table.

    Stored in Redis as Parquet for efficient multi-turn conversations.
    Agent queries with SQL via DuckDB.

    Internal storage is a PyArrow Table (``_df`` field).  Use ``.arrow_table``
    or ``.to_pylist()`` to access data.
    """

    table_name: str  # e.g., "virtual_machines" (derived from operation)
    operation_id: str  # Original operation (e.g., "list_virtual_machines")
    connector_id: str  # Source connector
    columns: list[str]  # Column names for schema
    row_count: int  # Number of rows
    cached_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # In-memory Arrow table (loaded from Redis on demand)
    _df: Any | None = field(default=None, repr=False)  # pa.Table

    @property
    def arrow_table(self) -> pa.Table:
        """Get the Arrow table (must be loaded first)."""
        if self._df is None:
            raise ValueError(f"Arrow table not loaded for table '{self.table_name}'")
        if isinstance(self._df, pa.Table):
            return self._df
        # Legacy path: if somehow a DataFrame was set, convert
        return pa.Table.from_pandas(self._df)

    def to_pylist(self) -> list[dict[Any, Any]]:
        """Convert to list of dicts (Arrow-native, no pandas)."""
        result: list[dict[Any, Any]] = self.arrow_table.to_pylist()
        return result

    def to_summary(self) -> dict[str, Any]:
        """Create a summary for the agent (no data, just schema)."""
        return {
            "table": self.table_name,
            "operation": self.operation_id,
            "connector_id": self.connector_id,
            "columns": self.columns,
            "row_count": self.row_count,
            "cached_at": self.cached_at.isoformat(),
        }


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
    identifier_field: str | None = None  # x-meho-identifier
    display_name_field: str | None = None  # x-meho-display-name
    entity_type: str | None = None  # x-meho-entity-type

    # Extracted from schema properties
    fields: list[str] = field(default_factory=list)
    field_types: dict[str, str] = field(default_factory=dict)  # field -> type

    def to_brain_format(self) -> dict[str, Any]:
        """Format for inclusion in Brain prompt."""
        result: dict[str, Any] = {
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
    response_schema: dict[str, Any]  # Full schema (for Muscle operations)

    # The actual data (STAYS ON SERVER!)
    data: list[dict[str, Any]]
    count: int

    # Metadata
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    ttl_seconds: int = 3600  # Default 1 hour TTL

    def summarize_for_brain(self, sample_size: int = 3) -> dict[str, Any]:
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

    def lookup_entity(self, match: str) -> dict[str, Any] | None:  # NOSONAR (cognitive complexity)
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
            for _field_name, field_value in item.items():
                if isinstance(field_value, str) and field_value.lower() == match_lower:
                    return item

        return None


@dataclass
class CachedData:
    """
    Unified cache for ALL connector types (REST and typed).

    This dataclass combines the best of CachedResponse (schema hints) and
    CachedTable (SQL queryability) into a single unified structure.

    Key features:
    - Works with any connector type (REST, Kubernetes, VMware, Proxmox, GCP)
    - Always includes schema hints (entity_type, identifier, display_name)
    - Supports tier-based LLM responses to prevent context overflow
    - SQL-queryable via table_name
    """

    # Identity
    cache_key: str
    session_id: str
    table_name: str  # For SQL queries (e.g., "namespaces", "virtual_machines")

    # Source (connector-agnostic)
    source_id: str  # endpoint_id OR operation_id
    source_path: str  # endpoint path OR operation_id
    connector_id: str
    connector_type: str  # "rest", "kubernetes", "vmware", "proxmox", "gcp"

    # Schema hints (from OpenAPI x-meho-* OR OperationDefinition)
    entity_type: str | None = None  # "Namespace", "VirtualMachine", "Pod"
    identifier_field: str | None = None  # "uid", "moref_id", "id"
    display_name_field: str | None = None  # "name", "display_name"

    # Data metadata
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    estimated_tokens: int = 0
    cached_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # In-memory Arrow table (loaded from Redis on demand)
    _df: Any | None = field(default=None, repr=False)  # pa.Table

    @property
    def arrow_table(self) -> pa.Table:
        """Get the Arrow table (must be loaded first)."""
        if self._df is None:
            raise ValueError(f"Arrow table not loaded for table '{self.table_name}'")
        if isinstance(self._df, pa.Table):
            return self._df
        return pa.Table.from_pandas(self._df)

    def to_pylist(self) -> list[dict[Any, Any]]:
        """Convert to list of dicts (Arrow-native, no pandas)."""
        result: list[dict[Any, Any]] = self.arrow_table.to_pylist()
        return result

    def to_llm_summary(self, tier: ResponseTier) -> dict[str, Any]:
        """
        Create tier-appropriate summary for LLM.

        Simple 2-tier response:
        - INLINE: Full data (LLM can answer directly)
        - CACHED: Metadata only with ACTION REQUIRED signal (LLM must use SQL)

        This prevents hallucination by never giving partial data.

        Args:
            tier: ResponseTier indicating how much data to include

        Returns:
            Dict suitable for JSON serialization to LLM
        """
        entity_label = self.entity_type or "items"
        name_col = self.display_name_field or "name"

        # Extract column types from Arrow schema when available
        column_types: dict[str, str] = {}
        if self._df is not None and isinstance(self._df, pa.Table):
            for i, name in enumerate(self._df.schema.names):
                column_types[name] = str(self._df.schema.field(i).type)

        if tier == ResponseTier.INLINE:
            # Return everything - small enough for LLM context
            base: dict[str, Any] = {
                "success": True,
                "data_available": True,
                "cached": False,
                "table": self.table_name,
                "count": self.row_count,
                "columns": self.columns,
                "column_types": column_types,
                "schema": {
                    "entity_type": self.entity_type,
                    "identifier": self.identifier_field,
                    "display_name": self.display_name_field,
                },
            }
            if self._df is not None:
                if isinstance(self._df, pa.Table):
                    base["data"] = self._df.to_pylist()
                else:
                    base["data"] = []
            else:
                base["data"] = []
            base["message"] = f"Retrieved {self.row_count} {entity_label}."
        else:  # CACHED
            # NO data - explicit ACTION REQUIRED signal forces LLM to use reduce_data
            base = {
                "success": True,
                "data_available": False,  # CRITICAL: LLM must check this!
                "action_required": "reduce_data",  # CRITICAL: Tells LLM what to do
                "cached": True,
                "table": self.table_name,
                "row_count": self.row_count,
                "columns": self.columns,
                "column_types": column_types,
                "schema": {
                    "entity_type": self.entity_type,
                    "identifier": self.identifier_field,
                    "display_name": self.display_name_field,
                },
                "message": (
                    f"Data cached but NOT returned. You do NOT have the actual data. "
                    f"You MUST call reduce_data with SQL to retrieve the {self.row_count} {entity_label}."
                ),
                "next_step": {
                    "tool": "reduce_data",
                    "example_sql": f"SELECT {name_col} FROM {self.table_name}",  # noqa: S608 -- static SQL query, no user input
                },
            }

        return base

    def to_summary(self) -> dict[str, Any]:
        """
        Create a summary for the agent (no data, just schema).

        Backwards compatible with CachedTable.to_summary().
        """
        return {
            "table": self.table_name,
            "source": self.source_id,
            "connector_id": self.connector_id,
            "connector_type": self.connector_type,
            "columns": self.columns,
            "row_count": self.row_count,
            "entity_type": self.entity_type,
            "identifier_field": self.identifier_field,
            "display_name_field": self.display_name_field,
            "cached_at": self.cached_at.isoformat(),
        }
