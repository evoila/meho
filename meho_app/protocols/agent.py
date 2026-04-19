# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Protocol definitions for the Agent module.

These protocols define the interfaces for agent dependencies.
Note: IWorkflowRepository removed - ReAct agent operates without persistent plan storage.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

# Type alias for tool handler functions
ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


@runtime_checkable
class IAgentDependencies(Protocol):
    """
    Protocol for agent tool dependencies.

    This interface defines what tools and capabilities are available
    to the agent during execution.

    Implementations:
        - MEHODependencies
    """

    # Tool registration
    def register_tool(
        self, name: str, handler: ToolHandler, description: str | None = None
    ) -> None:
        """Register a tool handler by name."""
        ...

    def get_tool(self, name: str) -> ToolHandler | None:
        """Get a registered tool handler by name."""
        ...

    def list_tools(self) -> list[str]:
        """List all registered tool names."""
        ...

    # Core capabilities (these are the most commonly used tools)
    async def search_knowledge(
        self, query: str, top_k: int = 10, metadata_filters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Search the knowledge base."""
        ...

    async def search_endpoints(
        self, query: str, connector_id: str | None = None, top_k: int = 10
    ) -> dict[str, Any]:
        """Search available API endpoints."""
        ...

    async def call_endpoint(
        self, endpoint_id: str, parameters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call an API endpoint."""
        ...

    async def search_operations(
        self,
        query: str,
        connector_id: str | None = None,
        category: str | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        """Search connector operations (VMware, etc.)."""
        ...

    async def call_operation(
        self, operation_id: str, connector_id: str, parameters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute a connector operation."""
        ...

    async def list_connectors(self) -> dict[str, Any]:
        """List available connectors."""
        ...
