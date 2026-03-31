# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Base node contract for agent graph nodes.

This module defines the abstract base class for graph nodes and the
NodeResult dataclass used to communicate transitions without circular imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter

# Type variable for state
TState = TypeVar("TState")


@dataclass
class NodeResult:
    """Result of node execution - communicates next node without imports.

    Using NodeResult instead of returning node instances directly avoids
    circular imports between node modules.

    Attributes:
        next_node: Name of next node to execute, or None to end flow.
        data: Optional data to pass to next node (e.g., tool name, args).

    Example:
        >>> return NodeResult(next_node="tool_dispatch", data={"tool": "search"})
        >>> return NodeResult(next_node=None)  # End flow
    """

    next_node: str | None
    data: dict[str, Any] = field(default_factory=dict)

    def is_terminal(self) -> bool:
        """Check if this result ends the flow.

        Returns:
            True if next_node is None (flow should end).
        """
        return self.next_node is None


@dataclass
class BaseNode(ABC, Generic[TState]):
    """Abstract base class for graph nodes.

    Nodes are the building blocks of agent flows. Each node:
    1. Receives state and dependencies
    2. Does work (LLM call, tool execution, etc.)
    3. Returns NodeResult indicating next node or end

    The flow controller (in agent.py) resolves node names to instances
    via the node registry, avoiding circular imports.

    Attributes:
        NODE_NAME: Unique identifier for this node type.

    Example:
        >>> @dataclass
        ... class ReasonNode(BaseNode[MyState]):
        ...     NODE_NAME = "reason"
        ...
        ...     async def run(self, state, deps, emitter):
        ...         # Do reasoning...
        ...         return NodeResult(next_node="tool_dispatch", data={...})
    """

    # Class attribute - MUST be defined by subclass
    NODE_NAME: ClassVar[str]

    @abstractmethod
    async def run(
        self,
        state: TState,
        deps: Any,
        emitter: EventEmitter,
    ) -> NodeResult:
        """Execute this node.

        Args:
            state: Current agent state (mutable).
            deps: Agent dependencies (services, config, etc.).
            emitter: SSE event emitter for streaming updates.

        Returns:
            NodeResult indicating next node to execute or None to end.

        Raises:
            NodeExecutionError: If node execution fails unrecoverably.
        """
        ...
