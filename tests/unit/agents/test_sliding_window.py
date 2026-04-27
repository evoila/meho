# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for specialist agent sliding window scratchpad.

Tests StepRecord data structure, step summarization via Haiku 4.5,
collapse logic, and key parameter extraction.
Phase 34 (v1.69 Token Optimization): SLID-01 through SLID-03.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch

import pytest

from meho_app.modules.agents.specialist_agent.summarizer import (
    StepRecord,
    _extract_key_param,
    collapse_old_steps,
    summarize_step,
)

# ─────────────────────────────────────────────────────────────────────────────
# Minimal state stub for tests (mirrors fields used by collapse_old_steps)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _StubState:
    """Minimal stub matching SpecialistReActState fields used by collapse."""

    completed_steps: list[StepRecord] = field(default_factory=list)
    window_size: int = 3


# ─────────────────────────────────────────────────────────────────────────────
# StepRecord tests
# ─────────────────────────────────────────────────────────────────────────────


class TestStepRecord:
    def test_step_record_not_summarized_by_default(self) -> None:
        step = StepRecord(
            step_number=1,
            tool="search_operations",
            action_input_key="pods",
            observation="Found 12 operations",
        )
        assert step.summary is None
        assert step.is_summarized is False

    def test_step_record_is_summarized_when_set(self) -> None:
        step = StepRecord(
            step_number=1,
            tool="search_operations",
            action_input_key="pods",
            observation="Found 12 operations",
            summary="Step 1: search_operations(pods) -> Found 12 ops",
        )
        assert step.is_summarized is True
        assert step.summary == "Step 1: search_operations(pods) -> Found 12 ops"


# ─────────────────────────────────────────────────────────────────────────────
# summarize_step tests
# ─────────────────────────────────────────────────────────────────────────────


class TestSummarizeStep:
    @pytest.mark.asyncio
    @patch("meho_app.modules.agents.specialist_agent.summarizer.infer")
    async def test_summarize_step_calls_haiku(self, mock_infer: AsyncMock) -> None:
        mock_infer.return_value = "Step 1: search_operations(pods) -> Found 12 ops"
        step = StepRecord(
            step_number=1,
            tool="search_operations",
            action_input_key="pods",
            observation="Found 12 pod management operations",
        )
        result = await summarize_step(step, current_goal="Find pods in CrashLoopBackOff")
        assert result == "Step 1: search_operations(pods) -> Found 12 ops"

        mock_infer.assert_called_once()
        call_kwargs = mock_infer.call_args
        assert call_kwargs.kwargs["model"] == "anthropic:claude-haiku-4-5"
        assert call_kwargs.kwargs["temperature"] == pytest.approx(0.0)
        # System prompt should explain the format
        assert "ONE line" in call_kwargs.kwargs["system_prompt"]
        # Message should include goal, tool, observation
        assert "Find pods in CrashLoopBackOff" in call_kwargs.kwargs["message"]
        assert "search_operations" in call_kwargs.kwargs["message"]
        assert "Found 12 pod management operations" in call_kwargs.kwargs["message"]

    @pytest.mark.asyncio
    @patch("meho_app.modules.agents.specialist_agent.summarizer.infer")
    async def test_summarize_step_returns_stripped_response(self, mock_infer: AsyncMock) -> None:
        mock_infer.return_value = "  Step 1: search_operations(pods) -> Found 12 ops  \n"
        step = StepRecord(
            step_number=1,
            tool="search_operations",
            action_input_key="pods",
            observation="Found 12 operations",
        )
        result = await summarize_step(step, current_goal="test")
        assert result == "Step 1: search_operations(pods) -> Found 12 ops"

    @pytest.mark.asyncio
    @patch("meho_app.modules.agents.specialist_agent.summarizer.infer")
    async def test_summarize_step_fallback_on_exception(self, mock_infer: AsyncMock) -> None:
        mock_infer.side_effect = Exception("API timeout")
        step = StepRecord(
            step_number=3,
            tool="reduce_data",
            action_input_key="SELECT * FROM pods",
            observation="| name | status |\n| pod-1 | Running |\n| pod-2 | Failed |",
        )
        result = await summarize_step(step, current_goal="test")
        # Should produce rule-based fallback
        assert result.startswith("Step 3: reduce_data(SELECT * FROM pods) -> ")
        assert "name" in result  # observation preview
        assert "\n" not in result  # newlines replaced with spaces


# ─────────────────────────────────────────────────────────────────────────────
# collapse_old_steps tests
# ─────────────────────────────────────────────────────────────────────────────


class TestCollapseOldSteps:
    @pytest.mark.asyncio
    @patch("meho_app.modules.agents.specialist_agent.summarizer.infer")
    async def test_collapse_noop_when_window_not_full(self, mock_infer: AsyncMock) -> None:
        state = _StubState(
            completed_steps=[
                StepRecord(1, "search_operations", "pods", "Found 12 ops"),
                StepRecord(2, "call_operation", "list-pods", "Cached table"),
            ],
            window_size=3,
        )
        await collapse_old_steps(state, current_thought="investigating")  # type: ignore[arg-type]
        mock_infer.assert_not_called()

    @pytest.mark.asyncio
    @patch("meho_app.modules.agents.specialist_agent.summarizer.infer")
    async def test_collapse_summarizes_oldest_step(self, mock_infer: AsyncMock) -> None:
        mock_infer.return_value = "Step 1: search_operations(pods) -> Found 12 ops"
        state = _StubState(
            completed_steps=[
                StepRecord(1, "search_operations", "pods", "Found 12 operations"),
                StepRecord(2, "call_operation", "list-pods", "Cached table 'pods': 47 rows"),
                StepRecord(3, "reduce_data", "SELECT * FROM pods", "3 pods in CrashLoopBackOff"),
                StepRecord(
                    4, "lookup_topology", "worker-01", "worker-01 (Node) ==SAME_AS== instance-xyz"
                ),
            ],
            window_size=3,
        )
        await collapse_old_steps(state, current_thought="checking topology")  # type: ignore[arg-type]
        # Only step 1 should be summarized (boundary = 4 - 3 = 1)
        assert state.completed_steps[0].is_summarized is True
        assert state.completed_steps[0].summary == "Step 1: search_operations(pods) -> Found 12 ops"
        # Steps 2-4 should remain unsummarized
        assert state.completed_steps[1].is_summarized is False
        assert state.completed_steps[2].is_summarized is False
        assert state.completed_steps[3].is_summarized is False
        mock_infer.assert_called_once()

    @pytest.mark.asyncio
    @patch("meho_app.modules.agents.specialist_agent.summarizer.infer")
    async def test_collapse_skips_already_summarized(self, mock_infer: AsyncMock) -> None:
        state = _StubState(
            completed_steps=[
                StepRecord(
                    1,
                    "search_operations",
                    "pods",
                    "Found 12 ops",
                    summary="Step 1: search_operations(pods) -> Found 12 ops",
                ),
                StepRecord(2, "call_operation", "list-pods", "Cached table"),
                StepRecord(3, "reduce_data", "SELECT ...", "3 pods"),
                StepRecord(4, "lookup_topology", "worker-01", "topology result"),
            ],
            window_size=3,
        )
        await collapse_old_steps(state, current_thought="investigating")  # type: ignore[arg-type]
        # Step 1 already summarized -- no infer calls
        mock_infer.assert_not_called()

    @pytest.mark.asyncio
    @patch("meho_app.modules.agents.specialist_agent.summarizer.infer")
    async def test_collapse_multiple_steps(self, mock_infer: AsyncMock) -> None:
        mock_infer.side_effect = [
            "Step 1: search_operations(pods) -> Found 12 ops",
            "Step 2: call_operation(list-pods) -> Cached 47 rows",
            "Step 3: reduce_data(SELECT ...) -> 3 CrashLoopBackOff",
        ]
        state = _StubState(
            completed_steps=[
                StepRecord(1, "search_operations", "pods", "Found 12 operations"),
                StepRecord(2, "call_operation", "list-pods", "Cached table 'pods': 47 rows"),
                StepRecord(3, "reduce_data", "SELECT ...", "3 pods in CrashLoopBackOff"),
                StepRecord(4, "lookup_topology", "worker-01", "topology result"),
                StepRecord(5, "search_knowledge", "runbook", "Node memory docs"),
                StepRecord(6, "call_operation", "get-metrics", "Metrics table"),
            ],
            window_size=3,
        )
        await collapse_old_steps(state, current_thought="analyzing metrics")  # type: ignore[arg-type]
        # Steps 1-3 should all be summarized (boundary = 6 - 3 = 3)
        assert state.completed_steps[0].is_summarized is True
        assert state.completed_steps[1].is_summarized is True
        assert state.completed_steps[2].is_summarized is True
        # Steps 4-6 remain unsummarized
        assert state.completed_steps[3].is_summarized is False
        assert state.completed_steps[4].is_summarized is False
        assert state.completed_steps[5].is_summarized is False
        assert mock_infer.call_count == 3


# ─────────────────────────────────────────────────────────────────────────────
# _extract_key_param tests
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractKeyParam:
    def test_extract_key_param_known_tools(self) -> None:
        assert _extract_key_param("search_operations", {"query": "pods"}) == "pods"
        assert _extract_key_param("call_operation", {"operation_id": "list-pods"}) == "list-pods"
        assert (
            _extract_key_param("reduce_data", {"sql": "SELECT * FROM pods"}) == "SELECT * FROM pods"
        )
        assert _extract_key_param("lookup_topology", {"query": "worker-01"}) == "worker-01"
        assert _extract_key_param("search_knowledge", {"query": "runbook"}) == "runbook"
        assert _extract_key_param("store_memory", {"title": "pod status"}) == "pod status"
        assert _extract_key_param("forget_memory", {"query": "old memory"}) == "old memory"
        assert _extract_key_param("invalidate_topology", {"query": "stale"}) == "stale"

    def test_extract_key_param_truncation(self) -> None:
        long_sql = "SELECT name, status, cpu_usage, memory_usage FROM pods WHERE status = 'CrashLoopBackOff'"
        assert len(long_sql) > 60
        result = _extract_key_param("reduce_data", {"sql": long_sql})
        assert len(result) == 60
        assert result.endswith("...")

    def test_extract_key_param_none_input(self) -> None:
        assert _extract_key_param("search_operations", None) == ""

    def test_extract_key_param_unknown_tool(self) -> None:
        assert _extract_key_param("unknown_tool", {"query": "test"}) == ""
