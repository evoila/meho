# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_backplane.agent`` — the in-process agent runtime (G11.1).

This package hosts the ``AgentRun`` seam: a thin, swappable wrapper around
the third-party agent loop (Pydantic AI) that runs one bounded tool-use
loop inside MEHO's own process, with every tool call routed through the
existing ``call_operation`` dispatch path.

The framework is deliberately confined behind :mod:`meho_backplane.agent.run`.
Nothing outside this package imports ``pydantic_ai``; callers depend on the
:class:`~meho_backplane.agent.run.AgentRun` Protocol and the
:class:`~meho_backplane.agent.run.AgentDefinition` /
:class:`~meho_backplane.agent.run.AgentRunHandle` value objects only. That
keeps the loop library replaceable (G11 Goal #800 architecture decision) and
keeps the run-handle store, audit wiring, and invocation surface ours.

The wider G11.1 initiative (#802) builds on this seam: definition persistence
(T2 #809), full toolset resolution (T3 #810), the public sync/async surface
(T4 #811), composition (T5 #812), and run records (T6 #813) all import the
types this package exports.
"""

from meho_backplane.agent.run import (
    AgentDefinition,
    AgentRun,
    AgentRunError,
    AgentRunEvent,
    AgentRunEventKind,
    AgentRunHandle,
    AgentRunResult,
    AgentRunStatus,
    ModelFactory,
    PydanticAgentRun,
    default_model_factory,
)
from meho_backplane.agent.toolset import (
    META_TOOL_NAMES,
    MetaToolSpec,
    resolve_agent_tools,
)

__all__ = [
    "META_TOOL_NAMES",
    "AgentDefinition",
    "AgentRun",
    "AgentRunError",
    "AgentRunEvent",
    "AgentRunEventKind",
    "AgentRunHandle",
    "AgentRunResult",
    "AgentRunStatus",
    "MetaToolSpec",
    "ModelFactory",
    "PydanticAgentRun",
    "default_model_factory",
    "resolve_agent_tools",
]
