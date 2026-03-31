"""
Agent Session State Management

Provides comprehensive state tracking for multi-turn conversations,
enabling intelligent context awareness, entity resolution, and workflow tracking.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set
from datetime import datetime
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class OperationType(Enum):
    """Types of operations the agent is performing"""
    DISCOVERY = "discovery"          # Finding systems/endpoints
    DIAGNOSIS = "diagnosis"          # Investigating issues
    RETRIEVAL = "retrieval"          # Getting data
    COMPARISON = "comparison"        # Comparing across systems
    WORKFLOW = "workflow"            # Multi-step automation


@dataclass
class ConnectorContext:
    """
    Everything known about a connector in this conversation.
    
    Tracks the full connector UUID, discovered endpoints, recent data,
    and failed queries to enable intelligent multi-turn interactions.
    """
    connector_id: str                          # Full UUID (never truncated!)
    connector_name: str                        # Display name
    system_type: str                           # "vcenter", "kubernetes", etc.
    last_used: datetime                        # When last accessed
    
    # Discovered endpoints (method:path -> endpoint_id)
    known_endpoints: Dict[str, str] = field(default_factory=dict)
    
    # Recently retrieved data (for follow-up questions)
    recent_data: Dict[str, Any] = field(default_factory=dict)
    # Example: {"vms": {"data": [...], "retrieved_at": datetime(...)}}
    
    # Failed attempts (to avoid retrying)
    failed_queries: List[str] = field(default_factory=list)
    
    def add_endpoint(self, path: str, endpoint_id: str, method: str = "GET") -> None:
        """Remember an endpoint we discovered"""
        key = f"{method}:{path}"
        self.known_endpoints[key] = endpoint_id
        self.last_used = datetime.now()
        logger.debug(f"Cached endpoint: {key} -> {endpoint_id}")
    
    def get_endpoint(self, path: str, method: str = "GET") -> Optional[str]:
        """Retrieve cached endpoint ID"""
        key = f"{method}:{path}"
        return self.known_endpoints.get(key)
    
    def store_data(self, data_type: str, data: Any) -> None:
        """Cache API response data for follow-up questions"""
        self.recent_data[data_type] = {
            "data": data,
            "retrieved_at": datetime.now()
        }
        self.last_used = datetime.now()
        logger.debug(f"Cached {data_type} data ({len(str(data))} chars)")
    
    def get_data(self, data_type: str, max_age_seconds: int = 3600) -> Optional[Any]:
        """Retrieve cached data if not too old"""
        if data_type not in self.recent_data:
            return None
        
        cached = self.recent_data[data_type]
        age = (datetime.now() - cached["retrieved_at"]).total_seconds()
        
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
class ExtractedEntity:
    """
    Entities extracted from API responses (IDs, names, etc.)
    
    Enables the agent to reference things by name instead of UUIDs.
    Example: "Get IP of vidm-primary" instead of "Get IP of vm-107"
    """
    entity_type: str        # "vm", "pod", "user", "cluster"
    entity_id: str          # Actual ID from API
    entity_name: str        # Human-readable name
    source_connector: str   # Which connector it came from
    attributes: Dict[str, Any] = field(default_factory=dict)
    # Example: {"status": "running", "ip": "10.0.1.5", "power_state": "POWERED_ON"}
    
    def matches(self, identifier: str) -> bool:
        """Check if this entity matches an identifier (ID or name)"""
        identifier_lower = identifier.lower()
        return (
            self.entity_id.lower() == identifier_lower or
            self.entity_name.lower() == identifier_lower
        )


@dataclass
class WorkflowStep:
    """Track multi-step operations"""
    step_id: str
    description: str
    status: str  # "pending", "in_progress", "completed", "failed"
    tool_name: str
    tool_args: Dict[str, Any]
    result: Optional[Any] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass
class AgentSessionState:
    """
    Comprehensive conversation state for intelligent multi-turn interactions.
    
    This allows the agent to:
    - Remember what it has discovered (connectors, endpoints, entities)
    - Reference previous API responses without re-calling
    - Switch between multiple systems intelligently
    - Avoid redundant searches and repeated failures
    - Build on previous context across multiple turns
    
    State is session-scoped and should be persisted between requests.
    """
    
    # ============================================================================
    # 1. CONNECTOR & ENDPOINT DISCOVERY
    # ============================================================================
    
    connectors: Dict[str, ConnectorContext] = field(default_factory=dict)
    """All connectors discovered/used in this conversation"""
    
    primary_connector_id: Optional[str] = None
    """The main connector for current context (can change!)"""
    
    def get_or_create_connector(
        self, 
        connector_id: str, 
        connector_name: str, 
        system_type: str = "unknown"
    ) -> ConnectorContext:
        """Get existing or create new connector context"""
        if connector_id not in self.connectors:
            self.connectors[connector_id] = ConnectorContext(
                connector_id=connector_id,
                connector_name=connector_name,
                system_type=system_type,
                last_used=datetime.now()
            )
            logger.info(f"📝 New connector context: {connector_name} ({connector_id[:8]}...)")
        return self.connectors[connector_id]
    
    def get_active_connector(self) -> Optional[ConnectorContext]:
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
    # 2. EXTRACTED ENTITIES (IDs from API responses)
    # ============================================================================
    
    entities: Dict[str, ExtractedEntity] = field(default_factory=dict)
    """Entities extracted from API responses (VMs, pods, users, etc.)"""
    
    def add_entities_from_response(
        self,
        entity_type: str,
        items: List[Dict],
        connector_id: str,
        id_field: Optional[str] = None,
        name_field: str = "name"
    ) -> None:
        """
        Automatically extract entities from API response.
        
        Example:
            # After GET /api/vcenter/vm returns 24 VMs
            state.add_entities_from_response(
                entity_type="vm",
                items=[{"vm": "vm-107", "name": "vidm-primary", ...}, ...],
                connector_id="vcenter-uuid",
                id_field="vm",  # Optional - will auto-detect if not provided
                name_field="name"
            )
            
            # Now agent can reference "vm-107" or "vidm-primary" in future queries
        """
        count = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            
            # Smart ID field detection - try multiple common patterns
            entity_id = None
            if id_field:
                entity_id = item.get(id_field)
            
            if not entity_id:
                # Try common ID field patterns in priority order
                for possible_id_field in [
                    entity_type,  # e.g., "vm" for VM entities
                    f"{entity_type}_id",  # e.g., "vm_id"
                    "id",  # Standard
                    "uid",  # Kubernetes style
                    "uuid",  # UUID style
                    "identifier",  # Generic
                ]:
                    entity_id = item.get(possible_id_field)
                    if entity_id:
                        break
            
            entity_name = item.get(name_field, entity_id)
            
            if entity_id:
                key = f"{entity_type}:{entity_id}"
                self.entities[key] = ExtractedEntity(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    entity_name=entity_name or entity_id,
                    source_connector=connector_id,
                    attributes=item
                )
                count += 1
        
        if count > 0:
            logger.info(f"📦 Extracted {count} {entity_type}(s) from response")
    
    def find_entity(
        self, 
        identifier: str, 
        entity_type: Optional[str] = None
    ) -> Optional[ExtractedEntity]:
        """
        Find entity by ID or name.
        
        Example:
            entity = state.find_entity("vidm-primary")  # Finds VM by name
            entity = state.find_entity("vm-107")        # Finds VM by ID
            entity = state.find_entity("vm-107", "vm")  # Finds VM by ID with type hint
        """
        # Try exact match by key first (fast path)
        if entity_type:
            key = f"{entity_type}:{identifier}"
            if key in self.entities:
                return self.entities[key]
        
        # Search by ID or name (slower but more flexible)
        for entity in self.entities.values():
            if entity_type and entity.entity_type != entity_type:
                continue
            if entity.matches(identifier):
                return entity
        
        logger.debug(f"Entity not found: {identifier} (type: {entity_type or 'any'})")
        return None
    
    def get_entities_by_type(self, entity_type: str) -> List[ExtractedEntity]:
        """Get all entities of a specific type"""
        return [e for e in self.entities.values() if e.entity_type == entity_type]
    
    def get_entity_count(self) -> Dict[str, int]:
        """Get count of entities by type"""
        counts: Dict[str, int] = {}
        for entity in self.entities.values():
            counts[entity.entity_type] = counts.get(entity.entity_type, 0) + 1
        return counts
    
    # ============================================================================
    # 3. OPERATION CONTEXT
    # ============================================================================
    
    operation_type: Optional[OperationType] = None
    """What is the user trying to accomplish?"""
    
    operation_goal: Optional[str] = None
    """Natural language description of current goal"""
    
    workflow_steps: List[WorkflowStep] = field(default_factory=list)
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
    
    def get_workflow_progress(self) -> Dict[str, int]:
        """Get workflow progress summary"""
        if not self.workflow_steps:
            return {"total": 0, "completed": 0, "failed": 0, "in_progress": 0}
        
        return {
            "total": len(self.workflow_steps),
            "completed": sum(1 for s in self.workflow_steps if s.status == "completed"),
            "failed": sum(1 for s in self.workflow_steps if s.status == "failed"),
            "in_progress": sum(1 for s in self.workflow_steps if s.status == "in_progress")
        }
    
    # ============================================================================
    # 4. USER PREFERENCES & PATTERNS
    # ============================================================================
    
    output_preferences: Dict[str, Any] = field(default_factory=dict)
    """User's preferred output format, filters, etc."""
    # Example: {"format": "table", "show_details": True, "max_results": 50}
    
    repeated_queries: List[str] = field(default_factory=list)
    """Track repeated queries (might indicate confusion)"""
    
    def learn_preference(self, key: str, value: Any) -> None:
        """Learn from user interactions"""
        self.output_preferences[key] = value
        logger.debug(f"📚 Learned preference: {key} = {value}")
    
    def get_preference(self, key: str, default: Any = None) -> Any:
        """Get user preference"""
        return self.output_preferences.get(key, default)
    
    # ============================================================================
    # 5. KNOWLEDGE BASE CONTEXT
    # ============================================================================
    
    referenced_docs: Set[str] = field(default_factory=set)
    """Documentation URIs that have been referenced"""
    
    learned_facts: Dict[str, str] = field(default_factory=dict)
    """Facts learned from documentation"""
    # Example: {"vm_reset_endpoint": "/api/vcenter/vm/{vm}/power/reset"}
    
    def add_documentation_reference(self, doc_uri: str, fact: Optional[str] = None) -> None:
        """Remember that we've seen this documentation"""
        self.referenced_docs.add(doc_uri)
        if fact:
            self.learned_facts[doc_uri] = fact
            logger.debug(f"📖 Learned fact from {doc_uri}")
    
    # ============================================================================
    # 6. ERROR CONTEXT & LEARNING
    # ============================================================================
    
    recent_errors: List[Dict[str, Any]] = field(default_factory=list)
    """Recent errors encountered (to avoid retrying)"""
    
    def record_error(self, tool_name: str, error_msg: str, context: Dict[str, Any]) -> None:
        """Record an error to avoid repeating it"""
        self.recent_errors.append({
            "tool": tool_name,
            "error": error_msg,
            "context": context,
            "timestamp": datetime.now()
        })
        # Keep only last 10 errors
        self.recent_errors = self.recent_errors[-10:]
        logger.warning(f"❌ Recorded error: {tool_name} - {error_msg}")
    
    def has_similar_error(self, tool_name: str, context: Dict[str, Any]) -> bool:
        """Check if we've already tried something similar that failed"""
        for error in self.recent_errors:
            if error["tool"] == tool_name:
                # Simple similarity check based on connector
                if error["context"].get("connector_id") == context.get("connector_id"):
                    return True
        return False
    
    # ============================================================================
    # 7. CROSS-SYSTEM CORRELATION
    # ============================================================================
    
    correlations: Dict[str, List[str]] = field(default_factory=dict)
    """Relationships between entities across systems"""
    # Example: {"vm-107": ["k8s-node-1", "vcenter-host-esxi01"]}
    
    def add_correlation(self, entity_id: str, related_entity_id: str) -> None:
        """Track that two entities are related"""
        if entity_id not in self.correlations:
            self.correlations[entity_id] = []
        if related_entity_id not in self.correlations[entity_id]:
            self.correlations[entity_id].append(related_entity_id)
            logger.debug(f"🔗 Correlated: {entity_id} ↔ {related_entity_id}")
    
    def get_related_entities(self, entity_id: str) -> List[str]:
        """Get all entities related to this one"""
        return self.correlations.get(entity_id, [])
    
    # ============================================================================
    # 8. DATA REDUCTION TRACKING
    # ============================================================================
    
    data_was_reduced: bool = False
    """Flag indicating if any data reduction was applied in this session"""
    
    reduction_stats: Dict[str, Any] = field(default_factory=dict)
    """Statistics about data reduction (original count, reduced count, etc.)"""
    
    def mark_data_reduced(self, original_count: int, reduced_count: int, entity_type: str = "records") -> None:
        """Record that data reduction was applied"""
        self.data_was_reduced = True
        self.reduction_stats = {
            "original_count": original_count,
            "reduced_count": reduced_count,
            "entity_type": entity_type,
            "reduction_pct": round((1 - reduced_count / original_count) * 100, 1) if original_count > 0 else 0
        }
        logger.info(f"📊 Data reduced: {original_count} → {reduced_count} ({self.reduction_stats['reduction_pct']}% reduction)")
    
    # ============================================================================
    # 9. PERFORMANCE INSIGHTS
    # ============================================================================
    
    slow_endpoints: Set[str] = field(default_factory=set)
    """Endpoints that took > 5s to respond"""
    
    rate_limited_endpoints: Dict[str, datetime] = field(default_factory=dict)
    """Endpoints that hit rate limits (endpoint_id -> when)"""
    
    def mark_slow_endpoint(self, endpoint_id: str) -> None:
        """Remember that this endpoint is slow"""
        self.slow_endpoints.add(endpoint_id)
        logger.warning(f"🐌 Marked slow endpoint: {endpoint_id}")
    
    def mark_rate_limited(self, endpoint_id: str) -> None:
        """Remember that this endpoint is rate-limited"""
        self.rate_limited_endpoints[endpoint_id] = datetime.now()
        logger.warning(f"🚦 Marked rate-limited endpoint: {endpoint_id}")
    
    def is_rate_limited(self, endpoint_id: str, cooldown_seconds: int = 60) -> bool:
        """Check if endpoint is currently rate-limited"""
        if endpoint_id not in self.rate_limited_endpoints:
            return False
        
        limited_at = self.rate_limited_endpoints[endpoint_id]
        age = (datetime.now() - limited_at).total_seconds()
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
                c.connector_name or c.connector_id[:8] + "..."
                for c in self.connectors.values()
            ]
            summary_parts.append(f"Connectors: {', '.join(conn_names)}")
        
        # Operation
        if self.operation_goal:
            summary_parts.append(f"Goal: {self.operation_goal}")
        
        # Entities
        entity_counts = self.get_entity_count()
        if entity_counts:
            counts_str = ", ".join([f"{count} {etype}(s)" for etype, count in entity_counts.items()])
            summary_parts.append(f"Entities: {counts_str}")
        
        # Workflow
        if self.workflow_steps:
            progress = self.get_workflow_progress()
            summary_parts.append(f"Workflow: {progress['completed']}/{progress['total']} steps")
        
        return " | ".join(summary_parts) if summary_parts else "No active context"
    
    def clear_stale_data(self, max_age_seconds: int = 3600) -> None:
        """Clear data older than max_age (default 1 hour)"""
        now = datetime.now()
        
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
            e for e in self.recent_errors
            if (now - e["timestamp"]).total_seconds() < max_age_seconds
        ]
        
        logger.info(f"🧹 Cleared stale data (older than {max_age_seconds}s)")
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize state to dictionary for persistence.
        
        Note: datetime objects are converted to ISO strings.
        """
        return {
            "connectors": {
                cid: {
                    "connector_id": c.connector_id,
                    "connector_name": c.connector_name,
                    "system_type": c.system_type,
                    "last_used": c.last_used.isoformat(),
                    "known_endpoints": c.known_endpoints,
                    "failed_queries": c.failed_queries
                }
                for cid, c in self.connectors.items()
            },
            "primary_connector_id": self.primary_connector_id,
            # 🎯 CRITICAL FIX: Serialize actual entities, not just count!
            "entities": {
                key: {
                    "entity_type": e.entity_type,
                    "entity_id": e.entity_id,
                    "entity_name": e.entity_name,
                    "source_connector": e.source_connector,
                    "attributes": e.attributes
                }
                for key, e in self.entities.items()
            },
            "operation_type": self.operation_type.value if self.operation_type else None,
            "operation_goal": self.operation_goal,
            "workflow_progress": self.get_workflow_progress(),
            # Data reduction tracking
            "data_was_reduced": self.data_was_reduced,
            "reduction_stats": self.reduction_stats
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentSessionState":
        """
        Deserialize state from dictionary.
        """
        state = cls()
        
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
                system_type=conn_data.get("system_type") or "unknown",
                last_used=datetime.fromisoformat(conn_data["last_used"]),
                known_endpoints=conn_data.get("known_endpoints", {}),
                failed_queries=conn_data.get("failed_queries", [])
            )
        
        # 🎯 CRITICAL FIX: Restore entities!
        for key, entity_data in data.get("entities", {}).items():
            state.entities[key] = ExtractedEntity(
                entity_type=entity_data["entity_type"],
                entity_id=entity_data["entity_id"],
                entity_name=entity_data.get("entity_name", entity_data["entity_id"]),
                source_connector=entity_data.get("source_connector", "unknown"),
                attributes=entity_data.get("attributes", {})
            )
        
        # Restore data reduction tracking
        state.data_was_reduced = data.get("data_was_reduced", False)
        state.reduction_stats = data.get("reduction_stats", {})
        
        logger.info(f"📦 Restored {len(state.entities)} entities from persisted state (reduced={state.data_was_reduced})")
        
        return state

