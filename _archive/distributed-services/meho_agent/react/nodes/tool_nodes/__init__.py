"""
Typed Tool Nodes for ReAct Graph (TASK-92, TASK-97)

Each tool has its own typed node class following pydantic-graph best practice.
Node fields are validated by Pydantic when the node is instantiated.

TASK-97: Generic nodes work for ALL connector types (REST, SOAP, VMware).
"""

# =============================================================================
# GENERIC TOOL NODES (TASK-97 - work for all connector types)
# =============================================================================
from meho_agent.react.nodes.tool_nodes.search_operations_node import SearchOperationsNode
from meho_agent.react.nodes.tool_nodes.call_operation_node import CallOperationNode
from meho_agent.react.nodes.tool_nodes.search_types_node import SearchTypesNode

# Knowledge tools
from meho_agent.react.nodes.tool_nodes.search_knowledge_node import SearchKnowledgeNode

# Connector management
from meho_agent.react.nodes.tool_nodes.list_connectors_node import ListConnectorsNode

# Data reduction
from meho_agent.react.nodes.tool_nodes.reduce_data_node import ReduceDataNode

__all__ = [
    # Generic tools (work for all connector types)
    "SearchOperationsNode",
    "CallOperationNode",
    "SearchTypesNode",
    # Knowledge
    "SearchKnowledgeNode",
    # Connector
    "ListConnectorsNode",
    # Data reduction
    "ReduceDataNode",
]

