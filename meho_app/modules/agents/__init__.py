# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""MEHO Multi-Agent Architecture.

This module provides the complete agent system including:
- Base agent framework (BaseAgent, BaseTool, BaseNode)
- Agent dependencies and configuration (MEHODependencies, AgentService)
- Session state management (AgentSessionState)
- Data reduction and execution pipeline (UnifiedExecutor)
- Approval system
- SSE event system
"""

from __future__ import annotations

from typing import Any

from meho_app.modules.agents.base import (
    BaseAgent,
    BaseNode,
    BaseTool,
    NodeResult,
    infer,
)
from meho_app.modules.agents.base.events import AgentEvent, EventType
from meho_app.modules.agents.config import ModelConfig, load_yaml_config
from meho_app.modules.agents.sse import EventEmitter, EventRegistry

# Service-layer imports are lazy to avoid circular imports.
# The dependencies.py -> agent_factories.py -> ... chain pulls in heavy modules
# that may circularly reference this package during startup.

_LAZY_IMPORTS = {
    "AgentService": "meho_app.modules.agents.service",
    "get_agent_service": "meho_app.modules.agents.service",
    "router": "meho_app.modules.agents.routes",
    "AgentSessionState": "meho_app.modules.agents.session_state",
    "UnifiedExecutor": "meho_app.modules.agents.unified_executor",
    "get_unified_executor": "meho_app.modules.agents.unified_executor",
    "MEHODependencies": "meho_app.modules.agents.dependencies",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        import importlib

        # Safe (non-literal-import): module paths from _LAZY_IMPORTS dict with fixed hardcoded keys
        module = importlib.import_module(_LAZY_IMPORTS[name])
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # SSE
    "AgentEvent",
    # Service layer (merged from agent/)
    "AgentService",
    "AgentSessionState",
    # Base contracts
    "BaseAgent",
    "BaseNode",
    "BaseTool",
    "EventEmitter",
    "EventRegistry",
    "EventType",
    "MEHODependencies",
    # Config
    "ModelConfig",
    "NodeResult",
    "UnifiedExecutor",
    "get_agent_service",
    "get_unified_executor",
    # Utilities
    "infer",
    "load_yaml_config",
    "router",
]
