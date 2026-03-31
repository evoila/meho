# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Specialist Agent Nodes - One file per workflow step.

Each node handles a single step in the deterministic workflow:
1. search_intent - LLM decides what to search for (with skill context)
2. search_operations - Code executes the search
3. select_operation - LLM picks which operation to call (with skill context)
4. call_operation - Code executes the operation
5. reduce_data - Code fetches cached data if needed

Usage:
    from meho_app.modules.agents.specialist_agent.nodes import (
        SearchIntentNode,
        SearchOperationsNode,
        SelectOperationNode,
        CallOperationNode,
        ReduceDataNode,
    )
"""

from meho_app.modules.agents.specialist_agent.nodes.call_operation import (
    CallOperationNode,
)
from meho_app.modules.agents.specialist_agent.nodes.reduce_data import (
    ReduceDataNode,
)
from meho_app.modules.agents.specialist_agent.nodes.search_intent import (
    SearchIntentNode,
)
from meho_app.modules.agents.specialist_agent.nodes.search_operations import (
    SearchOperationsNode,
)
from meho_app.modules.agents.specialist_agent.nodes.select_operation import (
    SelectOperationNode,
)

__all__ = [
    "CallOperationNode",
    "ReduceDataNode",
    "SearchIntentNode",
    "SearchOperationsNode",
    "SelectOperationNode",
]
