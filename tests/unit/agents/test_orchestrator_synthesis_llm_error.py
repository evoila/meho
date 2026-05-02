# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Regression test for orchestrator synthesis LLMError final_answer contract.

When ``_stream_synthesis_chunks`` raises ``LLMError`` (retries exhausted, no
partial text accumulated), ``_synthesize_streaming``'s ``finally`` block must
leave ``state.final_answer`` as ``None`` -- not assign an empty string -- so
that downstream code reading ``success: state.final_answer is not None``
correctly reports failure and ``_close_run`` does not mark the transcript
``"completed"`` or fire memory extraction on a failed run.

Originally regressed in #496's first fix-up for B1 (the conditional
``if full_text:`` over-corrected by becoming an unconditional assignment that
also wrote ``""`` on the LLMError path). Locked down here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest

from meho_app.core.errors import LLMError
from meho_app.modules.agents.base.events import AgentEvent
from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent
from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput
from meho_app.modules.agents.orchestrator.state import OrchestratorState


class TestSynthesisLLMErrorFinalAnswerContract:
    """state.final_answer must remain None when synthesis raises LLMError."""

    @pytest.mark.asyncio
    async def test_final_answer_stays_none_when_stream_chunks_raises_llm_error(self):
        """LLMError from _stream_synthesis_chunks must leave state.final_answer untouched."""
        agent = object.__new__(OrchestratorAgent)
        agent.agent_name = "orchestrator"

        state = OrchestratorState(user_goal="Compare prod and staging")
        # Multi-connector to bypass single-connector passthrough fast path.
        state.all_findings = [
            SubgraphOutput("k8s-prod", "Production K8s", "10 pods", status="success"),
            SubgraphOutput("k8s-staging", "Staging K8s", "3 pods", status="success"),
        ]

        async def _raise_llm_error(*args, **kwargs) -> AsyncIterator[AgentEvent]:
            raise LLMError(
                "rate_limit",
                "transient",
                "Synthesis LLM exhausted retries (test)",
            )
            yield  # pragma: no cover -- keeps function an async generator

        with (
            patch.object(agent, "_build_synthesis_prompt", return_value=""),
            patch.object(agent, "_stream_synthesis_chunks", _raise_llm_error),
        ):
            with pytest.raises(LLMError):
                async for _ in agent._synthesize_streaming(state, session_id="test-llmerr"):
                    pass

        # The contract: final_answer stays None on LLMError so the downstream
        # success flag (state.final_answer is not None) reports False.
        assert state.final_answer is None

    @pytest.mark.asyncio
    async def test_final_answer_set_to_empty_string_when_stream_succeeds_with_no_text(self):
        """Empty-but-successful stream must set state.final_answer = "" (truthy `is not None`).

        Distinct from the LLMError case: an LLM that legitimately returns no chunks is a
        successful run with an empty answer; the success flag should still be True.
        """
        agent = object.__new__(OrchestratorAgent)
        agent.agent_name = "orchestrator"

        state = OrchestratorState(user_goal="Compare prod and staging")
        state.all_findings = [
            SubgraphOutput("k8s-prod", "Production K8s", "10 pods", status="success"),
            SubgraphOutput("k8s-staging", "Staging K8s", "3 pods", status="success"),
        ]

        async def _empty_stream(
            self_,
            state_,
            prompt_,
            session_id_,
            text_out,
        ) -> AsyncIterator[AgentEvent]:
            text_out.append("")
            return
            yield  # pragma: no cover -- keeps function an async generator

        with (
            patch.object(agent, "_build_synthesis_prompt", return_value=""),
            patch.object(
                agent,
                "_stream_synthesis_chunks",
                _empty_stream.__get__(agent, OrchestratorAgent),
            ),
        ):
            events = []
            async for event in agent._synthesize_streaming(state, session_id="test-empty"):
                events.append(event)

        assert state.final_answer == ""
        assert state.final_answer is not None  # downstream success flag reports True
