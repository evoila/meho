# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Base contracts for MEHO agents.

This module provides the abstract base classes that define the contract
for all agents, tools, and nodes in the MEHO multi-agent architecture.

Exports:
    BaseAgent: Abstract base class for all agents
    BaseTool: Abstract base class for agent tools
    BaseNode: Abstract base class for graph nodes
    NodeResult: Result type for node execution
"""

from __future__ import annotations

from meho_app.modules.agents.base.agent import BaseAgent
from meho_app.modules.agents.base.inference import infer, one_shot, quick_llm
from meho_app.modules.agents.base.node import BaseNode, NodeResult
from meho_app.modules.agents.base.reduce_data import BaseReduceDataNode
from meho_app.modules.agents.base.tool import BaseTool

__all__ = [
    "BaseAgent",
    "BaseNode",
    "BaseReduceDataNode",
    "BaseTool",
    "NodeResult",
    "infer",
    "one_shot",
    "quick_llm",
]
