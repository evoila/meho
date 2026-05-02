# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Orchestrator Agent - Coordinates parallel dispatch to connector-specific agents.

This module implements the OrchestratorAgent class that:
- Uses an iterative parallel dispatch loop to query multiple connectors
- Streams events via SSE as agents complete (TTFUR)
- Synthesizes findings from all connectors into a unified answer

Event Flow:
    orchestrator_start -> iteration_start -> dispatch_start ->
    [agent_event]* -> early_findings/connector_complete ->
    iteration_complete -> [repeat or] -> synthesis_start ->
    final_answer -> orchestrator_complete
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import yaml
from opentelemetry.trace import Span, Status, StatusCode, use_span
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from meho_app.core.errors import (
    InternalError,
    LLMError,
    UpstreamApiError,
    get_current_trace_id,
)
from meho_app.core.otel import get_logger, get_tracer
from meho_app.modules.agents.base.agent import BaseAgent
from meho_app.modules.agents.base.events import AgentEvent
from meho_app.modules.agents.config.loader import AgentConfig, load_yaml_config
from meho_app.modules.agents.orchestrator.ask_mode import run_ask_mode
from meho_app.modules.agents.orchestrator.context_passing import (
    parse_discovered_entities,
)
from meho_app.modules.agents.orchestrator.contracts import (
    SubgraphOutput,
    WrappedEvent,
)
from meho_app.modules.agents.orchestrator.event_wrapper import EventWrapper
from meho_app.modules.agents.orchestrator.history import parse_history_text
from meho_app.modules.agents.orchestrator.routing import (
    build_routing_prompt,
    parse_decision,
)
from meho_app.modules.agents.orchestrator.state import (
    ConnectorSelection,
    OrchestratorState,
)
from meho_app.modules.agents.orchestrator.streaming import build_agent_prompt
from meho_app.modules.agents.orchestrator.synthesis import (
    build_conversational_prompt,
    build_multi_turn_synthesis_context,
    build_synthesis_prompt,
)
from meho_app.modules.agents.orchestrator.topology_routing import (
    build_topology_investigation_context,
    route_via_topology,
)
from meho_app.modules.agents.orchestrator.transcript import (
    create_transcript_collector,
)
from meho_app.modules.agents.orchestrator.utils import log_structured
from meho_app.modules.agents.unified_executor import get_unified_executor

if TYPE_CHECKING:
    from meho_app.modules.agents.persistence.transcript_collector import (
        TranscriptCollector,
    )

from meho_app.modules.agents.persistence.event_context import set_transcript_collector

logger = get_logger(__name__)
tracer = get_tracer("meho.orchestrator")


from meho_app.modules.agents.base.retry import (  # noqa: E402 -- conditional/deferred import
    retry_llm_call as _retry_llm_call,
)


@dataclass
class OrchestratorAgent(BaseAgent):
    """Orchestrator using iterative parallel dispatch.

    The orchestrator coordinates queries across multiple connectors:
    1. Decide which connectors to query (or respond if sufficient)
    2. Dispatch to selected connectors IN PARALLEL
    3. Stream events as they arrive (TTFUR)
    4. Aggregate findings
    5. Repeat or synthesize final answer

    Attributes:
        agent_name: Class variable identifying this agent type.
        dependencies: Injected service container (MEHODependencies).

    Example:
        >>> agent = OrchestratorAgent(dependencies=deps)
        >>> async for event in agent.run_streaming("Why is my app slow?"):
        ...     print(event.type, event.data)
    """

    agent_name: ClassVar[str] = "orchestrator"

    def _load_config(self) -> AgentConfig:
        """Load configuration from config.yaml in agent folder.

        Validates required orchestrator settings on startup.

        Returns:
            AgentConfig with validated orchestrator settings.

        Raises:
            ValueError: If required settings are missing or invalid.
        """
        config = load_yaml_config(self.agent_folder / "config.yaml")
        self._validate_orchestrator_config(config)
        return config

    def _validate_orchestrator_config(self, config: AgentConfig) -> None:
        """Validate orchestrator-specific configuration.

        Checks for required settings and validates value ranges.

        Args:
            config: The loaded agent configuration.

        Raises:
            ValueError: If configuration is invalid.
        """
        raw = config.raw
        errors: list[str] = []
        if "orchestrator" not in raw:
            errors.append("Missing 'orchestrator' section in config")
        else:
            self._validate_orch_section(raw["orchestrator"], errors)
        if errors:
            error_msg = "Orchestrator config validation failed:\n  - " + "\n  - ".join(errors)
            logger.error(error_msg)
            raise ValueError(error_msg)

    def _validate_orch_section(self, orch: dict[str, Any], errors: list[str]) -> None:
        max_iter = orch.get("max_iterations")
        if max_iter is not None and (not isinstance(max_iter, int) or max_iter < 1):
            errors.append(f"max_iterations must be a positive integer, got: {max_iter}")
        for field in ("agent_timeout", "total_timeout"):
            val = orch.get(field)
            if val is not None:
                self._validate_positive_float(field, val, errors)
        dispatch = orch.get("dispatch", {})
        if dispatch:
            self._validate_dispatch_section(dispatch, errors)

    def _validate_positive_float(self, name: str, value: Any, errors: list[str]) -> None:
        try:
            timeout_val = float(value)
            if timeout_val <= 0:
                errors.append(f"{name} must be positive, got: {timeout_val}")
        except (TypeError, ValueError):
            errors.append(f"{name} must be a number, got: {value}")

    def _validate_dispatch_section(self, dispatch: dict[str, Any], errors: list[str]) -> None:
        max_conn = dispatch.get("max_connectors_per_iteration")
        if max_conn is not None and (not isinstance(max_conn, int) or max_conn < 1):
            errors.append(
                f"dispatch.max_connectors_per_iteration must be a positive integer, got: {max_conn}"
            )
        min_score = dispatch.get("min_relevance_score")
        if min_score is not None:
            try:
                score = float(min_score)
                if not 0.0 <= score <= 1.0:
                    errors.append(
                        f"dispatch.min_relevance_score must be between 0.0 and 1.0, got: {score}"
                    )
            except (TypeError, ValueError):
                errors.append(f"dispatch.min_relevance_score must be a number, got: {min_score}")

    def build_flow(self) -> str:
        """Build flow - not used by orchestrator (uses loop instead)."""
        return "orchestrator_loop"

    async def run_streaming(
        self,
        user_message: str,
        session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute the orchestrator loop.

        Args:
            user_message: The user's input message.
            session_id: Optional session ID for conversation tracking.
            context: Optional additional context (history, session_state, session_mode from adapter).

        Yields:
            AgentEvent objects for SSE streaming to frontend.
        """
        # Phase 65: Mode-aware dispatch -- ask mode bypasses connector action dispatch
        session_mode = (context or {}).get("session_mode", "agent")
        if session_mode == "ask":
            async for event in run_ask_mode(self, user_message, session_id, context):
                yield event
            return

        state, transcript_collector, session_state = await self._build_run_state(
            user_message, session_id, context
        )
        is_partial = False
        loop_error: Exception | None = None

        # Start OTEL span for orchestrator run (OBSV-03).
        # use_span sets this as current context so child spans auto-parent.
        root_span = tracer.start_span(
            "meho.orchestrator.run",
            attributes={
                "meho.session_id": session_id or "",
                "meho.user_query": user_message[:200],
                "meho.max_iterations": state.max_iterations,
            },
        )
        _root_span_ctx = use_span(root_span, end_on_exit=False)
        _root_span_ctx.__enter__()

        try:
            # Emit orchestrator start
            yield AgentEvent(
                type="orchestrator_start",
                agent=self.agent_name,
                data={"goal": user_message},
                session_id=session_id,
            )

            log_structured(
                logging.INFO,
                "orchestrator_start",
                "Orchestrator starting",
                session_id=session_id,
                goal_length=len(user_message),
                max_iterations=state.max_iterations,
                goal_preview=(
                    user_message[:100] + "..." if len(user_message) > 100 else user_message
                ),
            )
            # Main iteration loop
            while state.should_continue and state.current_iteration < state.max_iterations:
                iteration = state.current_iteration + 1

                # STEP 1: Decide which connectors to query (or respond)
                yield AgentEvent(
                    type="iteration_start",
                    agent=self.agent_name,
                    data={"iteration": iteration},
                    session_id=session_id,
                )

                # Graceful degradation: wrap decision in try/catch
                try:
                    # Phase 110: Collect thinking events during routing LLM call
                    thinking_events: list[AgentEvent] = []
                    decision = await self._decide_next_action(
                        state,
                        thinking_events=thinking_events,
                        session_id=session_id,
                    )
                    # Yield thinking events BEFORE the orchestrator_plan event
                    for thinking_event in thinking_events:
                        yield thinking_event
                except Exception as e:
                    logger.error(f"Decision failed at iteration {iteration}: {e}")
                    if state.all_findings:
                        # Have some findings - proceed to synthesis with what we have
                        logger.info("Proceeding to synthesis with existing findings")
                        is_partial = True
                        loop_error = e
                        state.should_continue = False
                        break
                    else:
                        # No findings at all - can't recover
                        raise

                # Phase 77 D-11: Record LLM-judged novelty from routing decision
                # For iterations after the first, the routing LLM has assessed all
                # accumulated findings and reported has_novelty. Feed this into convergence.
                if state.current_iteration > 0:
                    llm_novelty = decision.get("_llm_has_novelty", True)
                    state.record_novelty(llm_novelty)

                if decision["action"] == "respond":
                    # LLM decided we have enough info
                    log_structured(
                        logging.INFO,
                        "iteration_decision",
                        f"Deciding to respond at iteration {iteration}",
                        session_id=session_id,
                        iteration=iteration,
                        action="respond",
                        connectors_selected=[],
                        findings_count=len(state.all_findings),
                    )
                    state.should_continue = False
                    break

                # STEP 2: Resolve selection and emit plan events
                selected = self._resolve_dispatch_selection(decision, state)
                async for event in self._emit_plan_events(
                    decision, selected, iteration, session_id
                ):
                    yield event

                # STEP 3: Parallel execution - stream events as they arrive
                outputs: list[SubgraphOutput] = []
                async for event in self._stream_dispatch_round(
                    state, selected, iteration, session_id, outputs
                ):
                    yield event

                # STEP 4: Aggregate findings
                state.add_iteration_findings(outputs)

                yield AgentEvent(
                    type="iteration_complete",
                    agent=self.agent_name,
                    data={
                        "iteration": iteration,
                        "findings_count": len(outputs),
                        "total_findings": len(state.all_findings),
                    },
                    session_id=session_id,
                )

                # Calculate status counts for structured logging
                success_count = sum(1 for o in outputs if o.status == "success")
                timeout_count = sum(1 for o in outputs if o.status == "timeout")
                error_count = sum(1 for o in outputs if o.status in ("failed", "cancelled"))
                iter_duration_ms = (time.time() * 1000) - state.start_time_ms

                log_structured(
                    logging.INFO,
                    "iteration_complete",
                    f"Iteration {iteration} complete",
                    session_id=session_id,
                    iteration=iteration,
                    findings_count=len(outputs),
                    success_count=success_count,
                    timeout_count=timeout_count,
                    error_count=error_count,
                    total_findings=len(state.all_findings),
                    iteration_time_ms=int(iter_duration_ms),
                )

                # NOTE: We previously had an optimization here that skipped the LLM
                # decision call if all connectors succeeded. This was removed because
                # it prevented the orchestrator from iterating on multi-part queries
                # (e.g., "get namespaces AND get pods from X namespace").
                # The LLM decision is now always made to properly evaluate if the
                # query was fully answered.

                # Phase 77: Topology-driven follow-up traversal
                state.record_dispatches([c.connector_id for c in selected])
                initial_has_novelty = decision.get("_llm_has_novelty", True) if decision else True
                state.record_novelty(initial_has_novelty)
                async for event in self._stream_topology_traversal(
                    state, outputs, iteration, session_id
                ):
                    yield event

        except GeneratorExit:
            logger.warning(f"Generator cancelled for session {session_id} - client disconnected")
            is_partial = True
            if transcript_collector:
                with contextlib.suppress(Exception):
                    await transcript_collector.add_error_event(
                        "Client disconnected - generator cancelled"
                    )
            raise

        except Exception as e:
            error_msg = f"Orchestrator error: {e}"
            logger.exception(error_msg)
            loop_error = e
            is_partial = True

            if transcript_collector:
                with contextlib.suppress(Exception):
                    await transcript_collector.add_error_event(str(e), include_traceback=True)

            yield AgentEvent(
                type="error",
                agent=self.agent_name,
                data={
                    "message": error_msg,
                    "findings_so_far": len(state.all_findings),
                    **self._classify_loop_error(e, state),
                },
                session_id=session_id,
            )
            async for event in self._stream_completion(
                state, transcript_collector, is_partial, loop_error, session_id
            ):
                yield event

        else:
            async for event in self._stream_completion(
                state,
                transcript_collector,
                is_partial,
                loop_error,
                session_id,
                session_state=session_state,
            ):
                yield event

        finally:
            await self._close_run(
                state, transcript_collector, root_span, _root_span_ctx, is_partial, session_id
            )

    # =========================================================================
    # run_streaming helpers
    # =========================================================================

    def _resolve_dispatch_selection(
        self,
        decision: dict[str, Any],
        state: OrchestratorState,
    ) -> list[ConnectorSelection]:
        """Apply quick/standard classification to the routing decision's connector list.

        Phase 99: quick mode caps to one priority-1 connector and disables further
        iterations; standard mode splits priority-1 and priority-2 connectors,
        storing priority-2 for a potential follow-up iteration.

        Args:
            decision: Routing LLM decision dict (must contain "connectors" key).
            state: Current orchestrator state (mutated: selected_connectors,
                should_continue, _deferred_connectors as appropriate).

        Returns:
            Resolved list of ConnectorSelection objects for the upcoming dispatch round.
        """
        selected: list[ConnectorSelection] = decision["connectors"]
        state.selected_connectors = selected
        classification = decision.get("classification", "standard")

        if classification == "quick":
            selected = [c for c in selected if c.priority == 1][:1]
            state.should_continue = False
            state.selected_connectors = selected
        elif classification == "standard":
            priority_1 = [c for c in selected if c.priority == 1]
            priority_2 = [c for c in selected if c.priority == 2 or c.conditional]
            if priority_1:
                state._deferred_connectors = priority_2
                selected = priority_1
                state.selected_connectors = selected

        return selected

    async def _emit_plan_events(
        self,
        decision: dict[str, Any],
        selected: list[ConnectorSelection],
        iteration: int,
        session_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        """Yield orchestrator_plan and dispatch_start events then log the dispatch.

        orchestrator_plan shows the full unfiltered connector list from the routing
        decision (visible reasoning for the user). dispatch_start shows only the
        connectors actually dispatched this round (the filtered ``selected`` list).

        Args:
            decision: Routing LLM decision dict used to build the plan payload.
            selected: Resolved connector list (after quick/standard filtering).
            iteration: Current outer iteration number (for event correlation).
            session_id: Optional session ID for event tagging.

        Yields:
            AgentEvent objects: orchestrator_plan, then dispatch_start.
        """
        original = decision["connectors"]
        classification = decision.get("classification", "standard")
        yield AgentEvent(
            type="orchestrator_plan",
            agent=self.agent_name,
            data={
                "classification": classification,
                "reasoning": decision.get("reasoning", ""),
                "strategy": (
                    "direct"
                    if classification == "quick"
                    else ("progressive" if classification == "standard" else "parallel")
                ),
                "planned_systems": [
                    {
                        "id": c.connector_id,
                        "name": c.connector_name,
                        "reason": c.reason,
                        "priority": c.priority,
                        "conditional": c.conditional,
                    }
                    for c in original
                ],
                "estimated_calls": sum(
                    c.max_steps or self._get_default_max_steps() for c in original
                ),
            },
            session_id=session_id,
        )
        yield AgentEvent(
            type="dispatch_start",
            agent=self.agent_name,
            data={
                "iteration": iteration,
                "connectors": [{"id": c.connector_id, "name": c.connector_name} for c in selected],
            },
            session_id=session_id,
        )
        log_structured(
            logging.INFO,
            "iteration_decision",
            f"Dispatching to {len(selected)} connectors",
            session_id=session_id,
            iteration=iteration,
            action="query",
            connectors_selected=[c.connector_id for c in selected],
            connector_names=[c.connector_name for c in selected],
            selection_reasons={c.connector_id: c.reason for c in selected},
        )

    async def _build_run_state(
        self,
        user_message: str,
        session_id: str | None,
        context: dict[str, Any] | None,
    ) -> tuple[OrchestratorState, TranscriptCollector | None, Any]:
        """Initialise OrchestratorState and optional transcript collector for a run.

        Args:
            user_message: The user's input message.
            session_id: Optional session ID for conversation tracking.
            context: Optional context dict containing history, session_state, session_mode.

        Returns:
            Tuple of (state, transcript_collector, session_state).
            transcript_collector is None when session_id is absent or collector
            creation fails. session_state is extracted from context.
        """
        conversation_history: list[dict[str, str]] | None = None
        if context and "history" in context:
            conversation_history = parse_history_text(context["history"])

        session_state = context.get("session_state") if context else None

        state = OrchestratorState(
            user_goal=user_message,
            session_id=session_id,
            conversation_history=conversation_history,
            max_iterations=self._get_max_iterations(),
            start_time_ms=time.time() * 1000,
            session_state=session_state,
        )

        investigation_config = self._get_investigation_config()
        if self.dependencies.session_type != "interactive":
            state.investigation_budget = investigation_config.get("budget_automated", 20)
        else:
            state.investigation_budget = investigation_config.get("budget_interactive", 30)
        state.convergence_window = investigation_config.get("convergence_window", 2)

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

        return state, transcript_collector, session_state

    def _classify_loop_error(self, e: Exception, state: OrchestratorState) -> dict[str, Any]:
        """Classify a loop exception for structured error event emission.

        Args:
            e: The exception that was raised in the main dispatch loop.
            state: Current orchestrator state (used to check recoverability).

        Returns:
            Dict of error classification keys for merging into the error event data.
        """
        error_classification: dict[str, Any] = {
            "error_source": "internal",
            "error_type": "unknown",
            "severity": "permanent",
            "trace_id": get_current_trace_id(),
            "recoverable": len(state.all_findings) > 0,
        }
        if isinstance(e, (ModelHTTPError, ModelAPIError)):
            error_classification["error_source"] = "llm"
            if isinstance(e, ModelHTTPError) and e.status_code == 429:
                error_classification["error_type"] = "rate_limit"
                error_classification["severity"] = "transient"
            elif isinstance(e, ModelHTTPError) and e.status_code == 401:
                error_classification["error_type"] = "auth"
                error_classification["remediation"] = "Check ANTHROPIC_API_KEY"
        elif isinstance(e, UpstreamApiError):
            error_classification["error_source"] = "connector"
            error_classification["error_type"] = "connection"
        return error_classification

    async def _stream_dispatch_round(
        self,
        state: OrchestratorState,
        connectors: list[ConnectorSelection],
        iteration: int,
        session_id: str | None,
        outputs: list[SubgraphOutput],
        *,
        follow_up_round: int | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Stream events from one parallel dispatch round and collect SubgraphOutputs.

        Wraps _dispatch_parallel, forwarding WrappedEvents as agent_event SSE items,
        extracting hypotheses from thought events, and emitting early_findings /
        connector_complete events when agents complete. Appends SubgraphOutput
        objects to the caller-supplied ``outputs`` list (mutation by reference).

        Args:
            state: Current orchestrator state.
            connectors: Connectors to dispatch to this round.
            iteration: Current outer iteration number (for event correlation).
            session_id: Optional session ID for event tagging.
            outputs: Mutable list; completed SubgraphOutput objects are appended here.
            follow_up_round: When not None, marks events as belonging to a topology
                follow-up round and includes the round number in event data.

        Yields:
            AgentEvent objects (agent_event, hypothesis_update, early_findings,
            connector_complete).
        """
        async for event_or_output in self._dispatch_parallel(state, connectors, iteration):
            if isinstance(event_or_output, WrappedEvent):
                yield AgentEvent(
                    type="agent_event",
                    agent=self.agent_name,
                    data=event_or_output.to_dict(),
                    session_id=session_id,
                )
                inner = event_or_output.inner_event
                if inner.get("type") == "thought":
                    from meho_app.modules.agents.orchestrator.hypothesis_parser import (
                        extract_hypotheses,
                    )

                    hypotheses = extract_hypotheses(inner.get("data", {}).get("content", ""))
                    for h in hypotheses:
                        yield AgentEvent(
                            type="hypothesis_update",
                            agent=self.agent_name,
                            data={
                                "hypothesis_id": h["hypothesis_id"],
                                "text": h["text"],
                                "status": h["status"],
                                "connector_id": event_or_output.agent_source.get("connector_id"),
                                "connector_name": event_or_output.agent_source.get(
                                    "connector_name"
                                ),
                            },
                            session_id=session_id,
                        )
            else:
                outputs.append(event_or_output)
                remaining_count = len(connectors) - len(outputs)
                early_findings_data: dict[str, Any] = {
                    "connector_id": event_or_output.connector_id,
                    "connector_name": event_or_output.connector_name,
                    "findings_preview": (
                        event_or_output.findings[:200] if event_or_output.findings else None
                    ),
                    "status": event_or_output.status,
                    "remaining_count": remaining_count,
                }
                connector_complete_data: dict[str, Any] = {
                    "connector_id": event_or_output.connector_id,
                    "connector_name": event_or_output.connector_name,
                    "status": event_or_output.status,
                    "findings_preview": (
                        event_or_output.findings[:200] if event_or_output.findings else None
                    ),
                    "data_refs": event_or_output.data_refs,
                }
                if follow_up_round is not None:
                    early_findings_data["follow_up_round"] = follow_up_round
                    connector_complete_data["follow_up_round"] = follow_up_round
                yield AgentEvent(
                    type="early_findings",
                    agent=self.agent_name,
                    data=early_findings_data,
                    session_id=session_id,
                )
                yield AgentEvent(
                    type="connector_complete",
                    agent=self.agent_name,
                    data=connector_complete_data,
                    session_id=session_id,
                )

    async def _stream_topology_traversal(
        self,
        state: OrchestratorState,
        outputs: list[SubgraphOutput],
        iteration: int,
        session_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        """Drive topology-based follow-up dispatch rounds within one main iteration.

        Phase 77: After the initial dispatch round, this loop inspects discovered
        entities in the outputs, looks up topology-linked connectors, and dispatches
        follow-up agents until one of the loop-local exit conditions fires:
        ``state.should_force_synthesis()`` (budget exhaustion or convergence window),
        no entities discovered in the latest outputs, no topology targets returned by
        ``route_via_topology``, the investigation budget is exhausted by the planned
        dispatch, or ``max_traversal_rounds`` is reached. Convergence within this
        helper is heuristic-based (new connectors discovered or non-empty findings
        in the follow-up outputs); the LLM-judged convergence (``_llm_has_novelty``
        from the routing decision) is applied at the outer iteration boundary in
        ``run_streaming``, not inside this helper.

        Mutates ``state.all_findings`` and ``state.dispatch_count`` in place.

        Args:
            state: Current orchestrator state (mutated: all_findings, dispatch tracking).
            outputs: SubgraphOutput list from the preceding dispatch round; used as
                the starting point for entity extraction.
            iteration: Current outer iteration number (for event correlation).
            session_id: Optional session ID for event tagging.

        Yields:
            AgentEvent objects (dispatch_start, agent_event, hypothesis_update,
            early_findings, connector_complete).
        """
        max_traversal_rounds = self._get_max_traversal_rounds()
        current_round = 1  # Round 1 was the initial dispatch above

        while current_round < max_traversal_rounds:
            if state.should_force_synthesis():
                log_structured(
                    logging.INFO,
                    "force_synthesis",
                    f"Forcing synthesis: {state.budget_exhaustion_reason} "
                    f"(dispatches: {state.dispatch_count}/{state.investigation_budget})",
                    session_id=session_id,
                )
                break

            round_entities = []
            for output in outputs:
                if output.findings:
                    parsed = parse_discovered_entities(output.findings)
                    if parsed:
                        output.entities_discovered = parsed
                        round_entities.extend(parsed)

            if not round_entities:
                log_structured(
                    logging.DEBUG,
                    "no_discovered_entities",
                    f"Round {current_round} produced no discovered entities",
                    session_id=session_id,
                )
                break

            available_connectors = await self._get_available_connectors()
            try:
                from meho_app.api.database import create_openapi_session_maker

                session_maker = create_openapi_session_maker()
                async with session_maker() as db_session:
                    follow_ups = await route_via_topology(
                        discovered_entities=round_entities,
                        tenant_id=self._get_tenant_id(),
                        available_connectors=available_connectors,
                        visited_connectors=state.visited_connectors,
                        session=db_session,
                    )
            except (
                AttributeError,
                RuntimeError,
                ValueError,
                ImportError,
                TimeoutError,
                OSError,
            ) as e:
                logger.warning(f"Topology routing failed, skipping follow-up: {e}")
                break

            if not follow_ups:
                log_structured(
                    logging.INFO,
                    "no_topology_targets",
                    f"Round {current_round}: No topology targets for {len(round_entities)} entities",
                    session_id=session_id,
                )
                break

            if state.dispatch_count + len(follow_ups) > state.investigation_budget:
                remaining = state.remaining_budget()
                if remaining <= 0:
                    break
                follow_ups = follow_ups[:remaining]

            current_round += 1

            inv_context = build_topology_investigation_context(
                state.all_findings, round_entities, current_round, state.visited_connectors
            )
            log_structured(
                logging.INFO,
                "topology_traversal_dispatch",
                f"Round {current_round}: Dispatching to {len(follow_ups)} connectors via topology "
                f"(budget: {state.dispatch_count + len(follow_ups)}/{state.investigation_budget})",
                session_id=session_id,
                round=current_round,
                entity_count=len(round_entities),
                connectors=[c.connector_name for c in follow_ups],
            )
            for fu in follow_ups:
                fu._investigation_context = inv_context

            yield AgentEvent(
                type="dispatch_start",
                agent=self.agent_name,
                data={
                    "iteration": iteration,
                    "round": current_round,
                    "connectors": [
                        {"id": c.connector_id, "name": c.connector_name} for c in follow_ups
                    ],
                    "follow_up": True,
                    "entity_count": len(round_entities),
                    "budget_used": state.dispatch_count,
                    "budget_total": state.investigation_budget,
                },
                session_id=session_id,
            )

            followup_outputs: list[SubgraphOutput] = []
            async for event in self._stream_dispatch_round(
                state,
                follow_ups,
                iteration,
                session_id,
                followup_outputs,
                follow_up_round=current_round,
            ):
                yield event

            state.record_dispatches([c.connector_id for c in follow_ups])

            new_connectors_found = any(
                e.get("connector_id") and e["connector_id"] not in state.visited_connectors
                for e in round_entities
            )
            followup_had_findings = any(
                fo.status in ("success", "partial") and fo.findings and fo.findings.strip()
                for fo in followup_outputs
            )
            state.record_novelty(new_connectors_found or followup_had_findings)

            state.all_findings.extend(followup_outputs)
            outputs = followup_outputs

            log_structured(
                logging.INFO,
                "topology_traversal_complete",
                f"Round {current_round} complete",
                session_id=session_id,
                round=current_round,
                findings_count=len(followup_outputs),
                total_findings=len(state.all_findings),
                budget_used=state.dispatch_count,
                budget_total=state.investigation_budget,
            )

    async def _stream_completion(
        self,
        state: OrchestratorState,
        transcript_collector: TranscriptCollector | None,
        is_partial: bool,
        loop_error: Exception | None,
        session_id: str | None,
        *,
        session_state: Any = None,
    ) -> AsyncIterator[AgentEvent]:
        """Synthesize findings and emit usage_summary + orchestrator_complete events.

        Called from both the normal (else) path and the error-recovery path.
        When ``session_state`` is provided (normal path only), operation context is
        extracted for multi-turn awareness before synthesis begins.

        Args:
            state: Current orchestrator state with accumulated findings.
            transcript_collector: Optional transcript collector for usage stats.
            is_partial: Whether the run completed in degraded/partial mode.
            loop_error: Exception that interrupted the loop, or None on clean exit.
            session_id: Optional session ID for event tagging.
            session_state: OrchestratorSessionState from adapter; when not None,
                triggers operation-context extraction (normal path only).

        Yields:
            AgentEvent objects (synthesis_start, answer_chunk, final_answer,
            error, usage_summary, orchestrator_complete).
        """
        if state.all_findings or not loop_error:
            yield AgentEvent(
                type="synthesis_start",
                agent=self.agent_name,
                data={"partial": is_partial},
                session_id=session_id,
            )

            if session_state is not None and state.all_findings:
                try:
                    operation, entities = await self._extract_operation_context(
                        user_message=state.user_goal,
                        findings=state.all_findings,
                    )
                    if operation:
                        session_state.set_operation_context(operation, entities)
                        log_structured(
                            logging.DEBUG,
                            "context_extracted",
                            "Extracted operation context",
                            session_id=session_id,
                            operation=operation[:50] if operation else None,
                            entity_count=len(entities),
                        )
                except (AttributeError, ValueError, TypeError) as ctx_err:
                    logger.warning(f"Failed to extract operation context: {ctx_err}")

            try:
                async for chunk_event in self._synthesize_streaming(state, session_id):
                    yield chunk_event

                yield AgentEvent(
                    type="final_answer",
                    agent=self.agent_name,
                    data={
                        "content": state.final_answer,
                        "iterations": state.current_iteration,
                        "connectors_queried": [f.connector_name for f in state.all_findings],
                        "total_time_ms": time.time() * 1000 - state.start_time_ms,
                        "partial": is_partial,
                        "error": str(loop_error) if loop_error else None,
                    },
                    session_id=session_id,
                )

            except Exception as synth_err:
                error_msg = f"Synthesis error: {synth_err}"
                logger.exception(error_msg)
                yield AgentEvent(
                    type="error",
                    agent=self.agent_name,
                    data={"message": error_msg, "recoverable": False},
                    session_id=session_id,
                )

        if transcript_collector:
            try:  # noqa: SIM105 -- explicit error handling preferred
                _cr = transcript_collector._cache_read_tokens
                _cw = transcript_collector._cache_write_tokens
                _total = transcript_collector._total_tokens
                _regular = _total - _cr - _cw
                _effective = int(_regular + _cr * 0.1 + _cw * 1.25)
                yield AgentEvent(
                    type="usage_summary",
                    agent=self.agent_name,
                    data={
                        "total_tokens": _total,
                        "effective_tokens": _effective,
                        "prompt_tokens": transcript_collector._prompt_tokens,
                        "completion_tokens": transcript_collector._completion_tokens,
                        "cache_read_tokens": _cr,
                        "cache_write_tokens": _cw,
                        "estimated_cost_usd": transcript_collector._total_cost_usd,
                        "llm_calls": transcript_collector._llm_calls,
                    },
                    session_id=session_id,
                )
            except (AttributeError, TypeError, ValueError):  # usage summary is best-effort
                pass

        yield AgentEvent(
            type="orchestrator_complete",
            agent=self.agent_name,
            data={
                "success": state.final_answer is not None,
                "iterations": state.current_iteration,
                "total_time_ms": time.time() * 1000 - state.start_time_ms,
                "partial": is_partial,
            },
            session_id=session_id,
        )

        total_time_ms = time.time() * 1000 - state.start_time_ms
        log_structured(
            logging.INFO,
            "orchestrator_complete",
            "Orchestrator finished",
            session_id=session_id,
            total_time_ms=int(total_time_ms),
            iterations=state.current_iteration,
            connectors_queried=len(state.all_findings),
            partial=is_partial,
            success=state.final_answer is not None,
        )

    async def _close_run(
        self,
        state: OrchestratorState,
        transcript_collector: TranscriptCollector | None,
        root_span: Any,
        root_span_ctx: Any,
        is_partial: bool,
        session_id: str | None,
    ) -> None:
        """Close transcript collector, fire memory extraction, and end the OTEL span.

        Called unconditionally from the ``finally`` block of run_streaming so that
        observability resources are released even when the generator is cancelled.

        Args:
            state: Final orchestrator state.
            transcript_collector: Optional transcript collector to close.
            root_span: OpenTelemetry span started at the beginning of the run.
            root_span_ctx: Context manager returned by use_span(); must be exited.
            is_partial: Whether the run completed in degraded/partial mode.
            session_id: Optional session ID for log correlation.
        """
        if transcript_collector:
            try:
                if state.final_answer is not None:
                    status = "completed"
                elif is_partial:
                    status = "interrupted"
                else:
                    status = "failed"
                await transcript_collector.close(status=status)
                logger.debug(f"Transcript collector closed with status={status}")
                if status == "completed" and state.all_findings:
                    try:
                        from meho_app.core.config import get_config

                        config = get_config()
                        if config.enable_memory_extraction:
                            connector_findings = [
                                {
                                    "connector_id": str(f.connector_id),
                                    "connector_name": f.connector_name,
                                    "findings": f.findings,
                                }
                                for f in state.all_findings
                                if f.status == "success" and f.findings
                            ]
                            if connector_findings:
                                from meho_app.modules.memory.extraction import (
                                    run_memory_extraction,
                                )

                                asyncio.create_task(  # noqa: RUF006 -- fire-and-forget task pattern
                                    run_memory_extraction(
                                        connector_findings=connector_findings,
                                        tenant_id=self._get_tenant_id(),
                                        conversation_id=session_id or "",
                                        tool_calls_count=transcript_collector._tool_calls,
                                    )
                                )
                                logger.info(
                                    "memory_extraction_fired",
                                    extra={
                                        "session_id": session_id,
                                        "connector_count": len(connector_findings),
                                    },
                                )
                    except (AttributeError, RuntimeError) as extraction_err:
                        logger.warning(f"Failed to start memory extraction: {extraction_err}")
            except (AttributeError, RuntimeError) as e:
                logger.warning(f"Failed to close transcript collector: {e}")
            finally:
                set_transcript_collector(None)

        root_span.set_attribute("meho.iterations", state.current_iteration)
        root_span.set_attribute("meho.partial", is_partial)
        root_span_ctx.__exit__(None, None, None)
        root_span.end()

    # =========================================================================
    # Decision Logic
    # =========================================================================

    async def _decide_next_action(
        self,
        state: OrchestratorState,
        thinking_events: list[AgentEvent] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """LLM decides: query more connectors or respond to user.

        Uses iteration awareness to:
        - Exclude already-queried connectors
        - Bias toward "respond" on last iteration

        When ``thinking_events`` is provided, the routing LLM call is streamed
        and ``orchestrator_thinking`` events are collected into the list so the
        caller can yield them to the frontend (Phase 110 D-01).

        Args:
            state: Current orchestrator state.
            thinking_events: Optional list to collect thinking AgentEvent objects.
            session_id: Optional session ID for event correlation.

        Returns:
            Dict with "action" key ("respond" or "query") and optional "connectors" list.
        """
        connectors = await self._get_available_connectors()
        logger.info(
            f"🔌 Available connectors: {len(connectors)} - {[c['name'] for c in connectors]}"
        )

        if not connectors:
            logger.warning("No active connectors available - check connector_repo access")
            return {"action": "respond"}

        # Track already-queried connectors but don't filter them out
        # This allows re-querying for multi-part queries (e.g., "get namespaces AND pods")
        already_queried = state.get_queried_connector_ids()

        # Mark connectors as already queried for the prompt
        for c in connectors:
            c["already_queried"] = c["id"] in already_queried

        # Check if this is the last iteration - bias toward respond
        is_last_iteration = state.is_last_iteration()
        if is_last_iteration and state.has_sufficient_findings():
            logger.info("Last iteration with sufficient findings, proceeding to respond")
            return {"action": "respond"}

        # Fetch all skill summaries for prompt injection (Phase 52 existing behavior)
        skill_summaries = await self._get_skill_summaries()

        # Phase 77: Fetch investigation skill summaries separately for the dedicated template variable
        investigation_skill_summaries = await self._get_skill_summaries(skill_type="orchestrator")

        prompt = build_routing_prompt(
            state,
            connectors,
            already_queried,
            skill_summaries=skill_summaries,
            investigation_skill_summaries=investigation_skill_summaries,
        )

        # Add session context for multi-turn conversations (Phase 3)
        session_context = self._build_session_context(state.session_state)
        if session_context:
            prompt += f"\n\n## Session Context (Multi-Turn)\n\n{session_context}"
            prompt += "\n\n**Follow-up Guidance:**"
            prompt += "\n- If the user says 'show more', 'filter those', 'what about X', they likely mean the same context"
            prompt += (
                "\n- Prefer connectors used successfully in previous turns for follow-up queries"
            )
            prompt += "\n- Avoid re-querying connectors that recently failed with the same error"

        # Phase 110: Stream thinking events from routing LLM when caller requests it
        if thinking_events is not None:
            response = ""
            async for item in self._call_llm_streaming(prompt, session_id=session_id):
                if isinstance(item, AgentEvent):
                    thinking_events.append(item)
                else:
                    response = item
            # If streaming yielded no final string, fall back to non-streaming
            if not response:
                response = await self._call_llm(prompt)
        else:
            response = await self._call_llm(prompt)

        decision = parse_decision(response, connectors)

        # Handle read_skill: load full skill content for synthesis (Phase 52)
        if decision.get("read_skill"):
            skill_name = decision["read_skill"]
            skill_content = await self._read_orchestrator_skill(skill_name)
            if skill_content:
                state.orchestrator_skill_context = skill_content  # type: ignore[attr-defined]
                logger.info(f"Loaded orchestrator skill '{skill_name}' for synthesis")

        # Phase 77: Extract LLM-judged novelty for convergence detection (D-11)
        # The routing LLM assesses whether specialist findings contain new information
        # has_novelty is part of the routing response JSON format (see routing.md)
        llm_has_novelty = decision.get(
            "has_novelty", True
        )  # Default to True (assume novelty) if missing
        decision["_llm_has_novelty"] = llm_has_novelty

        logger.info(f"Decision: {decision['action']}")
        if decision["action"] == "query":
            logger.info(
                f"Selected connectors: {[c.connector_name for c in decision['connectors']]}"
            )

        return decision

    # =========================================================================
    # Parallel Dispatch
    # =========================================================================

    def _get_agent_timeout(self) -> float:
        """Get per-agent timeout from config (default 30s).

        Environment variable ORCHESTRATOR_AGENT_TIMEOUT takes precedence over config.yaml.

        Returns:
            Timeout in seconds.
        """
        # Check environment override first
        from meho_app.core.config import get_config

        env_config = get_config()
        if env_config.orchestrator_agent_timeout is not None:
            return env_config.orchestrator_agent_timeout

        # Fall back to config.yaml
        if hasattr(self._config, "raw") and "orchestrator" in self._config.raw:
            return float(self._config.raw["orchestrator"].get("agent_timeout", 30.0))
        return 30.0

    def _get_total_timeout(self) -> float:
        """Get total iteration timeout from config (default 120s).

        Environment variable ORCHESTRATOR_TOTAL_TIMEOUT takes precedence over config.yaml.

        Returns:
            Timeout in seconds.
        """
        # Check environment override first
        from meho_app.core.config import get_config

        env_config = get_config()
        if env_config.orchestrator_total_timeout is not None:
            return env_config.orchestrator_total_timeout

        # Fall back to config.yaml
        if hasattr(self._config, "raw") and "orchestrator" in self._config.raw:
            return float(self._config.raw["orchestrator"].get("total_timeout", 120.0))
        return 120.0

    def _get_max_iterations(self) -> int:
        """Get max iterations from config (default 3).

        Environment variable ORCHESTRATOR_MAX_ITERATIONS takes precedence over config.yaml.

        Returns:
            Maximum number of iterations.
        """
        # Check environment override first
        from meho_app.core.config import get_config

        env_config = get_config()
        if env_config.orchestrator_max_iterations is not None:
            return env_config.orchestrator_max_iterations

        # Fall back to config.yaml
        if hasattr(self._config, "raw") and "orchestrator" in self._config.raw:
            return int(self._config.raw["orchestrator"].get("max_iterations", 3))
        return 3

    def _get_max_dispatch_rounds(self) -> int:
        """Get max dispatch rounds from config (default 3).

        Separate from max_iterations -- rounds happen WITHIN a single iteration.
        Round 1 is the initial LLM-routed dispatch (existing).
        Rounds 2-3 are deterministic entity-driven follow-ups.

        Returns:
            Maximum number of dispatch rounds per iteration.
        """
        if hasattr(self._config, "raw") and "orchestrator" in self._config.raw:
            return int(self._config.raw["orchestrator"].get("max_dispatch_rounds", 3))
        return 3

    def _get_investigation_config(self) -> dict:
        """Get investigation config section.

        Returns:
            Investigation config dict from config.yaml, or empty dict.
        """
        if hasattr(self._config, "raw") and "orchestrator" in self._config.raw:
            result: dict[str, Any] = self._config.raw["orchestrator"].get("investigation", {})
            return result
        return {}

    def _get_max_traversal_rounds(self) -> int:
        """Get max traversal rounds from config (default 10).

        Phase 77: Controls the topology-driven traversal loop. Separate from
        max_dispatch_rounds (Phase 32 legacy) and max_iterations (main loop).

        Returns:
            Maximum number of follow-up dispatch rounds per investigation.
        """
        return int(self._get_investigation_config().get("max_traversal_rounds", 10))

    def _get_followup_max_steps(self) -> int:
        """Get max steps for follow-up round agents (default 6).

        Follow-up agents have focused goals and need fewer steps
        than Round 1 agents.

        Returns:
            Maximum ReAct steps for follow-up round agents.
        """
        if hasattr(self._config, "raw") and "orchestrator" in self._config.raw:
            return int(self._config.raw["orchestrator"].get("followup_max_steps", 6))
        return 6

    def _get_default_max_steps(self) -> int:
        """Get default specialist max_steps from specialist config (default 8).

        Phase 99: Used as fallback when routing LLM does not specify per-connector max_steps.

        Returns:
            Default max steps for specialist agents.
        """
        try:
            specialist_config = load_yaml_config(
                Path(__file__).parent.parent / "specialist_agent" / "config.yaml"
            )
            return specialist_config.max_steps
        except (FileNotFoundError, OSError, AttributeError, ValueError, yaml.YAMLError) as e:
            logger.warning(
                "Failed to load specialist config, using default max_steps=8",
                exc_info=True,
                exception_type=type(e).__name__,
            )
            return 8

    async def _dispatch_parallel(
        self,
        state: OrchestratorState,
        connectors: list[ConnectorSelection],
        iteration: int,
    ) -> AsyncIterator[WrappedEvent | SubgraphOutput]:
        """Dispatch to all connectors in parallel, streaming events as they arrive.

        Args:
            state: Current orchestrator state.
            connectors: List of selected connectors to query.
            iteration: Current iteration number.

        Yields:
            WrappedEvent for streaming events, SubgraphOutput when agent completes.
        """
        span, span_ctx = self._start_dispatch_span(connectors, iteration)
        tasks: dict[str, asyncio.Task[None]] | None = None
        try:
            queues, tasks = self._spawn_agent_tasks(state, connectors, iteration)
            async for item in self._drain_dispatch_queues(
                queues, tasks, connectors, self._get_total_timeout()
            ):
                yield item
        finally:
            if tasks is not None:
                for task in tasks.values():
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks.values(), return_exceptions=True)
            self._finalize_dispatch_span(span, span_ctx)

    # _dispatch_parallel helpers

    def _start_dispatch_span(
        self,
        connectors: list[ConnectorSelection],
        iteration: int,
    ) -> tuple[Span, AbstractContextManager[Span]]:
        """Open and enter the OTEL dispatch span; return (span, ctx) for later close."""
        span = tracer.start_span(
            "meho.orchestrator.dispatch",
            attributes={
                "meho.connector_count": len(connectors),
                "meho.iteration": iteration,
            },
        )
        ctx = use_span(span, end_on_exit=False)
        ctx.__enter__()
        return span, ctx

    def _spawn_agent_tasks(
        self,
        state: OrchestratorState,
        connectors: list[ConnectorSelection],
        iteration: int,
    ) -> tuple[
        dict[str, asyncio.Queue[WrappedEvent | SubgraphOutput]],
        dict[str, asyncio.Task[None]],
    ]:
        """Create per-connector queues and asyncio tasks with context propagation."""
        agent_timeout = self._get_agent_timeout()

        async def run_with_timeout(
            conn: ConnectorSelection,
            queue: asyncio.Queue[WrappedEvent | SubgraphOutput],
        ) -> None:
            try:
                await asyncio.wait_for(
                    self._run_single_agent(state, conn, iteration, queue),
                    timeout=agent_timeout,
                )
            except TimeoutError:
                logger.warning(f"Agent for {conn.connector_name} exceeded {agent_timeout}s timeout")
                await queue.put(
                    SubgraphOutput(
                        connector_id=conn.connector_id,
                        connector_name=conn.connector_name,
                        findings="",
                        status="timeout",
                        error_message=f"Agent exceeded {agent_timeout}s timeout",
                    )
                )
            except asyncio.CancelledError:
                logger.info(f"Agent for {conn.connector_name} was cancelled")
                await queue.put(
                    SubgraphOutput(
                        connector_id=conn.connector_id,
                        connector_name=conn.connector_name,
                        findings="",
                        status="cancelled",
                        error_message="Request was cancelled",
                    )
                )
            except Exception as e:
                logger.exception(f"Agent for {conn.connector_name} failed with error")
                await queue.put(
                    SubgraphOutput(
                        connector_id=conn.connector_id,
                        connector_name=conn.connector_name,
                        findings="",
                        status="failed",
                        error_message=str(e),
                    )
                )

        queues: dict[str, asyncio.Queue[WrappedEvent | SubgraphOutput]] = {}
        tasks: dict[str, asyncio.Task[None]] = {}
        try:
            for conn in connectors:
                queue: asyncio.Queue[WrappedEvent | SubgraphOutput] = asyncio.Queue()
                queues[conn.connector_id] = queue
                # Fresh copy_context() per task so each task gets its own isolated
                # ContextVar snapshot (transcript collector, OTEL span context).
                # Sharing one ctx across all tasks causes OTEL span mutations in one
                # task to leak to siblings, breaking the dispatch → agent span tree.
                tasks[conn.connector_id] = asyncio.create_task(
                    run_with_timeout(conn, queue), context=contextvars.copy_context()
                )
        except Exception:
            for task in tasks.values():
                task.cancel()
            raise
        return queues, tasks

    async def _drain_dispatch_queues(
        self,
        queues: dict[str, asyncio.Queue[WrappedEvent | SubgraphOutput]],
        tasks: dict[str, asyncio.Task[None]],
        connectors: list[ConnectorSelection],
        total_timeout: float,
    ) -> AsyncIterator[WrappedEvent | SubgraphOutput]:
        """Poll all connector queues until every connector has produced a SubgraphOutput."""
        pending_outputs: dict[str, SubgraphOutput | None] = {
            c.connector_id: None for c in connectors
        }
        completed = 0
        try:
            async with asyncio.timeout(total_timeout):
                while completed < len(connectors):
                    for conn_id, queue in queues.items():
                        try:
                            item = queue.get_nowait()
                            if isinstance(item, SubgraphOutput):
                                pending_outputs[conn_id] = item
                                completed += 1
                            yield item
                        except asyncio.QueueEmpty:
                            pass
                    await asyncio.sleep(0.01)
        except TimeoutError:
            logger.warning(
                f"Total timeout ({total_timeout}s) exceeded, cancelling remaining agents"
            )
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            for conn_id, output in pending_outputs.items():
                if output is None:
                    conn_name = next(
                        (c.connector_name for c in connectors if c.connector_id == conn_id),
                        conn_id,
                    )
                    yield SubgraphOutput(
                        connector_id=conn_id,
                        connector_name=conn_name,
                        findings="",
                        status="timeout",
                        error_message="Total timeout exceeded",
                    )

    def _finalize_dispatch_span(self, span: Span, ctx: AbstractContextManager[Span]) -> None:
        """Exit and end the OTEL dispatch span opened by _start_dispatch_span."""
        ctx.__exit__(None, None, None)
        span.end()

    async def _run_single_agent(
        self,
        state: OrchestratorState,
        connector: ConnectorSelection,
        iteration: int,
        output_queue: asyncio.Queue[WrappedEvent | SubgraphOutput],
    ) -> None:
        """Run one specialist agent for a connector and stream its outputs.

        Args:
            state: Current orchestrator state used to derive the agent prompt,
                execution context, and session-scoped metadata.
            connector: Connector selected for this specialist run.
            iteration: Current dispatch-loop iteration number. Used to tag
                emitted events and spans so parallel work can be correlated in
                logs, traces, and downstream orchestration state.
            output_queue: Async queue used to publish wrapped streaming events
                from the specialist agent and, once execution completes, the
                final `SubgraphOutput` for this connector.
        """
        wrapper = EventWrapper(
            connector_id=connector.connector_id,
            connector_name=connector.connector_name,
            iteration=iteration,
        )
        start_time = time.time()
        span, span_ctx = self._start_agent_span(connector, iteration)
        output: SubgraphOutput | None = None
        _exc: BaseException | None = None
        try:
            agent, agent_prompt = await self._create_specialist_agent(state, connector, iteration)
            agent_context = self._build_agent_context(state, connector, iteration)
            findings, data_refs = await self._stream_agent_events(
                agent, agent_prompt, agent_context, state.session_id, wrapper, output_queue
            )
            execution_time_ms = (time.time() - start_time) * 1000
            output = SubgraphOutput(
                connector_id=connector.connector_id,
                connector_name=connector.connector_name,
                findings=findings,
                status="success",
                execution_time_ms=execution_time_ms,
                data_refs=data_refs,
            )
            await output_queue.put(output)
        except Exception as exc:
            _exc = exc
            output = await self._handle_agent_failure(exc, start_time, connector, output_queue)
        finally:
            self._finalize_agent_span(span, span_ctx, output, _exc)

    # _run_single_agent helpers

    def _start_agent_span(
        self,
        connector: ConnectorSelection,
        iteration: int,
    ) -> tuple[Span, AbstractContextManager[Span]]:
        """Open and enter a per-agent OTEL child span; return (span, ctx) for later close."""
        span = tracer.start_span(
            "meho.orchestrator.agent",
            attributes={
                "meho.connector_id": connector.connector_id,
                "meho.connector_name": connector.connector_name,
                "meho.iteration": iteration,
            },
        )
        ctx = use_span(span, end_on_exit=False)
        ctx.__enter__()
        return span, ctx

    async def _create_specialist_agent(
        self,
        state: OrchestratorState,
        connector: ConnectorSelection,
        iteration: int,
    ) -> tuple[Any, str]:
        """Resolve DB connector skill, instantiate the specialist agent, and build its prompt."""
        from meho_app.modules.agents.factory import create_agent

        db_skill_content = None
        if connector.connector_type and connector.connector_type != "generic":
            try:
                from meho_app.api.database import create_openapi_session_maker
                from meho_app.modules.orchestrator_skills.service import (
                    OrchestratorSkillService,
                )

                session_maker = create_openapi_session_maker()
                async with session_maker() as db_session:
                    skill_svc = OrchestratorSkillService(db_session)
                    db_skill = await skill_svc.get_connector_skill(
                        self._get_tenant_id(), connector.connector_type
                    )
                    if db_skill:
                        db_skill_content = str(db_skill.content)
            except (AttributeError, RuntimeError, ImportError, TimeoutError, OSError) as e:
                logger.warning(
                    f"DB connector skill resolution failed for {connector.connector_type}",
                    exc_info=True,
                    exception_type=type(e).__name__,
                )

        agent = create_agent(
            dependencies=self.dependencies,
            connector_id=connector.connector_id,
            connector_name=connector.connector_name,
            connector_type=connector.connector_type,
            routing_description=connector.routing_description,
            skill_name=getattr(connector, "skill_name", None),
            iteration=iteration,
            prior_findings=[f.findings for f in state.all_findings if f.findings],
            generated_skill=getattr(connector, "generated_skill", None),
            custom_skill=getattr(connector, "custom_skill", None),
            db_connector_skill=db_skill_content,
        )
        logger.info(f"Using {agent.agent_name} agent for connector {connector.connector_name}")

        inv_ctx = getattr(connector, "_investigation_context", None)
        agent_prompt = build_agent_prompt(state, connector, investigation_context=inv_ctx)
        return agent, agent_prompt

    def _build_agent_context(
        self,
        state: OrchestratorState,
        connector: ConnectorSelection,
        iteration: int,
    ) -> dict[str, Any]:
        """Build the context dict passed to the specialist agent's run_streaming."""
        ctx: dict[str, Any] = {
            "iteration": iteration,
            "prior_findings": [f.findings for f in state.all_findings],
            "session_state": state.session_state,
        }
        if connector.max_steps is not None:
            ctx["max_steps_override"] = connector.max_steps
        return ctx

    async def _stream_agent_events(
        self,
        agent: Any,
        agent_prompt: str,
        agent_context: dict[str, Any],
        session_id: str | None,
        wrapper: EventWrapper,
        output_queue: asyncio.Queue[WrappedEvent | SubgraphOutput],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Drive the specialist's run_streaming, forward events to queue, collect data refs."""
        findings = ""
        async for event in agent.run_streaming(
            user_message=agent_prompt,
            session_id=session_id,
            context=agent_context,
        ):
            await output_queue.put(wrapper.wrap(event))
            if event.type == "final_answer":
                findings = event.data.get("content", "")

        data_refs: list[dict[str, Any]] = []
        if session_id:
            try:
                executor = get_unified_executor()
                table_infos = await executor.get_session_table_info_async(session_id)
                data_refs = [
                    {
                        "table": info["table"],
                        "session_id": session_id,
                        "row_count": info["row_count"],
                    }
                    for info in table_infos
                ]
                if data_refs:
                    logger.info(
                        f"Populated {len(data_refs)} data_refs for "
                        f"{wrapper.connector_name}: {[r['table'] for r in data_refs]}"
                    )
            except (AttributeError, KeyError, RuntimeError) as e:
                logger.warning(
                    f"Failed to query cached tables for data_refs "
                    f"(session={session_id[:8]}...): {e}"
                )
        return findings, data_refs

    async def _handle_agent_failure(
        self,
        exc: Exception,
        start_time: float,
        connector: ConnectorSelection,
        output_queue: asyncio.Queue[WrappedEvent | SubgraphOutput],
    ) -> SubgraphOutput:
        """Classify exc, build a failed SubgraphOutput, push it to the queue, and return it."""
        execution_time_ms = (time.time() - start_time) * 1000
        if isinstance(exc, TimeoutError):
            error_msg = (
                f"{connector.connector_name} timed out after {int(execution_time_ms / 1000)}s"
            )
            remediation = f"Check {connector.connector_name}"
            if connector.base_url:
                remediation += f" at {connector.base_url}"
            output = SubgraphOutput(
                connector_id=connector.connector_id,
                connector_name=connector.connector_name,
                findings="",
                status="timeout",
                error_message=error_msg,
                execution_time_ms=execution_time_ms,
                error_classification={
                    "error_source": "connector",
                    "error_type": "timeout",
                    "severity": "transient",
                    "connector_name": connector.connector_name,
                    "remediation": remediation,
                    "trace_id": get_current_trace_id(),
                },
            )
        elif isinstance(exc, UpstreamApiError):
            if exc.status_code == 401:
                error_msg = f"{connector.connector_name} authentication failed"
                remediation = (
                    f"Verify credentials for {connector.connector_name} in connector settings"
                )
                error_type = "auth"
                severity = "permanent"
            elif exc.status_code >= 500:
                error_msg = f"{connector.connector_name} returned server error ({exc.status_code})"
                remediation = (
                    f"{connector.base_url or connector.connector_name} may be experiencing issues"
                )
                error_type = "connection"
                severity = "transient"
            elif exc.status_code == 429:
                error_msg = f"{connector.connector_name} rate limited"
                remediation = (
                    f"Too many requests to {connector.connector_name} -- will retry on next query"
                )
                error_type = "rate_limit"
                severity = "transient"
            else:
                error_msg = f"{connector.connector_name} returned error {exc.status_code}"
                remediation = f"Check {connector.connector_name} API at {connector.base_url or 'configured endpoint'}"
                error_type = "data_error"
                severity = "permanent" if exc.status_code < 500 else "transient"
            logger.warning(f"Connector error for {connector.connector_name}: {error_msg}")
            output = SubgraphOutput(
                connector_id=connector.connector_id,
                connector_name=connector.connector_name,
                findings="",
                status="failed",
                error_message=error_msg,
                execution_time_ms=execution_time_ms,
                error_classification={
                    "error_source": "connector",
                    "error_type": error_type,
                    "severity": severity,
                    "connector_name": connector.connector_name,
                    "remediation": remediation,
                    "trace_id": get_current_trace_id(),
                },
            )
        else:
            logger.exception(f"Error running agent for {connector.connector_name}")
            output = SubgraphOutput(
                connector_id=connector.connector_id,
                connector_name=connector.connector_name,
                findings="",
                status="failed",
                error_message=f"{connector.connector_name} encountered an unexpected error: {type(exc).__name__}",
                execution_time_ms=execution_time_ms,
                error_classification={
                    "error_source": "internal",
                    "error_type": "unknown",
                    "severity": "permanent",
                    "connector_name": connector.connector_name,
                    "remediation": "Contact support with trace ID",
                    "trace_id": get_current_trace_id(),
                },
            )
        await output_queue.put(output)
        return output

    def _finalize_agent_span(
        self,
        span: Span,
        span_ctx: AbstractContextManager[Span],
        output: SubgraphOutput | None,
        exc: BaseException | None = None,
    ) -> None:
        """Record output attributes on the agent span, exit context, and end it."""
        if output is not None:
            span.set_attribute("meho.agent.status", output.status)
            if output.execution_time_ms is not None:
                span.set_attribute("meho.agent.execution_time_ms", output.execution_time_ms)
            if exc is not None and output.status != "success":
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, output.error_message or str(exc)))
        span_ctx.__exit__(None, None, None)
        span.end()

    # =========================================================================
    # Synthesis
    # =========================================================================

    async def _synthesize_streaming(
        self, state: OrchestratorState, session_id: str | None = None
    ) -> AsyncIterator[AgentEvent]:
        """Synthesize findings into final answer, yielding chunks as they stream.

        Supports single-connector passthrough (ARCH-03): when exactly one
        connector succeeds and none failed, the response is returned directly
        without calling the synthesis LLM.

        Yields:
            AgentEvent objects of type 'synthesis_chunk' as text arrives.
            The caller is responsible for emitting the final 'final_answer' event
            after this generator completes (state.final_answer will be set).
        """
        span, span_ctx = self._start_synthesis_span(state, session_id)
        full_text: str | None = None  # None == "no value to set" (e.g. LLMError raised)
        try:
            passthrough_result = self._try_single_connector_passthrough(state, session_id)
            if passthrough_result is not None:
                passthrough_event, passthrough_text = passthrough_result
                yield passthrough_event
                full_text = passthrough_text
                return
            prompt = await self._build_synthesis_prompt(state)
            text_out: list[str] = []
            async for event in self._stream_synthesis_chunks(state, prompt, session_id, text_out):
                yield event
            # Reached only on streaming success / fallback success -- assign even if empty
            full_text = text_out[0] if text_out else ""
        finally:
            self._finalize_synthesis(state, span, span_ctx, full_text)

    # _synthesize_streaming helpers

    def _start_synthesis_span(
        self, state: OrchestratorState, session_id: str | None
    ) -> tuple[Span, AbstractContextManager[Span]]:
        """Open and enter the OTEL synthesis child span; return (span, ctx) for later close."""
        span = tracer.start_span(
            "meho.orchestrator.synthesis",
            attributes={
                "meho.session_id": session_id or "",
                "meho.findings_count": len(state.all_findings),
                "meho.partial": state.final_answer is None and bool(state.all_findings),
            },
        )
        ctx = use_span(span, end_on_exit=False)
        ctx.__enter__()
        return span, ctx

    def _try_single_connector_passthrough(
        self, state: OrchestratorState, session_id: str | None
    ) -> tuple[AgentEvent, str] | None:
        """Detect ARCH-03 single-connector fast path.

        When exactly one connector succeeded and none failed, returns a
        (event, findings_text) tuple with the pre-built synthesis_chunk event
        and the verbatim findings text.  Returns None when the condition is not
        met and LLM synthesis should proceed.  Does not mutate state.
        """
        successful = [f for f in state.all_findings if f.status == "success"]
        failed = [f for f in state.all_findings if f.status != "success"]
        if not (len(successful) == 1 and not failed):
            return None
        finding = successful[0]
        logger.info(
            f"Single-connector passthrough: {finding.connector_name} (skipping synthesis LLM)"
        )
        return AgentEvent(
            type="synthesis_chunk",
            agent=self.agent_name,
            data={
                "content": finding.findings,
                "accumulated_length": len(finding.findings),
                "passthrough": True,
                "source_connector": finding.connector_name,
                "source_connector_id": finding.connector_id,
            },
            session_id=session_id,
        ), finding.findings

    async def _build_synthesis_prompt(self, state: OrchestratorState) -> str:
        """Compose the synthesis prompt, fetching memory summaries and multi-turn context."""
        if not state.all_findings:
            return build_conversational_prompt(state.user_goal)

        memory_summary = ""
        try:
            from meho_app.modules.agents.orchestrator.synthesis import (
                build_memory_summaries_for_synthesis,
            )

            memory_summary = await build_memory_summaries_for_synthesis(
                state, tenant_id=self._get_tenant_id()
            )
        except (AttributeError, RuntimeError, ImportError) as e:
            logger.warning(f"Failed to fetch memory summaries for synthesis: {e}")

        skill_context = ""
        if hasattr(state, "orchestrator_skill_context") and state.orchestrator_skill_context:
            skill_context = state.orchestrator_skill_context

        prompt = build_synthesis_prompt(
            state, memory_summary=memory_summary, skill_context=skill_context
        )
        if state.session_state:
            turn_count = getattr(state.session_state, "turn_count", 0)
            if turn_count > 0:
                session_context = self._build_session_context(state.session_state)
                prompt += build_multi_turn_synthesis_context(
                    state.session_state, turn_count, session_context
                )
        return prompt

    async def _stream_synthesis_chunks(
        self,
        state: OrchestratorState,
        prompt: str,
        session_id: str | None,
        text_out: list[str],
    ) -> AsyncIterator[AgentEvent]:
        """Stream synthesis chunks from the LLM with retry; fall back to _call_llm on failure.

        Runs a pydantic_ai Agent with up to 4 attempts (exponential back-off) for
        transient HTTP errors.  If the stream raises an unexpected exception,
        falls back to a blocking _call_llm call.  In all success cases, appends
        the accumulated final text to text_out so the caller can set
        state.final_answer after the generator completes.

        Yields:
            synthesis_chunk events (deltas during streaming), then
            follow_up_suggestions and citation_map events if the structured
            parser finds them in the completed text.
        """
        import random as _random
        import time as _time

        from pydantic_ai import Agent, InstrumentationSettings

        from meho_app.core.config import get_config as _get_config
        from meho_app.modules.agents.agent_factories import get_model_settings

        model_name = _get_config().llm_model
        if hasattr(self._config, "model") and hasattr(self._config.model, "name"):
            model_name = self._config.model.name
        model_settings = get_model_settings(task_type="synthesis")
        agent = Agent(
            model=model_name,
            model_settings=model_settings,
            instrument=InstrumentationSettings(),
        )

        accumulated_text = ""
        start_time = _time.perf_counter()
        _stream_ref = None

        try:
            for _attempt in range(4):  # 1 initial + 3 retries
                try:
                    async with agent.run_stream(prompt) as stream:
                        _stream_ref = stream
                        async for chunk in stream.stream_text(delta=True):
                            accumulated_text += chunk
                            yield AgentEvent(
                                type="synthesis_chunk",
                                agent=self.agent_name,
                                data={
                                    "content": chunk,
                                    "accumulated_length": len(accumulated_text),
                                },
                                session_id=session_id,
                            )
                    break  # Success -- exit retry loop
                except ModelHTTPError as e:
                    if e.status_code in (429, 500, 502, 503, 529) and _attempt < 3:
                        _delay = 1.0 * (2**_attempt) * (0.8 + _random.random() * 0.4)  # noqa: S311 -- non-cryptographic context, random OK
                        logger.warning(
                            f"Synthesis LLM error (HTTP {e.status_code}), retrying in {_delay:.1f}s"
                        )
                        await asyncio.sleep(_delay)
                        continue
                    if accumulated_text:
                        accumulated_text += (
                            "\n\n---\n*Response interrupted -- AI service error. "
                            "The analysis above may be incomplete.*"
                        )
                        break
                    raise LLMError(
                        "rate_limit" if e.status_code == 429 else "connection",
                        "transient",
                        f"Claude API error during synthesis (HTTP {e.status_code})",
                        remediation="Try again shortly",
                    ) from e
                except ModelAPIError as e:
                    if _attempt < 3:
                        _delay = 1.0 * (2**_attempt) * (0.8 + _random.random() * 0.4)  # noqa: S311 -- non-cryptographic context, random OK
                        logger.warning(f"Synthesis LLM connection error, retrying in {_delay:.1f}s")
                        await asyncio.sleep(_delay)
                        continue
                    if accumulated_text:
                        accumulated_text += (
                            "\n\n---\n*Response interrupted -- cannot reach AI service. "
                            "The analysis above may be incomplete.*"
                        )
                        break
                    raise LLMError(
                        "connection",
                        "transient",
                        "Cannot reach Claude API for synthesis",
                        remediation="Check network connectivity",
                    ) from e

            duration_ms = (_time.perf_counter() - start_time) * 1000
            async for event in self._emit_structured_synthesis_events(
                accumulated_text, state.all_findings, session_id
            ):
                yield event
            await self._log_synthesis_usage(
                model_name, prompt, accumulated_text, duration_ms, _stream_ref
            )

        except LLMError:
            raise
        except Exception as e:
            logger.warning(f"Streaming synthesis failed, falling back to blocking: {e}")
            accumulated_text = await self._call_llm(prompt)
            async for event in self._emit_structured_synthesis_events(
                accumulated_text, state.all_findings, session_id
            ):
                yield event

        text_out.append(accumulated_text)

    async def _emit_structured_synthesis_events(
        self,
        text: str,
        findings: list[SubgraphOutput],
        session_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        """Parse structured synthesis output and yield follow-up and citation events.

        Best-effort: any parse failure is logged and swallowed so that the
        structured-events step never prevents the final answer from being set.
        """
        try:
            from meho_app.modules.agents.orchestrator.synthesis_parser import (
                build_citation_map,
                parse_synthesis_sections,
            )

            sections = parse_synthesis_sections(text)
            if sections:
                if sections["follow_ups"]:
                    yield AgentEvent(
                        type="follow_up_suggestions",
                        agent=self.agent_name,
                        data={"suggestions": sections["follow_ups"]},
                        session_id=session_id,
                    )
                citation_data = build_citation_map(text, findings)
                if citation_data:
                    yield AgentEvent(
                        type="citation_map",
                        agent=self.agent_name,
                        data={"citations": citation_data},
                        session_id=session_id,
                    )
        except (ValueError, AttributeError, KeyError) as e:
            logger.warning(f"Failed to parse structured synthesis: {e}")

    async def _log_synthesis_usage(
        self,
        model_name: str,
        prompt: str,
        text: str,
        duration_ms: float,
        stream_ref: Any,
    ) -> None:
        """Log LLM token usage to the transcript collector (best-effort)."""
        with contextlib.suppress(Exception):
            from datetime import datetime
            from uuid import uuid4

            from meho_app.modules.agents.base.detailed_events import (
                DetailedEvent,
                EventDetails,
                TokenUsage,
                estimate_cost,
            )
            from meho_app.modules.agents.persistence.event_context import (
                get_transcript_collector,
            )

            collector = get_transcript_collector()
            if collector and stream_ref:
                usage = stream_ref.usage()
                prompt_tokens = getattr(usage, "request_tokens", 0) or 0
                completion_tokens = getattr(usage, "response_tokens", 0) or 0
                total_tokens = prompt_tokens + completion_tokens
                cost = estimate_cost(model_name, prompt_tokens, completion_tokens)
                event = DetailedEvent(
                    id=str(uuid4()),
                    timestamp=datetime.now(tz=UTC),
                    type="llm_call",
                    summary=f"Orchestrator streaming synthesis ({model_name})",
                    details=EventDetails(
                        llm_prompt=prompt,
                        llm_response=text,
                        token_usage=TokenUsage(
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            total_tokens=total_tokens,
                            estimated_cost_usd=cost,
                        ),
                        llm_duration_ms=duration_ms,
                        model=model_name,
                    ),
                )
                await collector.add(event)

    def _finalize_synthesis(
        self,
        state: OrchestratorState,
        span: Span,
        span_ctx: AbstractContextManager[Span],
        full_text: str | None,
    ) -> None:
        """Set state.final_answer when synthesis produced a value; close the OTEL synthesis span.

        ``full_text`` uses an Optional[str] sentinel: ``None`` means synthesis raised before
        producing any value (e.g. LLMError after retries) and ``state.final_answer`` must be
        left as-is so the downstream ``success: state.final_answer is not None`` flag stays
        ``False``.  An empty string ``""`` is a *valid* synthesis output (LLM returned no text)
        and is assigned through.
        """
        if full_text is not None:
            state.final_answer = full_text
        span_ctx.__exit__(None, None, None)
        span.end()

    async def _synthesize(self, state: OrchestratorState) -> str:
        """Synthesize findings into final answer.

        Args:
            state: Current orchestrator state with all findings.

        Returns:
            Synthesized final answer string.
        """
        if not state.all_findings:
            # No connectors were queried - handle as conversational/general knowledge
            return await self._generate_conversational_response(state)

        # Fetch memory summaries for synthesis (Phase 11)
        synthesis_memory_summary = ""
        try:
            from meho_app.modules.agents.orchestrator.synthesis import (
                build_memory_summaries_for_synthesis,
            )

            synthesis_memory_summary = await build_memory_summaries_for_synthesis(
                state, tenant_id=self._get_tenant_id()
            )
        except (AttributeError, RuntimeError, ImportError) as e:
            logger.warning(f"Failed to fetch memory summaries for synthesis: {e}")
        prompt = build_synthesis_prompt(state, memory_summary=synthesis_memory_summary)

        # Add multi-turn context for follow-up queries (Phase 3)
        if state.session_state:
            turn_count = getattr(state.session_state, "turn_count", 0)
            if turn_count > 0:
                session_context = self._build_session_context(state.session_state)
                prompt += build_multi_turn_synthesis_context(
                    state.session_state, turn_count, session_context
                )

        return await self._call_llm(prompt)

    async def _generate_conversational_response(self, state: OrchestratorState) -> str:
        """Generate a natural response for conversational or general knowledge queries.

        Called when no connectors were needed to answer the user's question.

        Args:
            state: Current orchestrator state.

        Returns:
            Natural conversational response.
        """
        prompt = build_conversational_prompt(state.user_goal)
        return await self._call_llm(prompt)

    # =========================================================================
    # Operation Context Extraction (Phase 5 - TASK-185)
    # =========================================================================

    async def _extract_operation_context(
        self,
        user_message: str,
        findings: list[SubgraphOutput],
    ) -> tuple[str | None, list[str]]:
        """Extract operation context from the current turn.

        Uses LLM to identify what the user is investigating and
        key entities mentioned for context-aware follow-ups.

        This enables smarter handling of ambiguous queries like
        "show me more" or "filter those" by understanding what
        the user was previously investigating.

        Args:
            user_message: The user's input message.
            findings: List of SubgraphOutput from connector queries.

        Returns:
            Tuple of (operation description, list of entities).
            Returns (None, []) if extraction fails.
        """
        try:
            # Load prompt template
            template_path = Path(__file__).parent / "prompts" / "context_extraction.md"
            template = template_path.read_text()

            # Build findings summary (truncated to avoid token limits)
            findings_text = self._format_findings_for_context(findings)

            # Format prompt
            prompt = template.format(
                user_message=user_message,
                findings=findings_text,
            )

            # Call LLM
            response = await self._call_llm(prompt)

            # Parse JSON response
            return self._parse_context_extraction(response)

        except (LLMError, InternalError, ValueError, AttributeError, KeyError) as e:
            logger.warning(f"Failed to extract operation context: {e}")
            return None, []

    def _format_findings_for_context(
        self,
        findings: list[SubgraphOutput],
        max_chars: int = 1000,
    ) -> str:
        """Format findings for context extraction prompt.

        Truncates long findings to stay within token limits.

        Args:
            findings: List of SubgraphOutput from connector queries.
            max_chars: Maximum total characters for findings text.

        Returns:
            Formatted findings string.
        """
        if not findings:
            return "No findings from connectors."

        parts = []
        remaining_chars = max_chars

        for f in findings:
            if f.status != "success" or not f.findings:
                continue

            # Truncate individual finding
            finding_text = f.findings
            if len(finding_text) > 300:
                finding_text = finding_text[:300] + "..."

            entry = f"[{f.connector_name}]: {finding_text}"

            if len(entry) > remaining_chars:
                if remaining_chars > 50:
                    parts.append(entry[:remaining_chars] + "...")
                break

            parts.append(entry)
            remaining_chars -= len(entry) + 1  # +1 for newline

        return "\n".join(parts) if parts else "No successful findings."

    def _parse_context_extraction(
        self,
        response: str,
    ) -> tuple[str | None, list[str]]:
        """Parse LLM response for context extraction.

        Handles JSON parsing with graceful degradation.

        Args:
            response: Raw LLM response text.

        Returns:
            Tuple of (operation, entities) or (None, []) on failure.
        """
        try:
            # Try to extract JSON from response
            start_idx = response.find("{")
            end_idx = response.rfind("}") + 1

            if start_idx < 0 or end_idx <= start_idx:
                logger.debug("No JSON found in context extraction response")
                return None, []

            json_str = response[start_idx:end_idx]
            data = json.loads(json_str)

            operation = data.get("operation")
            entities = data.get("entities", [])

            # Validate types
            if operation and not isinstance(operation, str):
                operation = str(operation)

            if not isinstance(entities, list):  # noqa: SIM108 -- readability preferred over ternary
                entities = []
            else:
                # Ensure all entities are strings
                entities = [str(e) for e in entities if e]

            # Truncate operation if too long
            if operation and len(operation) > 200:
                operation = operation[:200] + "..."

            return operation, entities

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug(f"Failed to parse context extraction JSON: {e}")
            return None, []

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _build_session_context(
        self,
        session_state: Any | None,
    ) -> str:
        """Build session context section for LLM prompts.

        Creates a formatted summary of the session state for multi-turn
        conversations, including previous connectors, operation context,
        cached data, and recent errors.

        Args:
            session_state: OrchestratorSessionState from adapter, or None.

        Returns:
            Formatted session context string, or empty string for new conversations.
        """
        if not session_state:
            return ""

        turn_count = getattr(session_state, "turn_count", 0)
        if turn_count == 0:
            return ""

        parts = []

        connectors = getattr(session_state, "connectors", {})
        if connectors:
            prev_conns = []
            for c in connectors.values():
                status_icon = "OK" if c.last_status == "success" else "ERROR"
                query_note = (
                    f" ({c.last_query[:50]}{'...' if len(c.last_query) > 50 else ''})"
                    if c.last_query
                    else ""
                )
                prev_conns.append(f"- {c.connector_name} [{status_icon}]{query_note}")
            parts.append("**Previously Used Connectors:**\n" + "\n".join(prev_conns))

        # Current operation focus
        current_operation = getattr(session_state, "current_operation", None)
        if current_operation:
            parts.append(f"**User's Current Focus:** {current_operation}")

        # Entities being investigated
        operation_entities = getattr(session_state, "operation_entities", [])
        if operation_entities:
            entities = ", ".join(operation_entities[:5])
            parts.append(f"**Key Entities:** {entities}")

        # Cached data available
        cached_tables = getattr(session_state, "cached_tables", {})
        if cached_tables:
            tables = ", ".join(cached_tables.keys())
            parts.append(f"**Cached Data Available:** {tables}")
            parts.append("(User can query this cached data without re-fetching!)")

        # Recent errors to avoid
        recent_errors = getattr(session_state, "recent_errors", [])
        if recent_errors:
            err_notes = []
            for e in recent_errors[-3:]:  # Last 3 errors
                connector_id = e.get("connector_id", "unknown")[:8]
                error_type = e.get("error_type", "unknown")
                err_notes.append(f"- {connector_id}...: {error_type}")
            parts.append("**Recent Errors (avoid retrying):**\n" + "\n".join(err_notes))

        return "\n\n".join(parts)

    async def _get_available_connectors(self) -> list[dict[str, Any]]:
        """Get all active connectors with routing_description and relationship hints.

        Returns:
            List of connector dicts with id, name, routing_description, connector_type,
            and related_connectors for routing hints.
        """
        try:
            repo = self.dependencies.connector_repo
            tenant_id = self._get_tenant_id()
            logger.info(f"🔍 Getting connectors for tenant_id={tenant_id}")
            connectors = await repo.list_connectors(tenant_id=tenant_id)
            logger.info(f"🔍 Repository returned {len(connectors)} connectors")

            result = [
                {
                    "id": str(c.id),
                    "name": c.name,
                    "description": c.description or "",
                    "routing_description": getattr(c, "routing_description", "")
                    or c.description
                    or "",
                    "connector_type": c.connector_type,
                    "related_connectors": getattr(c, "related_connector_ids", []) or [],
                    # Skill fields for factory.create_agent() (Phase 7.1)
                    "generated_skill": getattr(c, "generated_skill", None),
                    "custom_skill": getattr(c, "custom_skill", None),
                    "skill_name": getattr(c, "skill_name", None),
                    # Connection details for error messaging (Phase 23)
                    "base_url": getattr(c, "base_url", None),
                }
                for c in connectors
                if c.is_active
            ]
            logger.info(f"🔍 After is_active filter: {len(result)} connectors")
            return result
        except (AttributeError, RuntimeError, TimeoutError, OSError) as e:
            logger.error(f"Failed to get connectors: {e}", exc_info=True)
            return []

    def _get_tenant_id(self) -> str:
        """Get tenant ID from dependencies."""
        if hasattr(self.dependencies, "user_context"):
            return str(self.dependencies.user_context.tenant_id)
        return "default"

    async def _get_skill_summaries(self, skill_type: str | None = None) -> str:
        """Fetch active orchestrator skill summaries for prompt injection.

        Args:
            skill_type: Optional filter -- "orchestrator" for investigation/orchestrator skills only,
                        "connector" for connector skills only, None for all skills.

        Returns:
            Formatted skill summaries string, or empty string if none found.
        """
        try:
            from meho_app.api.database import create_openapi_session_maker
            from meho_app.modules.orchestrator_skills.service import OrchestratorSkillService

            tenant_id = self._get_tenant_id()
            session_maker = create_openapi_session_maker()

            async with session_maker() as db:
                service = OrchestratorSkillService(db)
                skills = await service.list_active_skills(tenant_id)

            if not skills:
                return ""

            # Filter by skill_type if specified (Phase 77)
            if skill_type:
                skills = [
                    s for s in skills if getattr(s, "skill_type", "orchestrator") == skill_type
                ]
                if not skills:
                    return ""

            skill_lines = []
            for skill in skills:
                skill_lines.append(f"### {skill.name}\n{skill.summary}\n")

            return (
                "<orchestrator_skills>\n" + "\n".join(skill_lines) + "</orchestrator_skills>\n\n"
                "When your routing decision involves cross-system investigation "
                'that matches a skill above, include `"read_skill": "skill-name"` '
                "in your JSON response to load the full skill content."
            )
        except (AttributeError, RuntimeError, TimeoutError, OSError) as e:
            logger.warning(f"Failed to fetch orchestrator skill summaries: {e}")
            return ""

    async def _read_orchestrator_skill(self, skill_name: str) -> str | None:
        """Load full content of an orchestrator skill by name.

        Used when the orchestrator's routing decision includes a read_skill
        field to load the full skill content for synthesis.

        Args:
            skill_name: Name of the skill to load.

        Returns:
            Full skill markdown content, or None if not found.
        """
        try:
            from meho_app.api.database import create_openapi_session_maker
            from meho_app.modules.orchestrator_skills.service import OrchestratorSkillService

            tenant_id = self._get_tenant_id()
            session_maker = create_openapi_session_maker()

            async with session_maker() as db:
                service = OrchestratorSkillService(db)
                skill = await service.get_skill_by_name(tenant_id, skill_name)

            if skill:
                return str(skill.content)
            return None
        except (AttributeError, RuntimeError, TimeoutError, OSError) as e:
            logger.warning(f"Failed to read orchestrator skill '{skill_name}': {e}")
            return None

    async def _call_llm_streaming(
        self,
        prompt: str,
        session_id: str | None = None,
    ) -> AsyncIterator[AgentEvent | str]:
        """Call routing LLM with thinking stream for Phase 110.

        Streams thinking tokens as orchestrator_thinking events as the LLM
        reasons about the investigation plan, then yields the complete response
        text as the final item.

        Falls back to non-streaming ``_call_llm`` if the streaming connection
        fails after retries.

        Args:
            prompt: The prompt to send to the LLM.
            session_id: Optional session ID for event correlation.

        Yields:
            AgentEvent objects of type ``orchestrator_thinking`` as text arrives,
            followed by a final ``str`` containing the full LLM response.
        """
        import random as _random

        from pydantic_ai import Agent, InstrumentationSettings
        from pydantic_ai.exceptions import ModelAPIError as _ModelAPIError
        from pydantic_ai.exceptions import ModelHTTPError as _ModelHTTPError

        from meho_app.modules.agents.agent_factories import get_model_settings

        try:
            from meho_app.core.config import get_config as _get_config

            model_name = _get_config().llm_model
            if hasattr(self._config, "model") and hasattr(self._config.model, "name"):
                model_name = self._config.model.name

            model_settings = get_model_settings(task_type="synthesis")
            agent = Agent(
                model=model_name,
                model_settings=model_settings,
                instrument=InstrumentationSettings(),
            )

            accumulated_text = ""
            for _attempt in range(4):  # 1 initial + 3 retries
                try:
                    async with agent.run_stream(prompt) as stream:
                        async for chunk in stream.stream_text(delta=True):
                            accumulated_text += chunk
                            yield AgentEvent(
                                type="orchestrator_thinking",
                                agent=self.agent_name,
                                data={
                                    "content": chunk,
                                    "accumulated": accumulated_text,
                                },
                                session_id=session_id,
                            )
                    yield accumulated_text
                    return
                except (_ModelHTTPError, _ModelAPIError) as e:
                    retryable = isinstance(e, _ModelAPIError) or e.status_code in (
                        429,
                        500,
                        502,
                        503,
                        529,
                    )
                    if retryable and _attempt < 3:
                        _delay = 1.0 * (2**_attempt) * (0.8 + _random.random() * 0.4)  # noqa: S311
                        _detail = (
                            f"HTTP {e.status_code}"
                            if isinstance(e, _ModelHTTPError)
                            else "connection error"
                        )
                        logger.warning(
                            f"Routing LLM stream error ({_detail}), retrying in {_delay:.1f}s"
                        )
                        await asyncio.sleep(_delay)
                        accumulated_text = ""
                        continue
                    raise

            logger.warning("Routing LLM streaming exhausted retries, falling back to _call_llm")
            fallback_response = await self._call_llm(prompt)
            yield fallback_response

        except Exception:
            logger.warning("Routing LLM streaming failed, falling back to _call_llm", exc_info=True)
            fallback_response = await self._call_llm(prompt)
            yield fallback_response

    async def _call_llm(self, prompt: str) -> str:
        """Call LLM for routing/synthesis decisions.

        Args:
            prompt: The prompt to send to the LLM.

        Returns:
            LLM response text.
        """
        import time

        from pydantic_ai import Agent, InstrumentationSettings

        from meho_app.modules.agents.agent_factories import get_model_settings
        from meho_app.modules.agents.persistence.event_context import get_transcript_collector

        try:
            from meho_app.core.config import get_config as _get_config

            model_name = _get_config().llm_model
            if hasattr(self._config, "model") and hasattr(self._config.model, "name"):
                model_name = self._config.model.name

            model_settings = get_model_settings(task_type="synthesis")
            agent = Agent(
                model=model_name,
                model_settings=model_settings,
                instrument=InstrumentationSettings(),
            )

            start_time = time.perf_counter()
            result: Any = await _retry_llm_call(
                lambda: agent.run(prompt),
                max_retries=3,
                logger_ref=logger,
            )
            duration_ms = (time.perf_counter() - start_time) * 1000
            response: str = str(result.output)

            prompt_tokens, completion_tokens, total_tokens = self._extract_token_usage(result)
            collector = get_transcript_collector()
            if collector:
                await self._record_routing_llm_event(
                    result,
                    prompt,
                    response,
                    model_name,
                    duration_ms,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    collector,
                )

            return response
        except LLMError:
            raise
        except ModelHTTPError as e:
            raise LLMError(
                "unknown",
                "permanent",
                f"Claude API error (HTTP {e.status_code})",
                remediation="Check API configuration",
            ) from e
        except ModelAPIError as e:
            raise LLMError(
                "connection",
                "transient",
                "Cannot reach Claude API",
                remediation="Check network connectivity",
            ) from e
        except Exception as e:
            logger.exception(f"LLM call failed: {e}")
            raise InternalError(
                message=f"LLM call failed: {type(e).__name__}",
                remediation="Contact support with trace ID",
            ) from e

    def _extract_token_usage(self, result: Any) -> tuple[int, int, int]:
        """Extract (prompt_tokens, completion_tokens, total_tokens) from a PydanticAI result."""
        if not hasattr(result, "usage"):
            return 0, 0, 0
        try:
            usage = result.usage()
            if not usage:
                return 0, 0, 0
            pt = getattr(usage, "request_tokens", 0) or 0
            ct = getattr(usage, "response_tokens", 0) or 0
            tt = getattr(usage, "total_tokens", 0) or pt + ct
            return pt, ct, tt
        except Exception:  # noqa: S110 -- token usage extraction is optional
            return 0, 0, 0

    async def _record_routing_llm_event(
        self,
        result: Any,
        prompt: str,
        response: str,
        model_name: str,
        duration_ms: float,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        collector: TranscriptCollector,
    ) -> None:
        """Emit a DetailedEvent for an orchestrator LLM call to the transcript collector."""
        from datetime import datetime
        from uuid import uuid4

        from meho_app.modules.agents.base.detailed_events import (
            DetailedEvent,
            EventDetails,
            TokenUsage,
            estimate_cost,
            serialize_pydantic_messages,
        )

        messages = None
        if hasattr(result, "new_messages"):
            try:  # noqa: SIM105 -- explicit error handling preferred
                messages = serialize_pydantic_messages(result.new_messages())
            except Exception:  # noqa: S110 -- message serialization is optional
                pass

        cost = estimate_cost(model_name, prompt_tokens, completion_tokens)
        event = DetailedEvent(
            id=str(uuid4()),
            timestamp=datetime.now(tz=UTC),
            type="llm_call",
            summary=f"Orchestrator LLM call ({model_name})",
            details=EventDetails(
                llm_prompt=prompt,
                llm_response=str(response),
                llm_messages=messages,
                token_usage=TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    estimated_cost_usd=cost,
                ),
                llm_duration_ms=duration_ms,
                model=model_name,
            ),
        )
        await collector.add(event)
