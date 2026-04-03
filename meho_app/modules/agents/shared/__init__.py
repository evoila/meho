# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Shared utilities and primitives for MEHO agents.

This module contains utilities shared across agent types
(specialist_agent, react_agent, etc.) to avoid code duplication.

Classes:
    MEHOGraphState: State that persists across the reasoning loop
    MEHOGraphDeps: Dependencies injected into graph nodes
    ParsedStep: Parsed output from LLM reasoning step
    WorkflowState: State for deterministic workflow (nodes-based)
    WorkflowResult: Result of the deterministic workflow
    BaseAgentState: Base class for ReAct loop state

Functions:
    build_tables_context: Build context string from cached tables
    execute_workflow: Parametrized workflow executor
"""

from meho_app.modules.agents.shared.context_utils import build_tables_context
from meho_app.modules.agents.shared.flow import execute_workflow
from meho_app.modules.agents.shared.graph.graph_deps import MEHOGraphDeps
from meho_app.modules.agents.shared.graph.graph_state import MEHOGraphState, ParsedStep
from meho_app.modules.agents.shared.state import (
    BaseAgentState,
    WorkflowResult,
    WorkflowState,
)

__all__ = [
    "BaseAgentState",
    "MEHOGraphDeps",
    "MEHOGraphState",
    "ParsedStep",
    "WorkflowResult",
    "WorkflowState",
    "build_tables_context",
    "execute_workflow",
]
