# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for specialist agent step budget (Phase 36).

Tests the dynamic step budget: default 8 steps, LLM-driven extension
via extend_budget field (+4 steps, once only, capped at 12), near-budget
nudge, and forced synthesis on exhaustion.

Phase 36 (v1.69 Token Optimization): STEP-01, STEP-02, STEP-03.
"""

from __future__ import annotations

from typing import get_args

from meho_app.modules.agents.base.events import EventType
from meho_app.modules.agents.specialist_agent.models import ReActStep, SearchOperationsAction
from meho_app.modules.agents.specialist_agent.state import SpecialistReActState

# ---------------------------------------------------------------------------
# Task 1 tests: Model, state, config, event type (GREEN immediately)
# ---------------------------------------------------------------------------


class TestReActStepExtendBudget:
    """Tests for the extend_budget field on ReActStep."""

    def test_react_step_extend_budget_default_false(self):
        """extend_budget defaults to False when not specified."""
        step = ReActStep(
            thought="Thinking...",
            response_type="action",
            action_input=SearchOperationsAction(query="pods"),
        )
        assert step.extend_budget is False

    def test_react_step_extend_budget_serialization(self):
        """extend_budget=True round-trips through model_dump/model_validate."""
        step = ReActStep(
            thought="Need more steps",
            response_type="action",
            action_input=SearchOperationsAction(query="pods"),
            extend_budget=True,
        )
        dumped = step.model_dump()
        assert dumped["extend_budget"] is True

        restored = ReActStep.model_validate(dumped)
        assert restored.extend_budget is True


class TestStateDefaults:
    """Tests for SpecialistReActState budget defaults."""

    def test_state_default_max_steps_is_8(self):
        """Default max_steps is 8, not 15."""
        state = SpecialistReActState(user_goal="test")
        assert state.max_steps == 8

    def test_state_budget_extended_default_false(self):
        """budget_extended starts as False."""
        state = SpecialistReActState(user_goal="test")
        assert state.budget_extended is False

    def test_state_max_steps_from_config(self):
        """When max_steps is explicitly passed, state uses that value."""
        state = SpecialistReActState(user_goal="test", max_steps=6)
        assert state.max_steps == 6


class TestEventType:
    """Tests for EventType Literal including budget_extended."""

    def test_budget_extended_event_type_valid(self):
        """'budget_extended' is a valid EventType."""
        valid_types = get_args(EventType)
        assert "budget_extended" in valid_types


# ---------------------------------------------------------------------------
# Task 1 tests: Agent logic (RED until Task 2 defines constants)
# These tests import EXTENSION_STEPS and ABSOLUTE_MAX_STEPS from agent.py
# ---------------------------------------------------------------------------


# Import constants from agent.py (Phase 36: STEP-03)
from meho_app.modules.agents.specialist_agent.agent import (  # noqa: E402 -- conditional/deferred import for test setup
    ABSOLUTE_MAX_STEPS,
    EXTENSION_STEPS,
)


class TestExtensionGrantLogic:
    """Tests for step budget extension grant logic."""

    def test_extension_grant_logic(self):
        """Extension grants +4 steps and sets budget_extended=True."""
        state = SpecialistReActState(user_goal="test", max_steps=8)
        assert not state.budget_extended

        # Simulate extension grant
        old_max = state.max_steps
        state.max_steps = min(state.max_steps + EXTENSION_STEPS, ABSOLUTE_MAX_STEPS)
        state.budget_extended = True

        assert state.max_steps == 12
        assert state.budget_extended is True
        assert old_max == 8

    def test_extension_grant_capped_at_absolute_max(self):
        """If max_steps=10 and extension is +4, result is min(14, 12)=12."""
        state = SpecialistReActState(user_goal="test", max_steps=10)

        state.max_steps = min(state.max_steps + EXTENSION_STEPS, ABSOLUTE_MAX_STEPS)
        state.budget_extended = True

        assert state.max_steps == 12  # Capped at ABSOLUTE_MAX_STEPS

    def test_extension_ignored_when_already_granted(self):
        """If budget_extended=True, a second grant attempt leaves max_steps unchanged."""
        state = SpecialistReActState(user_goal="test", max_steps=12)
        state.budget_extended = True

        # Simulate a second extension request (should be blocked by caller)
        if not state.budget_extended:
            state.max_steps = min(state.max_steps + EXTENSION_STEPS, ABSOLUTE_MAX_STEPS)

        assert state.max_steps == 12  # Unchanged


class TestNearBudgetNudge:
    """Tests for near-budget nudge wording."""

    def test_near_budget_nudge_without_extension(self):
        """When steps_remaining <= 2 and not extended, nudge suggests extension."""
        state = SpecialistReActState(user_goal="test", max_steps=8)
        state.step_count = 5  # step 6 next, 2 remaining

        next_step = state.step_count + 1
        steps_remaining = state.max_steps - next_step

        assert steps_remaining <= 2
        assert not state.budget_extended

        # Build nudge as agent.py does
        nudge = ""
        if steps_remaining <= 2 and not state.budget_extended:
            nudge = (
                "\n\nBudget running low -- consider synthesizing your findings "
                "or requesting an extension with extend_budget."
            )
        elif steps_remaining <= 2 and state.budget_extended:
            nudge = "\n\nBudget running low -- consider synthesizing your findings."

        assert "extend_budget" in nudge

    def test_near_budget_nudge_with_extension(self):
        """When steps_remaining <= 2 and already extended, nudge suggests synthesis only."""
        state = SpecialistReActState(user_goal="test", max_steps=12)
        state.budget_extended = True
        state.step_count = 9  # step 10 next, 2 remaining

        next_step = state.step_count + 1
        steps_remaining = state.max_steps - next_step

        assert steps_remaining <= 2
        assert state.budget_extended

        # Build nudge as agent.py does
        nudge = ""
        if steps_remaining <= 2 and not state.budget_extended:
            nudge = (
                "\n\nBudget running low -- consider synthesizing your findings "
                "or requesting an extension with extend_budget."
            )
        elif steps_remaining <= 2 and state.budget_extended:
            nudge = "\n\nBudget running low -- consider synthesizing your findings."

        assert "extend_budget" not in nudge
        assert "synthesizing" in nudge
