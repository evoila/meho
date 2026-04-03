# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Orchestrator Agent Module.

The orchestrator coordinates multiple connector-specific agents to answer
user queries across different systems. It uses an iterative parallel dispatch
model to:

1. Decide which connectors to query based on the user's goal
2. Dispatch queries to multiple agents in parallel
3. Collect and aggregate findings
4. Synthesize a final answer

Usage:
    from meho_app.modules.agents.orchestrator import OrchestratorAgent

    agent = OrchestratorAgent(dependencies=deps)
    async for event in agent.run_streaming(user_message):
        yield event
"""

# Contracts for subgraph communication
# OrchestratorAgent (Phase 2 - TASK-181)
from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent
from meho_app.modules.agents.orchestrator.contracts import (
    IterationResult,
    SubgraphInput,
    SubgraphOutput,
    WrappedEvent,
)

# Event wrapping for SSE
from meho_app.modules.agents.orchestrator.event_wrapper import EventWrapper

# State management
from meho_app.modules.agents.orchestrator.state import (
    ConnectorSelection,
    OrchestratorState,
)

__all__ = [
    # State
    "ConnectorSelection",
    # Event wrapper
    "EventWrapper",
    "IterationResult",
    # Agent
    "OrchestratorAgent",
    "OrchestratorState",
    # Contracts
    "SubgraphInput",
    "SubgraphOutput",
    "WrappedEvent",
]
