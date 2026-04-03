# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Node registry for React Agent.

All nodes are registered here. The flow controller uses this registry
to resolve node names to classes, avoiding circular imports between nodes.

Exports:
    ReasonNode: LLM reasoning node
    ToolDispatchNode: Routes to tool execution
    ApprovalCheckNode: Handles approval flow
    TopologyLookupNode: Initial topology context injection
    NODE_REGISTRY: Maps node names to classes
    get_node_class: Get node class by name
    create_node: Create node instance by name
"""

from __future__ import annotations

from meho_app.modules.agents.base.node import BaseNode

from .approval_check import ApprovalCheckNode
from .reason import ReasonNode
from .tool_dispatch import ToolDispatchNode
from .topology_lookup import TopologyLookupNode

# Registry maps node names to classes
NODE_REGISTRY: dict[str, type[BaseNode]] = {
    "reason": ReasonNode,
    "tool_dispatch": ToolDispatchNode,
    "approval_check": ApprovalCheckNode,
    "topology_lookup": TopologyLookupNode,
}


def get_node_class(name: str) -> type[BaseNode]:
    """Get node class by name.

    Args:
        name: Node name as defined in NODE_NAME.

    Returns:
        Node class (not instance).

    Raises:
        KeyError: If node name not found in registry.
    """
    return NODE_REGISTRY[name]


def create_node(name: str) -> BaseNode:
    """Create node instance by name.

    Args:
        name: Node name as defined in NODE_NAME.

    Returns:
        Node instance ready to execute.

    Raises:
        KeyError: If node name not found in registry.
    """
    node_class = get_node_class(name)
    return node_class()


__all__ = [
    "NODE_REGISTRY",
    "ApprovalCheckNode",
    "ReasonNode",
    "ToolDispatchNode",
    "TopologyLookupNode",
    "create_node",
    "get_node_class",
]
