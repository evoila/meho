# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Agent Session State Management

Provides comprehensive state tracking for multi-turn conversations,
enabling intelligent context awareness and workflow tracking.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class OperationType(Enum):
    """Types of operations the agent is performing"""

    DISCOVERY = "discovery"  # Finding systems/endpoints
    DIAGNOSIS = "diagnosis"  # Investigating issues
    RETRIEVAL = "retrieval"  # Getting data
    COMPARISON = "comparison"  # Comparing across systems
    WORKFLOW = "workflow"  # Multi-step automation


@dataclass
class ConnectorContext:
    """
    Everything known about a connector in this conversation.

    Tracks the full connector UUID, discovered endpoints, recent data,
    and failed queries to enable intelligent multi-turn interactions.
    """

    connector_id: str  # Full UUID (never truncated!)
    connector_name: str  # Display name
    connector_type: str  # "kubernetes", "vmware", "rest", etc.
    last_used: datetime  # When last accessed

    # Discovered endpoints (method:path -> endpoint_id)
    known_endpoints: dict[str, str] = field(default_factory=dict)

    # Recently retrieved data (for follow-up questions)
    recent_data: dict[str, Any] = field(default_factory=dict)
    # Example: {"vms": {"data": [...], "retrieved_at": datetime(...)}}

    # Failed attempts (to avoid retrying)
    failed_queries: list[str] = field(default_factory=list)

    def add_endpoint(self, path: str, endpoint_id: str, method: str = "GET") -> None:
        """Remember an endpoint we discovered"""
        key = f"{method}:{path}"
        self.known_endpoints[key] = endpoint_id
        self.last_used = datetime.now(tz=UTC)
        logger.debug(f"Cached endpoint: {key} -> {endpoint_id}")

    def get_endpoint(self, path: str, method: str = "GET") -> str | None:
        """Retrieve cached endpoint ID"""
        key = f"{method}:{path}"
        return self.known_endpoints.get(key)

    def store_data(self, data_type: str, data: Any) -> None:
        """Cache API response data for follow-up questions"""
        self.recent_data[data_type] = {"data": data, "retrieved_at": datetime.now(tz=UTC)}
        self.last_used = datetime.now(tz=UTC)
        logger.debug(f"Cached {data_type} data ({len(str(data))} chars)")

    def get_data(self, data_type: str, max_age_seconds: int = 3600) -> Any | None:
        """Retrieve cached data if not too old"""
        if data_type not in self.recent_data:
            return None

        cached = self.recent_data[data_type]
        age = (datetime.now(tz=UTC) - cached["retrieved_at"]).total_seconds()

        if age > max_age_seconds:
            logger.debug(f"Cached {data_type} expired (age: {age}s)")
            return None

        return cached["data"]

    def record_failure(self, query: str) -> None:
        """Record a failed query to avoid retrying"""
        if query not in self.failed_queries:
            self.failed_queries.append(query)
            # Keep only last 20 failures
            self.failed_queries = self.failed_queries[-20:]
            logger.debug(f"Recorded failure: {query}")


@dataclass
class WorkflowStep:
    """Track multi-step operations"""

    step_id: str
    description: str
    status: str  # "pending", "in_progress", "completed", "failed"
    tool_name: str
    tool_args: dict[str, Any]
    result: Any | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class AgentSessionState:
    """
    Comprehensive conversation state for intelligent multi-turn interactions.

    This allows the agent to:
    - Remember what it has discovered (connectors, endpoints)
    - Reference previous API responses without re-calling
    - Switch between multiple systems intelligently
    - Avoid redundant searches and repeated failures
    - Build on previous context across multiple turns

    State is session-scoped and should be persisted between requests.
    """

    # ============================================================================
    # 1. CONNECTOR & ENDPOINT DISCOVERY
    # ============================================================================

    # ============================================================================
    # 0. SESSION MODE (Phase 65)
    # ============================================================================

    session_mode: str = "agent"
    """Session mode: 'agent' (full tool execution) or 'ask' (knowledge Q&A, read-only)"""

    # ============================================================================
    # 1. CONNECTOR & ENDPOINT DISCOVERY
    # ============================================================================

    connectors: dict[str, ConnectorContext] = field(default_factory=dict)
    """All connectors discovered/used in this conversation"""

    primary_connector_id: str | None = None
    """The main connector for current context (can change!)"""

    def get_or_create_connector(
        self, connector_id: str, connector_name: str, connector_type: str = "unknown"
    ) -> ConnectorContext:
        """Get existing or create new connector context"""
        if connector_id not in self.connectors:
            self.connectors[connector_id] = ConnectorContext(
                connector_id=connector_id,
                connector_name=connector_name,
                connector_type=connector_type,
                last_used=datetime.now(tz=UTC),
            )
            logger.info(f"📝 New connector context: {connector_name} ({connector_id[:8]}...)")
        return self.connectors[connector_id]

    def get_active_connector(self) -> ConnectorContext | None:
        """
        Get the currently active connector.

        Returns primary_connector if set, otherwise the most recently used.
        """
        if self.primary_connector_id and self.primary_connector_id in self.connectors:
            return self.connectors[self.primary_connector_id]

        # Fallback: most recently used connector
        if self.connectors:
            return max(self.connectors.values(), key=lambda c: c.last_used)

        return None

    def switch_connector(self, connector_id: str) -> None:
        """Explicitly switch to a different connector"""
        if connector_id in self.connectors:
            self.primary_connector_id = connector_id
            logger.info(f"🔄 Switched to connector: {connector_id[:8]}...")
        else:
            logger.warning(f"⚠️ Attempted to switch to unknown connector: {connector_id}")

    # ============================================================================
    # 2. OPERATION CONTEXT
    # ============================================================================

    operation_type: OperationType | None = None
    """What is the user trying to accomplish?"""

    operation_goal: str | None = None
    """Natural language description of current goal"""

    workflow_steps: list[WorkflowStep] = field(default_factory=list)
    """For multi-step operations, track progress"""

    def set_operation(self, op_type: OperationType, goal: str) -> None:
        """Set the current operation context"""
        self.operation_type = op_type
        self.operation_goal = goal
        logger.info(f"🎯 Operation: {op_type.value} - {goal}")

    def add_workflow_step(self, step: WorkflowStep) -> None:
        """Track a step in a multi-step workflow"""
        self.workflow_steps.append(step)
        logger.info(f"📋 Workflow step {len(self.workflow_steps)}: {step.description}")

    def get_workflow_progress(self) -> dict[str, int]:
        """Get workflow progress summary"""
        if not self.workflow_steps:
            return {"total": 0, "completed": 0, "failed": 0, "in_progress": 0}

        return {
            "total": len(self.workflow_steps),
            "completed": sum(1 for s in self.workflow_steps if s.status == "completed"),
            "failed": sum(1 for s in self.workflow_steps if s.status == "failed"),
            "in_progress": sum(1 for s in self.workflow_steps if s.status == "in_progress"),
        }

    # ============================================================================
    # 3. USER PREFERENCES & PATTERNS
    # ============================================================================

    output_preferences: dict[str, Any] = field(default_factory=dict)
    """User's preferred output format, filters, etc."""
    # Example: {"format": "table", "show_details": True, "max_results": 50}

    repeated_queries: list[str] = field(default_factory=list)
    """Track repeated queries (might indicate confusion)"""

    def learn_preference(self, key: str, value: Any) -> None:
        """Learn from user interactions"""
        self.output_preferences[key] = value
        logger.debug(f"📚 Learned preference: {key} = {value}")

    def get_preference(self, key: str, default: Any = None) -> Any:
        """Get user preference"""
        return self.output_preferences.get(key, default)

    # ============================================================================
    # 4. KNOWLEDGE BASE CONTEXT
    # ============================================================================

    referenced_docs: set[str] = field(default_factory=set)
    """Documentation URIs that have been referenced"""

    learned_facts: dict[str, str] = field(default_factory=dict)
    """Facts learned from documentation"""
    # Example: {"vm_reset_endpoint": "/api/vcenter/vm/{vm}/power/reset"}

    def add_documentation_reference(self, doc_uri: str, fact: str | None = None) -> None:
        """Remember that we've seen this documentation"""
        self.referenced_docs.add(doc_uri)
        if fact:
            self.learned_facts[doc_uri] = fact
            logger.debug(f"📖 Learned fact from {doc_uri}")

    # ============================================================================
    # 5. ERROR CONTEXT & LEARNING
    # ============================================================================

    recent_errors: list[dict[str, Any]] = field(default_factory=list)
    """Recent errors encountered (to avoid retrying)"""

    def record_error(self, tool_name: str, error_msg: str, context: dict[str, Any]) -> None:
        """Record an error to avoid repeating it"""
        self.recent_errors.append(
            {
                "tool": tool_name,
                "error": error_msg,
                "context": context,
                "timestamp": datetime.now(tz=UTC),
            }
        )
        # Keep only last 10 errors
        self.recent_errors = self.recent_errors[-10:]
        logger.warning(f"❌ Recorded error: {tool_name} - {error_msg}")

    def has_similar_error(self, tool_name: str, context: dict[str, Any]) -> bool:
        """Check if we've already tried something similar that failed"""
        for error in self.recent_errors:
            if error["tool"] == tool_name:  # noqa: SIM102 -- readability preferred over collapse
                # Simple similarity check based on connector
                if error["context"].get("connector_id") == context.get("connector_id"):
                    return True
        return False

    # ============================================================================
    # 6. CROSS-SYSTEM CORRELATION
    # ============================================================================

    correlations: dict[str, list[str]] = field(default_factory=dict)
    """Relationships between entities across systems"""
    # Example: {"vm-107": ["k8s-node-1", "vcenter-host-esxi01"]}

    def add_correlation(self, entity_id: str, related_entity_id: str) -> None:
        """Track that two entities are related"""
        if entity_id not in self.correlations:
            self.correlations[entity_id] = []
        if related_entity_id not in self.correlations[entity_id]:
            self.correlations[entity_id].append(related_entity_id)
            logger.debug(f"🔗 Correlated: {entity_id} ↔ {related_entity_id}")

    def get_related_entities(self, entity_id: str) -> list[str]:
        """Get all entities related to this one"""
        return self.correlations.get(entity_id, [])

    # ============================================================================
    # 7. DATA REDUCTION TRACKING
    # ============================================================================

    data_was_reduced: bool = False
    """Flag indicating if any data reduction was applied in this session"""

    reduction_stats: dict[str, Any] = field(default_factory=dict)
    """Statistics about data reduction (original count, reduced count, etc.)"""

    def mark_data_reduced(
        self, original_count: int, reduced_count: int, entity_type: str = "records"
    ) -> None:
        """Record that data reduction was applied"""
        self.data_was_reduced = True
        self.reduction_stats = {
            "original_count": original_count,
            "reduced_count": reduced_count,
            "entity_type": entity_type,
            "reduction_pct": round((1 - reduced_count / original_count) * 100, 1)
            if original_count > 0
            else 0,
        }
        logger.info(
            f"📊 Data reduced: {original_count} → {reduced_count} ({self.reduction_stats['reduction_pct']}% reduction)"
        )

    # ============================================================================
    # 8. PERFORMANCE INSIGHTS
    # ============================================================================

    slow_endpoints: set[str] = field(default_factory=set)
    """Endpoints that took > 5s to respond"""

    rate_limited_endpoints: dict[str, datetime] = field(default_factory=dict)
    """Endpoints that hit rate limits (endpoint_id -> when)"""

    def mark_slow_endpoint(self, endpoint_id: str) -> None:
        """Remember that this endpoint is slow"""
        self.slow_endpoints.add(endpoint_id)
        logger.warning(f"🐌 Marked slow endpoint: {endpoint_id}")

    def mark_rate_limited(self, endpoint_id: str) -> None:
        """Remember that this endpoint is rate-limited"""
        self.rate_limited_endpoints[endpoint_id] = datetime.now(tz=UTC)
        logger.warning(f"🚦 Marked rate-limited endpoint: {endpoint_id}")

    def is_rate_limited(self, endpoint_id: str, cooldown_seconds: int = 60) -> bool:
        """Check if endpoint is currently rate-limited"""
        if endpoint_id not in self.rate_limited_endpoints:
            return False

        limited_at = self.rate_limited_endpoints[endpoint_id]
        age = (datetime.now(tz=UTC) - limited_at).total_seconds()
        return age < cooldown_seconds

    # ============================================================================
    # UTILITY METHODS
    # ============================================================================

    def get_context_summary(self) -> str:
        """Generate a natural language summary of current state"""
        summary_parts = []

        # Connectors - handle None connector_name gracefully
        if self.connectors:
            conn_names = [
                c.connector_name or c.connector_id[:8] + "..." for c in self.connectors.values()
            ]
            summary_parts.append(f"Connectors: {', '.join(conn_names)}")

        # Operation
        if self.operation_goal:
            summary_parts.append(f"Goal: {self.operation_goal}")

        # Workflow
        if self.workflow_steps:
            progress = self.get_workflow_progress()
            summary_parts.append(f"Workflow: {progress['completed']}/{progress['total']} steps")

        return " | ".join(summary_parts) if summary_parts else "No active context"

    def clear_stale_data(self, max_age_seconds: int = 3600) -> None:
        """Clear data older than max_age (default 1 hour)"""
        now = datetime.now(tz=UTC)

        # Remove old connector data
        for connector in self.connectors.values():
            stale_keys = []
            for data_type, cached in connector.recent_data.items():
                age = (now - cached["retrieved_at"]).total_seconds()
                if age > max_age_seconds:
                    stale_keys.append(data_type)

            for key in stale_keys:
                del connector.recent_data[key]

        # Clear old errors
        self.recent_errors = [
            e
            for e in self.recent_errors
            if (now - e["timestamp"]).total_seconds() < max_age_seconds
        ]

        logger.info(f"🧹 Cleared stale data (older than {max_age_seconds}s)")

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize state to dictionary for persistence.

        Note: datetime objects are converted to ISO strings.
        """
        return {
            "session_mode": self.session_mode,
            "connectors": {
                cid: {
                    "connector_id": c.connector_id,
                    "connector_name": c.connector_name,
                    "connector_type": c.connector_type,
                    "last_used": c.last_used.isoformat(),
                    "known_endpoints": c.known_endpoints,
                    "failed_queries": c.failed_queries,
                }
                for cid, c in self.connectors.items()
            },
            "primary_connector_id": self.primary_connector_id,
            "operation_type": self.operation_type.value if self.operation_type else None,
            "operation_goal": self.operation_goal,
            "workflow_progress": self.get_workflow_progress(),
            # Data reduction tracking
            "data_was_reduced": self.data_was_reduced,
            "reduction_stats": self.reduction_stats,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentSessionState":
        """
        Deserialize state from dictionary.
        """
        state = cls()

        # Restore session mode (Phase 65)
        state.session_mode = data.get("session_mode", "agent")

        # Restore basic fields
        state.primary_connector_id = data.get("primary_connector_id")

        if data.get("operation_type"):
            state.operation_type = OperationType(data["operation_type"])
        state.operation_goal = data.get("operation_goal")

        # Restore connectors - handle None connector_name gracefully
        for cid, conn_data in data.get("connectors", {}).items():
            state.connectors[cid] = ConnectorContext(
                connector_id=conn_data["connector_id"],
                connector_name=conn_data.get("connector_name") or f"Connector {cid[:8]}...",
                connector_type=conn_data.get("connector_type")
                or conn_data.get("system_type")
                or "unknown",
                last_used=datetime.fromisoformat(conn_data["last_used"]),
                known_endpoints=conn_data.get("known_endpoints", {}),
                failed_queries=conn_data.get("failed_queries", []),
            )

        # Restore data reduction tracking
        state.data_was_reduced = data.get("data_was_reduced", False)
        state.reduction_stats = data.get("reduction_stats", {})

        logger.info(f"📦 Restored state from persisted data (reduced={state.data_was_reduced})")

        return state
