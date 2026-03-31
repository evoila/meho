# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""ReactAgent - Generic ReAct agent for multi-system operations.

This module implements the ReactAgent class, which uses a ReAct
(Reasoning + Acting) loop to process user requests across any system.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from meho_app.core.otel import get_logger
from meho_app.modules.agents.base.agent import BaseAgent
from meho_app.modules.agents.base.events import AgentEvent
from meho_app.modules.agents.config.loader import AgentConfig, load_yaml_config
from meho_app.modules.agents.persistence.event_context import set_transcript_collector
from meho_app.modules.agents.persistence.helpers import create_transcript_collector
from meho_app.modules.agents.react_agent.nodes import create_node
from meho_app.modules.agents.react_agent.state import ReactAgentState
from meho_app.modules.agents.sse.emitter import EventEmitter

if TYPE_CHECKING:
    from meho_app.modules.agents.persistence.transcript_collector import TranscriptCollector

logger = get_logger(__name__)


@dataclass
class AgentDeps:
    """Dependencies container passed to nodes.

    Wraps the external dependencies and adds agent-specific config.
    Provides compatibility accessors for handlers that expect MEHOGraphDeps interface.
    """

    external_deps: Any  # MEHODependencies or similar
    agent_config: AgentConfig
    topology_context: str = ""
    conversation_history: str = ""
    data_reduction_context: dict[str, Any] | None = None
    session_id: str | None = None  # Chat session ID

    # =========================================================================
    # Compatibility accessors - match MEHOGraphDeps interface for handlers
    # =========================================================================

    @property
    def meho_deps(self) -> Any:
        """Alias for external_deps - handlers expect 'meho_deps'."""
        return self.external_deps

    @property
    def tenant_id(self) -> str:
        """Get tenant ID from MEHODependencies.user_context."""
        if self.external_deps and hasattr(self.external_deps, "user_context"):
            return self.external_deps.user_context.tenant_id
        return "default"

    @property
    def user_id(self) -> str:
        """Get user ID from MEHODependencies.user_context."""
        if self.external_deps and hasattr(self.external_deps, "user_context"):
            return self.external_deps.user_context.user_id
        return "anonymous"

    def __getattr__(self, name: str) -> Any:
        """Delegate remaining attribute access to external_deps."""
        return getattr(self.external_deps, name)


@dataclass
class ReactAgent(BaseAgent):
    """React agent implementation - generic ReAct loop for any system.

    This agent uses a loop of:
    1. Topology Lookup - check known context
    2. Reasoning - LLM thinks about what to do
    3. Action - execute a tool
    4. Observation - see the result
    5. Repeat until goal is achieved
    6. Topology Learn - extract discoveries

    Attributes:
        agent_name: Class variable identifying this agent type.
        dependencies: Injected service container.

    Example:
        >>> agent = ReactAgent(dependencies=deps)
        >>> async for event in agent.run_streaming("List all VMs"):
        ...     print(event.type, event.data)
    """

    agent_name: ClassVar[str] = "react"

    def _load_config(self) -> AgentConfig:
        """Load configuration from config.yaml in agent folder.

        Returns:
            Parsed and validated agent configuration.
        """
        return load_yaml_config(self.agent_folder / "config.yaml")

    def build_flow(self) -> str:
        """Build the node flow for this agent.

        Returns:
            Name of the entry node to start execution.
        """
        return "topology_lookup"

    async def run_streaming(
        self,
        user_message: str,
        session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute agent with SSE streaming output.

        Implements the full ReAct loop:
        1. Create state with user message
        2. Start with entry node (topology_lookup)
        3. Execute nodes in sequence
        4. Yield events as nodes emit them
        5. Handle max_steps and errors

        Args:
            user_message: The user's input message.
            session_id: Optional session ID for conversation tracking.
            context: Optional additional context (e.g., conversation history).

        Yields:
            AgentEvent objects for SSE streaming to frontend.
        """
        # Create state for this request
        state = ReactAgentState(user_goal=user_message)

        # Create event queue for collecting events from nodes
        event_queue: asyncio.Queue[AgentEvent] = asyncio.Queue()

        # Create emitter with queue
        emitter = EventEmitter(agent_name=self.agent_name, session_id=session_id)
        emitter.set_queue(event_queue)

        # Wrap dependencies to include agent config
        deps = AgentDeps(
            external_deps=self.dependencies,
            agent_config=self._config,
            conversation_history=context.get("history", "") if context else "",
            session_id=session_id,
        )

        # Set up transcript collector for deep observability
        transcript_collector: TranscriptCollector | None = None
        if session_id:
            transcript_collector = await create_transcript_collector(
                dependencies=self.dependencies,
                session_id=session_id,
                user_message=user_message,
                agent_name=self.agent_name,
            )
            if transcript_collector:
                set_transcript_collector(transcript_collector)

        # Emit agent start
        yield AgentEvent(
            type="agent_start",
            agent=self.agent_name,
            data={"user_message": user_message},
            session_id=session_id,
        )

        # Get entry node
        current_node_name: str | None = self.build_flow()
        max_steps = self._config.max_steps

        logger.info(f"Starting ReactAgent with goal: {user_message[:100]}...")

        try:
            # Main execution loop
            while current_node_name and state.step_count < max_steps:
                # Create node instance
                try:
                    node = create_node(current_node_name)
                except KeyError:
                    error_msg = f"Unknown node: {current_node_name}"
                    logger.error(error_msg)
                    yield AgentEvent(
                        type="error",
                        agent=self.agent_name,
                        data={"message": error_msg},
                        session_id=session_id,
                    )
                    break

                # Update emitter context
                emitter.set_context(step=state.step_count, node=current_node_name)

                # Execute node
                logger.debug(f"Executing node: {current_node_name} (step {state.step_count})")

                try:
                    result = await node.run(state, deps, emitter)

                    # Yield all queued events from the node
                    while not event_queue.empty():
                        event = await event_queue.get()
                        yield event

                    # Move to next node
                    current_node_name = result.next_node

                    # Check for completion conditions
                    if state.is_complete():
                        logger.info("Agent completed - final answer or error set")
                        break

                    # Check if paused for approval
                    if result.data and result.data.get("awaiting_approval"):
                        logger.info("Agent paused - awaiting approval")
                        # Yield any remaining events
                        while not event_queue.empty():
                            yield await event_queue.get()
                        break

                except Exception as e:
                    error_msg = f"Node execution error: {e}"
                    logger.exception(error_msg)
                    yield AgentEvent(
                        type="error",
                        agent=self.agent_name,
                        data={"message": error_msg, "node": current_node_name},
                        session_id=session_id,
                    )
                    state.error_message = error_msg
                    break

            # Check termination reason
            if state.step_count >= max_steps:
                error_msg = f"Max steps ({max_steps}) exceeded"
                logger.warning(error_msg)
                yield AgentEvent(
                    type="error",
                    agent=self.agent_name,
                    data={"message": error_msg},
                    session_id=session_id,
                )
                state.error_message = error_msg

        except Exception as e:
            error_msg = f"Agent execution error: {e}"
            logger.exception(error_msg)
            yield AgentEvent(
                type="error",
                agent=self.agent_name,
                data={"message": error_msg},
                session_id=session_id,
            )

        finally:
            # Always close transcript collector and clear context
            if transcript_collector:
                try:
                    if state.final_answer is not None:
                        tc_status = "completed"
                    elif state.error_message is not None:
                        tc_status = "failed"
                    else:
                        tc_status = "interrupted"
                    await transcript_collector.close(status=tc_status)
                    logger.debug(f"Transcript collector closed with status={tc_status}")
                except Exception as e:
                    logger.warning(f"Failed to close transcript collector: {e}")
                finally:
                    set_transcript_collector(None)

        # Yield any remaining queued events
        while not event_queue.empty():
            yield await event_queue.get()

        # Emit agent complete
        yield AgentEvent(
            type="agent_complete",
            agent=self.agent_name,
            data={
                "success": state.final_answer is not None,
                "steps": state.step_count,
                "has_error": state.error_message is not None,
            },
            session_id=session_id,
        )

        logger.info(
            f"ReactAgent finished: success={state.final_answer is not None}, steps={state.step_count}"
        )
