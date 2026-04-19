# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""React Agent - Generic ReAct agent for multi-system operations.

This module provides a ReAct (Reasoning + Acting) agent that can interact
with any system through connectors. It uses a loop of:
1. Reasoning (LLM thinks about what to do)
2. Action (execute a tool)
3. Observation (see the result)
4. Repeat until goal is achieved

Exports:
    ReactAgent: The main agent class implementing BaseAgent.

Example:
    >>> from meho_app.modules.agents.react_agent import ReactAgent
    >>> agent = ReactAgent(dependencies=deps)
    >>> async for event in agent.run_streaming("List all VMs"):
    ...     print(event.type, event.data)
"""

from __future__ import annotations

from .agent import ReactAgent

__all__: list[str] = [
    "ReactAgent",
]
