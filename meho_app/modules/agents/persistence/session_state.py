# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Session state for multi-turn conversations in the new agent architecture.

This is the NEW equivalent of meho_app/modules/agent/session_state.py,
designed specifically for the orchestrator-based multi-agent system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class ConnectorMemory:
    """What we remember about a connector across turns.

    Lighter than legacy ConnectorContext - focused on orchestrator needs.
    """

    connector_id: str
    connector_name: str
    connector_type: str  # kubernetes, vmware, rest, etc.
    last_used: datetime

    # Endpoints we've successfully used
    used_endpoints: dict[str, str] = field(default_factory=dict)  # path -> endpoint_id

    # Last query sent to this connector (for context)
    last_query: str | None = None

    # Did the last call succeed?
    last_status: str = "unknown"  # success, failed, timeout

    def to_dict(self) -> dict[str, Any]:
        """Serialize for Redis storage."""
        return {
            "connector_id": self.connector_id,
            "connector_name": self.connector_name,
            "connector_type": self.connector_type,
            "last_used": self.last_used.isoformat(),
            "used_endpoints": self.used_endpoints,
            "last_query": self.last_query,
            "last_status": self.last_status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectorMemory:
        """Deserialize from Redis storage."""
        return cls(
            connector_id=data["connector_id"],
            connector_name=data["connector_name"],
            connector_type=data["connector_type"],
            last_used=datetime.fromisoformat(data["last_used"]),
            used_endpoints=data.get("used_endpoints", {}),
            last_query=data.get("last_query"),
            last_status=data.get("last_status", "unknown"),
        )


@dataclass
class OrchestratorSessionState:
    """
    Persistent state for multi-turn orchestrator conversations.

    This state is:
    - Loaded from Redis at request start
    - Updated during request execution
    - Saved to Redis at request end
    - Expires after 24 hours (configurable TTL)

    Design principles:
    - Keep it lightweight (fast serialization)
    - Focus on orchestrator-level context (not per-agent details)
    - Enable intelligent follow-up handling
    """

    # =========================================================================
    # CONNECTOR MEMORY
    # =========================================================================

    connectors: dict[str, ConnectorMemory] = field(default_factory=dict)
    """Connectors used in this conversation, keyed by connector_id."""

    primary_connector_id: str | None = None
    """The "current" connector for ambiguous follow-ups."""

    def remember_connector(
        self,
        connector_id: str,
        connector_name: str,
        connector_type: str,
        query: str | None = None,
        status: str = "success",
    ) -> None:
        """Record that we used a connector."""
        if connector_id not in self.connectors:
            self.connectors[connector_id] = ConnectorMemory(
                connector_id=connector_id,
                connector_name=connector_name,
                connector_type=connector_type,
                last_used=datetime.now(tz=UTC),
            )

        mem = self.connectors[connector_id]
        mem.last_used = datetime.now(tz=UTC)
        mem.last_query = query
        mem.last_status = status

        # Update primary if this succeeded
        if status == "success":
            self.primary_connector_id = connector_id

    def get_primary_connector(self) -> ConnectorMemory | None:
        """Get the current primary connector, or most recently successful."""
        if self.primary_connector_id and self.primary_connector_id in self.connectors:
            return self.connectors[self.primary_connector_id]

        # Fallback: most recently used successful connector
        successful = [c for c in self.connectors.values() if c.last_status == "success"]
        if successful:
            return max(successful, key=lambda c: c.last_used)

        return None

    # =========================================================================
    # OPERATION CONTEXT
    # =========================================================================

    current_operation: str | None = None
    """What the user is trying to accomplish (natural language)."""

    operation_entities: list[str] = field(default_factory=list)
    """Key entities being investigated (VM names, pod names, etc.)."""

    def set_operation_context(self, operation: str, entities: list[str] | None = None) -> None:
        """Track what the user is investigating."""
        self.current_operation = operation
        if entities:
            self.operation_entities = entities

    # =========================================================================
    # CACHED DATA REFERENCES
    # =========================================================================

    cached_tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    """
    References to cached API response data.

    Format: {table_name: {"connector_id": ..., "row_count": ..., "cached_at": ...}}

    The actual data lives in UnifiedExecutor's Redis cache.
    This just tracks what's available for follow-up queries.
    """

    def register_cached_data(
        self,
        table_name: str,
        connector_id: str,
        row_count: int,
    ) -> None:
        """Register that we have cached data available."""
        self.cached_tables[table_name] = {
            "connector_id": connector_id,
            "row_count": row_count,
            "cached_at": datetime.now(tz=UTC).isoformat(),
        }

    def get_available_tables(self) -> list[str]:
        """Get list of tables available for SQL queries."""
        return list(self.cached_tables.keys())

    # =========================================================================
    # ERROR TRACKING
    # =========================================================================

    recent_errors: list[dict[str, Any]] = field(default_factory=list)
    """Recent errors to avoid retrying (max 10)."""

    def record_error(
        self,
        connector_id: str,
        error_type: str,
        message: str,
    ) -> None:
        """Record an error to potentially avoid retrying."""
        self.recent_errors.append(
            {
                "connector_id": connector_id,
                "error_type": error_type,
                "message": message,
                "timestamp": datetime.now(tz=UTC).isoformat(),
            }
        )
        # Keep only last 10
        self.recent_errors = self.recent_errors[-10:]

    def has_recent_error(self, connector_id: str, error_type: str) -> bool:
        """Check if we recently had this type of error."""
        for err in self.recent_errors:
            if err["connector_id"] == connector_id and err["error_type"] == error_type:
                return True
        return False

    # =========================================================================
    # CONVERSATION METADATA
    # =========================================================================

    turn_count: int = 0
    """How many turns in this conversation."""

    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    """When this session started."""

    last_updated: datetime = field(default_factory=lambda: datetime.now(UTC))
    """When this session was last updated."""

    # =========================================================================
    # SERIALIZATION
    # =========================================================================

    def to_dict(self) -> dict[str, Any]:
        """Serialize for Redis storage."""
        return {
            "connectors": {cid: c.to_dict() for cid, c in self.connectors.items()},
            "primary_connector_id": self.primary_connector_id,
            "current_operation": self.current_operation,
            "operation_entities": self.operation_entities,
            "cached_tables": self.cached_tables,
            "recent_errors": self.recent_errors,
            "turn_count": self.turn_count,
            "created_at": self.created_at.isoformat(),
            "last_updated": self.last_updated.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrchestratorSessionState:
        """Deserialize from Redis storage."""
        state = cls()

        state.connectors = {
            cid: ConnectorMemory.from_dict(c) for cid, c in data.get("connectors", {}).items()
        }
        state.primary_connector_id = data.get("primary_connector_id")
        state.current_operation = data.get("current_operation")
        state.operation_entities = data.get("operation_entities", [])
        state.cached_tables = data.get("cached_tables", {})
        state.recent_errors = data.get("recent_errors", [])
        state.turn_count = data.get("turn_count", 0)

        if data.get("created_at"):
            state.created_at = datetime.fromisoformat(data["created_at"])
        if data.get("last_updated"):
            state.last_updated = datetime.fromisoformat(data["last_updated"])

        return state

    def get_context_summary(self) -> str:
        """Generate a natural language summary for LLM context."""
        parts = []

        if self.connectors:
            conn_names = [c.connector_name for c in self.connectors.values()]
            parts.append(f"Connectors used: {', '.join(conn_names)}")

        if self.current_operation:
            parts.append(f"Current focus: {self.current_operation}")

        if self.operation_entities:
            parts.append(f"Entities: {', '.join(self.operation_entities[:5])}")

        if self.cached_tables:
            parts.append(f"Cached data: {', '.join(self.cached_tables.keys())}")

        return " | ".join(parts) if parts else "New conversation"
