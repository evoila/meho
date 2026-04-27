# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for EventEmitter class.

These tests verify:
1. EventEmitter can emit events via callback
2. EventEmitter can emit events via queue
3. All typed helper methods work correctly
4. Context (step, node) is properly tracked
"""

from __future__ import annotations

import asyncio

import pytest

from meho_app.modules.agents.base.events import AgentEvent
from meho_app.modules.agents.sse import EventEmitter


class TestEventEmitterBasic:
    """Tests for basic EventEmitter functionality."""

    def test_create_emitter(self) -> None:
        """EventEmitter should be created with agent name."""
        emitter = EventEmitter(agent_name="react")
        assert emitter.agent_name == "react"
        assert emitter.session_id is None

    def test_create_emitter_with_session(self) -> None:
        """EventEmitter should accept session_id."""
        emitter = EventEmitter(agent_name="react", session_id="sess-123")
        assert emitter.session_id == "sess-123"

    def test_importable_from_sse(self) -> None:
        """EventEmitter should be importable from sse module."""
        from meho_app.modules.agents.sse import EventEmitter as ImportedEmitter

        assert ImportedEmitter is EventEmitter


class TestEventEmitterCallback:
    """Tests for callback-based event delivery."""

    @pytest.mark.asyncio
    async def test_emit_via_callback(self) -> None:
        """Events should be delivered via callback."""
        events: list[AgentEvent] = []

        def capture_event(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture_event)

        await emitter.emit("thought", {"content": "Test thought"})

        assert len(events) == 1
        assert events[0].type == "thought"
        assert events[0].agent == "react"
        assert events[0].data["content"] == "Test thought"

    @pytest.mark.asyncio
    async def test_multiple_events_via_callback(self) -> None:
        """Multiple events should be delivered in order."""
        events: list[AgentEvent] = []

        def capture_event(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture_event)

        await emitter.emit("thought", {"content": "First"})
        await emitter.emit("action", {"tool": "search", "args": {}})
        await emitter.emit("observation", {"tool": "search", "result": "data"})

        assert len(events) == 3
        assert events[0].type == "thought"
        assert events[1].type == "action"
        assert events[2].type == "observation"


class TestEventEmitterQueue:
    """Tests for queue-based event delivery."""

    @pytest.mark.asyncio
    async def test_emit_via_queue(self) -> None:
        """Events should be delivered via queue."""
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()

        emitter = EventEmitter(agent_name="react")
        emitter.set_queue(queue)

        await emitter.emit("thought", {"content": "Test"})

        event = await queue.get()
        assert event.type == "thought"
        assert event.data["content"] == "Test"

    @pytest.mark.asyncio
    async def test_emit_without_callback_or_queue(self) -> None:
        """Emit should not raise if no callback or queue set."""
        emitter = EventEmitter(agent_name="react")

        # Should not raise
        await emitter.emit("thought", {"content": "Test"})


class TestEventEmitterContext:
    """Tests for context tracking."""

    @pytest.mark.asyncio
    async def test_set_context_step(self) -> None:
        """Events should include step when set."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)
        emitter.set_context(step=5)

        await emitter.emit("thought", {"content": "Test"})

        assert events[0].step == 5

    @pytest.mark.asyncio
    async def test_set_context_node(self) -> None:
        """Events should include node when set."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)
        emitter.set_context(node="reason")

        await emitter.emit("thought", {"content": "Test"})

        assert events[0].node == "reason"

    @pytest.mark.asyncio
    def test_increment_step(self) -> None:
        """increment_step should increase and return step."""
        emitter = EventEmitter(agent_name="react")

        step1 = emitter.increment_step()
        step2 = emitter.increment_step()
        step3 = emitter.increment_step()

        assert step1 == 1
        assert step2 == 2
        assert step3 == 3

    @pytest.mark.asyncio
    async def test_session_id_in_events(self) -> None:
        """Events should include session_id."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react", session_id="sess-456")
        emitter.set_callback(capture)

        await emitter.emit("thought", {"content": "Test"})

        assert events[0].session_id == "sess-456"


class TestTypedHelpers:
    """Tests for typed helper methods."""

    @pytest.mark.asyncio
    async def test_thought_helper(self) -> None:
        """thought() should emit thought event."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        await emitter.thought("I need to search for data")

        assert events[0].type == "thought"
        assert events[0].data["content"] == "I need to search for data"

    @pytest.mark.asyncio
    async def test_action_helper(self) -> None:
        """action() should emit action event."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        await emitter.action("search_operations", {"connector_id": "abc"})

        assert events[0].type == "action"
        assert events[0].data["tool"] == "search_operations"
        assert events[0].data["args"]["connector_id"] == "abc"

    @pytest.mark.asyncio
    async def test_observation_helper(self) -> None:
        """observation() should emit observation event."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        await emitter.observation("search", [{"id": 1}])

        assert events[0].type == "observation"
        assert events[0].data["tool"] == "search"

    @pytest.mark.asyncio
    async def test_observation_truncates_large_results(self) -> None:
        """observation() should truncate large results."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        large_result = "x" * 10000
        await emitter.observation("search", large_result)

        assert len(events[0].data["result"]) < 6000
        assert "[truncated]" in events[0].data["result"]

    @pytest.mark.asyncio
    async def test_final_answer_helper(self) -> None:
        """final_answer() should emit final_answer event."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        await emitter.final_answer("Here are the results")

        assert events[0].type == "final_answer"
        assert events[0].data["content"] == "Here are the results"

    @pytest.mark.asyncio
    async def test_error_helper(self) -> None:
        """error() should emit error event."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        await emitter.error("Connection failed", details={"host": "localhost"})

        assert events[0].type == "error"
        assert events[0].data["message"] == "Connection failed"
        assert events[0].data["details"]["host"] == "localhost"

    @pytest.mark.asyncio
    async def test_tool_start_helper(self) -> None:
        """tool_start() should emit tool_start event."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        await emitter.tool_start("list_connectors")

        assert events[0].type == "tool_start"
        assert events[0].data["tool"] == "list_connectors"

    @pytest.mark.asyncio
    async def test_tool_complete_helper(self) -> None:
        """tool_complete() should emit tool_complete event."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        await emitter.tool_complete("list_connectors", success=True)

        assert events[0].type == "tool_complete"
        assert events[0].data["success"] is True

    @pytest.mark.asyncio
    async def test_node_enter_helper(self) -> None:
        """node_enter() should emit node_enter and update context."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        await emitter.node_enter("reason")

        assert events[0].type == "node_enter"
        assert events[0].data["node"] == "reason"
        # Should also update internal context
        assert emitter._current_node == "reason"

    @pytest.mark.asyncio
    async def test_node_exit_helper(self) -> None:
        """node_exit() should emit node_exit event."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        await emitter.node_exit("reason", next_node="tool_dispatch")

        assert events[0].type == "node_exit"
        assert events[0].data["node"] == "reason"
        assert events[0].data["next_node"] == "tool_dispatch"

    @pytest.mark.asyncio
    async def test_approval_required_helper(self) -> None:
        """approval_required() should emit approval_required event."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        await emitter.approval_required(
            tool="call_operation",
            args={"operation_id": "delete_vm"},
            danger_level="critical",
            description="Delete VM 'web-01'",
        )

        assert events[0].type == "approval_required"
        assert events[0].data["tool"] == "call_operation"
        assert events[0].data["danger_level"] == "critical"

    @pytest.mark.asyncio
    async def test_agent_start_helper(self) -> None:
        """agent_start() should emit agent_start event."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        await emitter.agent_start("List all VMs")

        assert events[0].type == "agent_start"
        assert events[0].data["user_message"] == "List all VMs"

    @pytest.mark.asyncio
    async def test_agent_complete_helper(self) -> None:
        """agent_complete() should emit agent_complete event."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        await emitter.agent_complete(success=True)

        assert events[0].type == "agent_complete"
        assert events[0].data["success"] is True

    @pytest.mark.asyncio
    async def test_progress_helper(self) -> None:
        """progress() should emit progress event."""
        events: list[AgentEvent] = []

        def capture(event: AgentEvent) -> None:
            events.append(event)

        emitter = EventEmitter(agent_name="react")
        emitter.set_callback(capture)

        await emitter.progress("Loading...", percentage=50)

        assert events[0].type == "progress"
        assert events[0].data["message"] == "Loading..."
        assert events[0].data["percentage"] == 50
