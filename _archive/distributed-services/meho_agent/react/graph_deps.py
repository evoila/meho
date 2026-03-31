"""
MEHO ReAct Graph Dependencies (TASK-89)

Dependencies injected into all graph nodes.
WRAPS MEHODependencies - does NOT duplicate its logic!

The key insight: MEHODependencies already has all the business logic
for calling endpoints, searching, credential handling, etc.
We just pass it through and delegate to it.
"""

from dataclasses import dataclass, field
from typing import Dict, Callable, Awaitable, Any, Optional, List
import logging

from pydantic_ai import Agent
from pydantic_ai.models import Model

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meho_agent.dependencies import MEHODependencies
    from meho_agent.approval.repository import ApprovalStore

logger = logging.getLogger(__name__)

# Type alias for tool handlers
# Each handler takes (deps, **kwargs) and returns a result string
ToolHandler = Callable[["MEHOGraphDeps", Dict[str, Any]], Awaitable[str]]


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
    approval_store: Optional[Any] = None  # ApprovalStore
    """Repository for approval flow (TASK-76)"""
    
    # =========================================================================
    # TOOL HANDLERS
    # =========================================================================
    
    tools: Dict[str, ToolHandler] = field(default_factory=dict)
    """
    Registered tool handlers.
    Each handler is a thin wrapper that delegates to MEHODependencies.
    """
    
    # =========================================================================
    # CONFIGURATION
    # =========================================================================
    
    max_steps: int = 100
    """Maximum number of Action→Observation cycles (depth limit)"""
    
    session_id: Optional[str] = None
    """Chat session ID"""
    
    # =========================================================================
    # CALLBACKS (for streaming)
    # =========================================================================
    
    progress_callback: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None
    """Callback for progress updates during graph execution."""
    
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
    
    async def emit_progress(self, event_type: str, data: Dict[str, Any]) -> None:
        """Emit a progress event to the callback."""
        if self.progress_callback:
            try:
                await self.progress_callback(event_type, data)
            except Exception as e:
                logger.warning(f"Progress callback failed: {e}")
    
    def get_tool(self, name: str) -> Optional[ToolHandler]:
        """Get a tool handler by name."""
        return self.tools.get(name)
    
    def list_tool_names(self) -> List[str]:
        """Get list of available tool names."""
        return list(self.tools.keys())
    
    def register_tool(self, name: str, handler: ToolHandler) -> None:
        """Register a tool handler."""
        self.tools[name] = handler
        logger.debug(f"Registered tool: {name}")

