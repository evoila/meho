# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
MEHO ReAct Graph Dependencies (TASK-89)

Dependencies injected into all graph nodes.
WRAPS MEHODependencies - does NOT duplicate its logic!

The key insight: MEHODependencies already has all the business logic
for calling endpoints, searching, credential handling, etc.
We just pass it through and delegate to it.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent

from meho_app.core.otel import get_logger

logger = get_logger(__name__)

# Type alias for tool handlers
# Each handler takes (deps, **kwargs) and returns a result string
ToolHandler = Callable[["MEHOGraphDeps", dict[str, Any]], Awaitable[str]]


@dataclass
class MEHOGraphDeps:
    """
    Dependencies injected into ReAct graph nodes.

    IMPORTANT: This is a thin wrapper around MEHODependencies.
    All business logic for API calls, searches, credentials, etc.
    is handled by MEHODependencies - we just delegate to it.

    This approach:
    - Reuses existing, tested code
    - Avoids duplication
    - Ensures consistency with old agent behavior
    """

    # =========================================================================
    # CORE DEPENDENCY - THE SOURCE OF TRUTH
    # =========================================================================

    meho_deps: Any  # MEHODependencies
    """
    The main dependency container with all business logic.
    Contains: knowledge_store, connector_repo, endpoint_repo,
    user_cred_repo, http_client, session_state, etc.
    """

    llm_agent: Agent[None, str]
    """PydanticAI agent for LLM reasoning (configured with ReAct prompt)"""

    # Optional: approval store (added by TASK-76)
    approval_store: Any | None = None  # ApprovalStore
    """Repository for approval flow (TASK-76)"""

    # =========================================================================
    # TOOL HANDLERS
    # =========================================================================

    tools: dict[str, ToolHandler] = field(default_factory=dict)
    """
    Registered tool handlers.
    Each handler is a thin wrapper that delegates to MEHODependencies.
    """

    # =========================================================================
    # CONNECTOR CONTEXT (for specialist agent scoping)
    # =========================================================================

    connector_id: str | None = None
    """
    Active connector ID for specialist agent scoping.
    When set, knowledge search is scoped strictly to this connector.
    Orchestrator leaves this None for cross-connector search.
    """

    # =========================================================================
    # CONFIGURATION
    # =========================================================================

    max_steps: int = 100
    """Maximum number of Action→Observation cycles (depth limit)"""

    session_id: str | None = None
    """Chat session ID"""

    # =========================================================================
    # EVENT EMISSION (for streaming and observability)
    # =========================================================================

    emitter: Any | None = None  # EventEmitter
    """
    EventEmitter for typed events and transcript persistence (TASK-193).
    When set, emit_progress() delegates to the emitter for consistent
    event handling across old and new agent implementations.
    """

    # DEPRECATED: Keep for backward compatibility during migration
    progress_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
    """
    Callback for progress updates during graph execution.
    DEPRECATED: Use emitter instead. Will be removed in future.
    """

    # =========================================================================
    # CONVENIENCE ACCESSORS - delegate to meho_deps
    # =========================================================================

    @property
    def tenant_id(self) -> str:
        """Get tenant ID from MEHODependencies."""
        return self.meho_deps.user_context.tenant_id if self.meho_deps else "default"

    @property
    def user_id(self) -> str:
        """Get user ID from MEHODependencies."""
        return self.meho_deps.user_context.user_id if self.meho_deps else "anonymous"

    @property
    def knowledge_store(self) -> Any:
        """Get knowledge store from MEHODependencies."""
        return self.meho_deps.knowledge_store if self.meho_deps else None

    @property
    def http_client(self) -> Any:
        """Get HTTP client from MEHODependencies."""
        return self.meho_deps.http_client if self.meho_deps else None

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    async def emit_progress(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit a progress event - delegates to EventEmitter or legacy callback.

        Priority:
        1. If emitter is set, use emitter.emit() for typed event handling
        2. Fall back to progress_callback for backward compatibility
        """
        if self.emitter:
            try:
                await self.emitter.emit(event_type, data)
            except Exception as e:
                logger.warning(f"EventEmitter emit failed: {e}")
        elif self.progress_callback:
            try:
                await self.progress_callback(event_type, data)
            except Exception as e:
                logger.warning(f"Progress callback failed: {e}")

    def get_tool(self, name: str) -> ToolHandler | None:
        """Get a tool handler by name."""
        return self.tools.get(name)

    def list_tool_names(self) -> list[str]:
        """Get list of available tool names."""
        return list(self.tools.keys())

    def register_tool(self, name: str, handler: ToolHandler) -> None:
        """Register a tool handler."""
        self.tools[name] = handler
        logger.debug(f"Registered tool: {name}")
