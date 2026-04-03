# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Base agent contract for all MEHO agents.

This module defines the abstract base class that all agents must implement.
Agents are self-contained units with configuration, tools, nodes, and SSE streaming.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from meho_app.modules.agents.base.events import AgentEvent
    from meho_app.modules.agents.base.node import BaseNode
    from meho_app.modules.agents.config.loader import AgentConfig
    from meho_app.modules.agents.sse.emitter import EventEmitter


@dataclass
class BaseAgent(ABC):
    """Abstract base class for all MEHO agents.

    Agents are self-contained units with:
    - Configuration loaded from YAML
    - Auto-loaded tools from tools/ folder
    - Auto-loaded nodes from nodes/ folder
    - SSE streaming support via EventEmitter

    Subclasses must define:
        agent_name: Class variable with unique identifier
        _load_config: Method to load configuration
        build_flow: Method to create the entry node
        run_streaming: Method to execute with SSE output

    Attributes:
        dependencies: Injected service container (MEHODependencies).

    Example:
        >>> @dataclass
        ... class ReactAgent(BaseAgent):
        ...     agent_name = "react"
        ...
        ...     def _load_config(self):
        ...         return load_yaml_config(self.agent_folder / "config.yaml")
        ...
        ...     def build_flow(self):
        ...         return "topology_lookup"  # Entry node name
        ...
        ...     async def run_streaming(self, user_message, ...):
        ...         # Execute agent loop...
        ...         yield event
    """

    # Injected dependencies
    dependencies: Any

    # Class variable - MUST be defined by subclass
    agent_name: ClassVar[str]

    # Internal state (initialized in __post_init__)
    _config: AgentConfig = field(init=False, repr=False)
    _tools: dict[str, Any] = field(init=False, default_factory=dict, repr=False)
    _nodes: dict[str, type[BaseNode]] = field(init=False, default_factory=dict, repr=False)
    _emitter: EventEmitter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialize internal state after dataclass fields are set.

        This method:
        1. Loads configuration from YAML
        2. Auto-discovers tools from tools/ folder
        3. Auto-discovers nodes from nodes/ folder
        4. Creates the SSE event emitter
        """
        # Lazy imports to avoid circular dependencies at module load time
        # These are from sibling packages, safe to import here
        from meho_app.modules.agents.config.loader import (
            load_nodes_from_folder,
            load_tools_from_folder,
        )
        from meho_app.modules.agents.sse.emitter import EventEmitter

        self._config = self._load_config()

        tools_folder = self.agent_folder / "tools"
        if tools_folder.exists():
            self._tools = load_tools_from_folder(tools_folder)

        nodes_folder = self.agent_folder / "nodes"
        if nodes_folder.exists():
            self._nodes = load_nodes_from_folder(nodes_folder)

        self._emitter = EventEmitter(agent_name=self.agent_name)

    @property
    def agent_folder(self) -> Path:
        """Path to this agent's folder.

        Auto-detected from the module location of the concrete class.

        Returns:
            Path to the folder containing this agent's files.
        """
        return Path(inspect.getfile(self.__class__)).parent

    @abstractmethod
    def _load_config(self) -> AgentConfig:
        """Load configuration from config.yaml in agent folder.

        Returns:
            Parsed and validated agent configuration.
        """
        ...

    @abstractmethod
    def build_flow(self) -> str:
        """Build the node flow for this agent.

        Returns:
            Name of the entry node to start execution.
        """
        ...

    @abstractmethod
    async def run_streaming(
        self,
        user_message: str,
        session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute agent with SSE streaming output.

        Args:
            user_message: The user's input message.
            session_id: Optional session ID for conversation tracking.
            context: Optional additional context (e.g., conversation history).

        Yields:
            AgentEvent objects for SSE streaming to frontend.
        """
        ...
        # This is needed to make the method an async generator
        if False:  # pragma: no cover
            yield

    async def run(
        self,
        user_message: str,
        **kwargs: Any,
    ) -> str:
        """Execute agent and return final answer (non-streaming).

        This is a convenience method that consumes the streaming output
        and returns only the final answer.

        Args:
            user_message: The user's input message.
            **kwargs: Additional arguments passed to run_streaming.

        Returns:
            The final answer string, or error message.
        """
        final_answer: str | None = None

        async for event in self.run_streaming(user_message, **kwargs):
            if event.type == "final_answer":
                final_answer = event.data.get("content", "")
            elif event.type == "error":
                return f"Error: {event.data.get('message', 'Unknown error')}"

        return final_answer or "No response generated"
