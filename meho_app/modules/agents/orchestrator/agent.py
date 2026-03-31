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
import contextvars
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from opentelemetry.trace import use_span
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
from meho_app.modules.agents.orchestrator.context_passing import (
    build_structured_prior_findings,
    parse_discovered_entities,
)
from meho_app.modules.agents.orchestrator.topology_routing import (
    build_topology_investigation_context,
    route_via_topology,
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

        # Check for required 'orchestrator' section
        if "orchestrator" not in raw:
            errors.append("Missing 'orchestrator' section in config")
        else:
            orch = raw["orchestrator"]

            # Validate max_iterations
            max_iter = orch.get("max_iterations")
            if max_iter is not None and (not isinstance(max_iter, int) or max_iter < 1):
                errors.append(f"max_iterations must be a positive integer, got: {max_iter}")

            # Validate agent_timeout
            agent_timeout = orch.get("agent_timeout")
            if agent_timeout is not None:
                try:
                    timeout_val = float(agent_timeout)
                    if timeout_val <= 0:
                        errors.append(f"agent_timeout must be positive, got: {timeout_val}")
                except (TypeError, ValueError):
                    errors.append(f"agent_timeout must be a number, got: {agent_timeout}")

            # Validate total_timeout
            total_timeout = orch.get("total_timeout")
            if total_timeout is not None:
                try:
                    timeout_val = float(total_timeout)
                    if timeout_val <= 0:
                        errors.append(f"total_timeout must be positive, got: {timeout_val}")
                except (TypeError, ValueError):
                    errors.append(f"total_timeout must be a number, got: {total_timeout}")

            # Validate dispatch settings if present
            dispatch = orch.get("dispatch", {})
            if dispatch:
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
                        errors.append(
                            f"dispatch.min_relevance_score must be a number, got: {min_score}"
                        )

        if errors:
            error_msg = "Orchestrator config validation failed:\n  - " + "\n  - ".join(errors)
            logger.error(error_msg)
            raise ValueError(error_msg)

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

        # Extract conversation history from context (passed by adapter)
        conversation_history: list[dict[str, str]] | None = None
        if context and "history" in context:
            conversation_history = parse_history_text(context["history"])

        # Extract session_state from context (injected by adapter in Phase 2)
        session_state = context.get("session_state") if context else None

        # Initialize state with conversation history and session_state
        state = OrchestratorState(
            user_goal=user_message,
            session_id=session_id,
            conversation_history=conversation_history,
            max_iterations=self._get_max_iterations(),
            start_time_ms=time.time() * 1000,
            session_state=session_state,
        )

        # Phase 77: Set investigation budget from session_type
        investigation_config = self._get_investigation_config()
        if self.dependencies.session_type != "interactive":
            state.investigation_budget = investigation_config.get("budget_automated", 20)
        else:
            state.investigation_budget = investigation_config.get("budget_interactive", 30)
        state.convergence_window = investigation_config.get("convergence_window", 2)

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
                # Set in context for deep code paths (HTTP client, etc.)
                set_transcript_collector(transcript_collector)

        # Track if we're in partial/degraded mode
        is_partial = False
        loop_error: Exception | None = None

        # Start OTEL span for orchestrator run (OBSV-03)
        # Using manual span management because wrapping 500 lines of try/except/finally
        # in a `with` block would require re-indenting the entire method body.
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
                    decision = await self._decide_next_action(state)
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

                # STEP 2: Dispatch to selected connectors IN PARALLEL
                selected = decision["connectors"]
                state.selected_connectors = selected

                # Phase 99: Emit orchestrator_plan event (visible reasoning)
                classification = decision.get("classification", "standard")
                reasoning = decision.get("reasoning", "")
                planned_systems = []
                for c in selected:
                    planned_systems.append({
                        "id": c.connector_id,
                        "name": c.connector_name,
                        "reason": c.reason,
                        "priority": c.priority,
                        "conditional": c.conditional,
                    })

                yield AgentEvent(
                    type="orchestrator_plan",
                    agent=self.agent_name,
                    data={
                        "classification": classification,
                        "reasoning": reasoning,
                        "strategy": (
                            "direct" if classification == "quick"
                            else ("progressive" if classification == "standard" else "parallel")
                        ),
                        "planned_systems": planned_systems,
                        "estimated_calls": sum(
                            c.max_steps or self._get_default_max_steps() for c in selected
                        ),
                    },
                    session_id=session_id,
                )

                # Phase 99: Quick mode -- single specialist, no iteration
                if classification == "quick":
                    selected = [c for c in selected if c.priority == 1][:1]
                    state.should_continue = False  # No further iterations after this dispatch
                    state.selected_connectors = selected

                # Phase 99: Standard mode progressive dispatch -- priority-1 first
                if classification == "standard":
                    priority_1 = [c for c in selected if c.priority == 1]
                    priority_2 = [c for c in selected if c.priority == 2 or c.conditional]
                    if priority_1:
                        # Store P2 for potential follow-up iteration
                        state._deferred_connectors = priority_2
                        selected = priority_1
                        state.selected_connectors = selected

                yield AgentEvent(
                    type="dispatch_start",
                    agent=self.agent_name,
                    data={
                        "iteration": iteration,
                        "connectors": [
                            {"id": c.connector_id, "name": c.connector_name} for c in selected
                        ],
                    },
                    session_id=session_id,
                )

                # Build selection reasons dict for logging
                selection_reasons = {c.connector_id: c.reason for c in selected}
                log_structured(
                    logging.INFO,
                    "iteration_decision",
                    f"Dispatching to {len(selected)} connectors",
                    session_id=session_id,
                    iteration=iteration,
                    action="query",
                    connectors_selected=[c.connector_id for c in selected],
                    connector_names=[c.connector_name for c in selected],
                    selection_reasons=selection_reasons,
                )

                # STEP 3: Parallel execution - stream events as they arrive
                outputs: list[SubgraphOutput] = []
                async for event_or_output in self._dispatch_parallel(state, selected, iteration):
                    if isinstance(event_or_output, WrappedEvent):
                        # Stream wrapped event immediately (TTFUR)
                        yield AgentEvent(
                            type="agent_event",
                            agent=self.agent_name,
                            data=event_or_output.to_dict(),
                            session_id=session_id,
                        )
                        # Phase 62: Extract hypotheses from thought events
                        inner = event_or_output.inner_event
                        if inner.get("type") == "thought":
                            from meho_app.modules.agents.orchestrator.hypothesis_parser import (
                                extract_hypotheses,
                            )

                            hypotheses = extract_hypotheses(
                                inner.get("data", {}).get("content", "")
                            )
                            for h in hypotheses:
                                yield AgentEvent(
                                    type="hypothesis_update",
                                    agent=self.agent_name,
                                    data={
                                        "hypothesis_id": h["hypothesis_id"],
                                        "text": h["text"],
                                        "status": h["status"],
                                        "connector_id": event_or_output.agent_source.get(
                                            "connector_id"
                                        ),
                                        "connector_name": event_or_output.agent_source.get(
                                            "connector_name"
                                        ),
                                    },
                                    session_id=session_id,
                                )
                    else:
                        # SubgraphOutput - agent completed
                        outputs.append(event_or_output)

                        # TTFUR: Emit early_findings event so frontend can show progress
                        remaining_count = len(selected) - len(outputs)
                        yield AgentEvent(
                            type="early_findings",
                            agent=self.agent_name,
                            data={
                                "connector_id": event_or_output.connector_id,
                                "connector_name": event_or_output.connector_name,
                                "findings_preview": (
                                    event_or_output.findings[:200]
                                    if event_or_output.findings
                                    else None
                                ),
                                "status": event_or_output.status,
                                "remaining_count": remaining_count,
                            },
                            session_id=session_id,
                        )

                        # Also emit connector_complete for backward compatibility
                        yield AgentEvent(
                            type="connector_complete",
                            agent=self.agent_name,
                            data={
                                "connector_id": event_or_output.connector_id,
                                "connector_name": event_or_output.connector_name,
                                "status": event_or_output.status,
                                "findings_preview": (
                                    event_or_output.findings[:200]
                                    if event_or_output.findings
                                    else None
                                ),
                                "data_refs": event_or_output.data_refs,
                            },
                            session_id=session_id,
                        )

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

                # ===========================================================
                # Phase 77: Topology-driven follow-up traversal
                # Replaces Phase 32 entity routing with topology lookups,
                # budget enforcement, and LLM-judged convergence (D-11).
                # ===========================================================
                max_traversal_rounds = self._get_max_traversal_rounds()
                current_round = 1  # Round 1 just completed above

                # Record initial dispatches against budget
                state.record_dispatches([c.connector_id for c in selected])

                # Record initial novelty from routing decision (D-11)
                initial_has_novelty = decision.get("_llm_has_novelty", True) if decision else True
                state.record_novelty(initial_has_novelty)

                while current_round < max_traversal_rounds:
                    # Check budget and convergence before dispatching
                    if state.should_force_synthesis():
                        log_structured(
                            logging.INFO,
                            "force_synthesis",
                            f"Forcing synthesis: {state.budget_exhaustion_reason} "
                            f"(dispatches: {state.dispatch_count}/{state.investigation_budget})",
                            session_id=session_id,
                        )
                        break

                    # Parse structured entities from latest findings
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

                    # Route via topology (not ENTITY_ROUTING_MAP)
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
                    except Exception as e:
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

                    # Check if dispatching these would exceed budget
                    if state.dispatch_count + len(follow_ups) > state.investigation_budget:
                        # Trim to budget
                        remaining = state.remaining_budget()
                        if remaining <= 0:
                            break
                        follow_ups = follow_ups[:remaining]

                    current_round += 1

                    # Build investigation context
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

                    # Attach investigation context to follow-up ConnectorSelections
                    for fu in follow_ups:
                        fu._investigation_context = inv_context

                    # Emit dispatch event (keep existing SSE event format)
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

                    # Dispatch follow-up round
                    followup_outputs: list[SubgraphOutput] = []
                    async for event_or_output in self._dispatch_parallel(
                        state, follow_ups, iteration
                    ):
                        if isinstance(event_or_output, WrappedEvent):
                            yield AgentEvent(
                                type="agent_event",
                                agent=self.agent_name,
                                data=event_or_output.to_dict(),
                                session_id=session_id,
                            )
                            # Phase 62: Extract hypotheses from thought events
                            inner = event_or_output.inner_event
                            if inner.get("type") == "thought":
                                from meho_app.modules.agents.orchestrator.hypothesis_parser import (
                                    extract_hypotheses,
                                )

                                hypotheses = extract_hypotheses(
                                    inner.get("data", {}).get("content", "")
                                )
                                for h in hypotheses:
                                    yield AgentEvent(
                                        type="hypothesis_update",
                                        agent=self.agent_name,
                                        data={
                                            "hypothesis_id": h["hypothesis_id"],
                                            "text": h["text"],
                                            "status": h["status"],
                                            "connector_id": event_or_output.agent_source.get(
                                                "connector_id"
                                            ),
                                            "connector_name": event_or_output.agent_source.get(
                                                "connector_name"
                                            ),
                                        },
                                        session_id=session_id,
                                    )
                        else:
                            followup_outputs.append(event_or_output)
                            remaining_count = len(follow_ups) - len(followup_outputs)
                            yield AgentEvent(
                                type="early_findings",
                                agent=self.agent_name,
                                data={
                                    "connector_id": event_or_output.connector_id,
                                    "connector_name": event_or_output.connector_name,
                                    "findings_preview": (
                                        event_or_output.findings[:200]
                                        if event_or_output.findings
                                        else None
                                    ),
                                    "status": event_or_output.status,
                                    "remaining_count": remaining_count,
                                    "follow_up_round": current_round,
                                },
                                session_id=session_id,
                            )
                            yield AgentEvent(
                                type="connector_complete",
                                agent=self.agent_name,
                                data={
                                    "connector_id": event_or_output.connector_id,
                                    "connector_name": event_or_output.connector_name,
                                    "status": event_or_output.status,
                                    "findings_preview": (
                                        event_or_output.findings[:200]
                                        if event_or_output.findings
                                        else None
                                    ),
                                    "data_refs": event_or_output.data_refs,
                                    "follow_up_round": current_round,
                                },
                                session_id=session_id,
                            )

                    # Record dispatches against budget
                    state.record_dispatches([c.connector_id for c in follow_ups])

                    # Phase 77 D-11: Within-traversal convergence detection
                    # The primary D-11 mechanism is LLM-judged has_novelty from routing decisions.
                    # Within the traversal loop (topology-driven, no routing LLM call), use a
                    # lightweight heuristic: novelty = new connectors discovered OR meaningful findings.
                    new_connectors_found = any(
                        e.get("connector_id") and e["connector_id"] not in state.visited_connectors
                        for e in round_entities
                    )
                    followup_had_findings = any(
                        fo.status in ("success", "partial") and fo.findings and fo.findings.strip()
                        for fo in followup_outputs
                    )
                    traversal_has_novelty = new_connectors_found or followup_had_findings
                    state.record_novelty(traversal_has_novelty)

                    # Add follow-up findings to state
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

        except GeneratorExit:
            # Generator was cancelled (client disconnected, browser refresh)
            logger.warning(f"Generator cancelled for session {session_id} - client disconnected")
            is_partial = True
            # Add error event to transcript before cleanup
            if transcript_collector:
                try:
                    await transcript_collector.add_error_event(
                        "Client disconnected - generator cancelled"
                    )
                except Exception as e:
                    logger.warning(f"Failed to add error event: {e}")
            raise
        except Exception as e:
            error_msg = f"Orchestrator error: {e}"
            logger.exception(error_msg)
            loop_error = e
            is_partial = True

            # Add error event to transcript
            if transcript_collector:
                try:
                    await transcript_collector.add_error_event(str(e), include_traceback=True)
                except Exception as te:
                    logger.warning(f"Failed to add error event: {te}")

            # Classify the error
            error_classification = {
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

            yield AgentEvent(
                type="error",
                agent=self.agent_name,
                data={
                    "message": error_msg,
                    "findings_so_far": len(state.all_findings),
                    **error_classification,
                },
                session_id=session_id,
            )

            # STEP 5: Synthesize final answer
            # If we have findings (even partial), try to synthesize
            if state.all_findings or not loop_error:
                yield AgentEvent(
                    type="synthesis_start",
                    agent=self.agent_name,
                    data={"partial": is_partial},
                    session_id=session_id,
                )

                try:
                    # Stream synthesis chunks to frontend
                    async for chunk_event in self._synthesize_streaming(state, session_id):
                        yield chunk_event

                    # Emit final_answer with complete accumulated text
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
                        data={
                            "message": error_msg,
                            "recoverable": False,
                        },
                        session_id=session_id,
                    )

            # Emit usage summary (best-effort, before orchestrator_complete) - error path
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
                except Exception:  # noqa: S110 -- intentional silent exception handling
                    pass  # Usage summary is best-effort

            # Emit orchestrator complete
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

        else:
            # Normal completion path (no exception)
            # STEP 5: Synthesize final answer
            # If we have findings (even partial), try to synthesize
            if state.all_findings or not loop_error:
                yield AgentEvent(
                    type="synthesis_start",
                    agent=self.agent_name,
                    data={"partial": is_partial},
                    session_id=session_id,
                )

                # STEP 6: Extract operation context for multi-turn (Phase 5 - TASK-185)
                # This updates session_state in-place before adapter saves it
                if session_state and state.all_findings:
                    try:
                        operation, entities = await self._extract_operation_context(
                            user_message=user_message,
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
                    except Exception as ctx_err:
                        # Non-fatal - log and continue
                        logger.warning(f"Failed to extract operation context: {ctx_err}")

                try:
                    # Stream synthesis chunks to frontend
                    async for chunk_event in self._synthesize_streaming(state, session_id):
                        yield chunk_event

                    # Emit final_answer with complete accumulated text
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
                        data={
                            "message": error_msg,
                            "recoverable": False,
                        },
                        session_id=session_id,
                    )

            # Emit usage summary (best-effort, before orchestrator_complete) - normal path
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
                except Exception:  # noqa: S110 -- intentional silent exception handling
                    pass  # Usage summary is best-effort

            # Emit orchestrator complete
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
        finally:
            # ALWAYS close transcript collector - even on GeneratorExit
            if transcript_collector:
                try:
                    # Determine status based on what happened
                    if state.final_answer is not None:
                        status = "completed"
                    elif is_partial:
                        status = "interrupted"
                    else:
                        status = "failed"
                    await transcript_collector.close(status=status)
                    logger.debug(f"Transcript collector closed with status={status}")
                    # Fire memory extraction if enabled (Phase 10)
                    # Must be after close() so transcript is committed, before set_transcript_collector(None)
                    if status == "completed" and state.all_findings:
                        try:
                            from meho_app.core.config import get_config

                            config = get_config()
                            if config.enable_memory_extraction:
                                # Snapshot all data BEFORE spawning task (avoid referencing mutable state)
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
                        except Exception as extraction_err:
                            # Never let extraction failure affect the main flow
                            logger.warning(f"Failed to start memory extraction: {extraction_err}")
                except Exception as e:
                    logger.warning(f"Failed to close transcript collector: {e}")
                finally:
                    # Clear context to prevent leakage to other requests
                    set_transcript_collector(None)

            # Close OTEL orchestrator run span (OBSV-03)
            root_span.set_attribute("meho.iterations", state.current_iteration)
            root_span.set_attribute("meho.partial", is_partial)
            _root_span_ctx.__exit__(None, None, None)
            root_span.end()

    # =========================================================================
    # Decision Logic
    # =========================================================================

    async def _decide_next_action(self, state: OrchestratorState) -> dict[str, Any]:
        """LLM decides: query more connectors or respond to user.

        Uses iteration awareness to:
        - Exclude already-queried connectors
        - Bias toward "respond" on last iteration

        Args:
            state: Current orchestrator state.

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
            state, connectors, already_queried,
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

        response = await self._call_llm(prompt)
        decision = parse_decision(response, connectors)

        # Handle read_skill: load full skill content for synthesis (Phase 52)
        if decision.get("read_skill"):
            skill_name = decision["read_skill"]
            skill_content = await self._read_orchestrator_skill(skill_name)
            if skill_content:
                state.orchestrator_skill_context = skill_content
                logger.info(f"Loaded orchestrator skill '{skill_name}' for synthesis")

        # Phase 77: Extract LLM-judged novelty for convergence detection (D-11)
        # The routing LLM assesses whether specialist findings contain new information
        # has_novelty is part of the routing response JSON format (see routing.md)
        llm_has_novelty = decision.get("has_novelty", True)  # Default to True (assume novelty) if missing
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
            return self._config.raw["orchestrator"].get("investigation", {})
        return {}

    def _get_max_traversal_rounds(self) -> int:
        """Get max traversal rounds from config (default 10).

        Phase 77: Controls the topology-driven traversal loop. Separate from
        max_dispatch_rounds (Phase 32 legacy) and max_iterations (main loop).

        Returns:
            Maximum number of follow-up dispatch rounds per investigation.
        """
        return self._get_investigation_config().get("max_traversal_rounds", 10)

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
        except Exception:
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
        # OTEL dispatch span (OBSV-03) - child of orchestrator.run
        _dispatch_span = tracer.start_span(
            "meho.orchestrator.dispatch",
            attributes={
                "meho.connector_count": len(connectors),
                "meho.iteration": iteration,
            },
        )
        _dispatch_ctx = use_span(_dispatch_span, end_on_exit=False)
        _dispatch_ctx.__enter__()

        agent_timeout = self._get_agent_timeout()
        total_timeout = self._get_total_timeout()

        # Create queues and tasks for each connector
        queues: dict[str, asyncio.Queue[WrappedEvent | SubgraphOutput]] = {}
        tasks: dict[str, asyncio.Task[None]] = {}

        async def run_with_timeout(
            conn: ConnectorSelection,
            queue: asyncio.Queue[WrappedEvent | SubgraphOutput],
        ) -> None:
            """Run agent with timeout, put result in queue."""
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

        # Create tasks with timeout wrapper
        # IMPORTANT: Copy context to propagate transcript collector to child tasks
        # Without this, context variables (like the transcript collector) won't be
        # available in the spawned tasks, breaking observability event collection.
        ctx = contextvars.copy_context()
        for conn in connectors:
            queue: asyncio.Queue[WrappedEvent | SubgraphOutput] = asyncio.Queue()
            queues[conn.connector_id] = queue
            # Pass context to child task (Python 3.11+)
            task = asyncio.create_task(run_with_timeout(conn, queue), context=ctx)
            tasks[conn.connector_id] = task

        # Track completion
        pending_outputs: dict[str, SubgraphOutput | None] = {
            c.connector_id: None for c in connectors
        }
        completed = 0

        # Stream events with total timeout
        try:
            async with asyncio.timeout(total_timeout):
                while completed < len(connectors):
                    # Check all queues for events
                    for conn_id, queue in queues.items():
                        try:
                            item = queue.get_nowait()
                            if isinstance(item, SubgraphOutput):
                                pending_outputs[conn_id] = item
                                completed += 1
                                yield item
                            else:
                                # WrappedEvent - stream immediately
                                yield item
                        except asyncio.QueueEmpty:
                            pass

                    # Small sleep to avoid busy-waiting
                    await asyncio.sleep(0.01)

        except TimeoutError:
            logger.warning(
                f"Total timeout ({total_timeout}s) exceeded, cancelling remaining agents"
            )
            # Cancel remaining tasks
            for conn_id, task in tasks.items():  # noqa: B007 -- loop variable unused intentionally
                if not task.done():
                    task.cancel()

            # Yield timeout outputs for incomplete connectors
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

        # Ensure all tasks complete (with exceptions captured)
        await asyncio.gather(*tasks.values(), return_exceptions=True)

        # Close OTEL dispatch span (OBSV-03)
        _dispatch_ctx.__exit__(None, None, None)
        _dispatch_span.end()

    async def _run_single_agent(
        self,
        state: OrchestratorState,
        connector: ConnectorSelection,
        iteration: int,
        output_queue: asyncio.Queue[WrappedEvent | SubgraphOutput],
    ) -> None:
        """Run a single connector's agent and stream events to queue.

        Args:
            state: Current orchestrator state.
            connector: The connector to query.
            iteration: Current iteration number.
            output_queue: Queue to push events and final output to.
        """
        wrapper = EventWrapper(
            connector_id=connector.connector_id,
            connector_name=connector.connector_name,
            iteration=iteration,
        )

        start_time = time.time()

        try:
            # ─────────────────────────────────────────────────────
            # CREATE AGENT VIA FACTORY
            # ─────────────────────────────────────────────────────
            from meho_app.modules.agents.factory import create_agent

            # Phase 77: Resolve DB connector skill before creating specialist agent
            db_skill_content = None
            if connector.connector_type and connector.connector_type != "generic":
                try:
                    from meho_app.api.database import create_openapi_session_maker
                    from meho_app.modules.orchestrator_skills.service import OrchestratorSkillService
                    session_maker = create_openapi_session_maker()
                    async with session_maker() as db_session:
                        skill_svc = OrchestratorSkillService(db_session)
                        db_skill = await skill_svc.get_connector_skill(
                            self._get_tenant_id(), connector.connector_type
                        )
                        if db_skill:
                            db_skill_content = db_skill.content
                except Exception as e:
                    logger.debug(f"DB connector skill resolution failed for {connector.connector_type}: {e}")

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
                db_connector_skill=db_skill_content,  # Phase 77: DB-first connector skill
            )
            logger.info(f"Using {agent.agent_name} agent for connector {connector.connector_name}")

            # Build context-aware prompt for the agent
            # For follow-up rounds, investigation_context will be present
            inv_ctx = getattr(connector, "_investigation_context", None)
            agent_prompt = build_agent_prompt(state, connector, investigation_context=inv_ctx)

            # Run agent and stream wrapped events
            findings = ""
            # Phase 99: Build agent context with optional max_steps override
            agent_context: dict[str, Any] = {
                "iteration": iteration,
                "prior_findings": [f.findings for f in state.all_findings],
                "session_state": state.session_state,  # Phase 3: pass to sub-agents
            }
            if connector.max_steps is not None:
                agent_context["max_steps_override"] = connector.max_steps

            async for event in agent.run_streaming(
                user_message=agent_prompt,
                session_id=state.session_id,
                context=agent_context,
            ):
                # Wrap and queue the event
                wrapped = wrapper.wrap(event)
                await output_queue.put(wrapped)

                # Capture final answer
                if event.type == "final_answer":
                    findings = event.data.get("content", "")

            # Query cached tables for data_refs
            data_refs: list[dict[str, Any]] = []
            if state.session_id:
                try:
                    executor = get_unified_executor()
                    table_infos = await executor.get_session_table_info_async(state.session_id)
                    data_refs = [
                        {
                            "table": info["table"],
                            "session_id": state.session_id,
                            "row_count": info["row_count"],
                        }
                        for info in table_infos
                    ]
                    if data_refs:
                        logger.info(
                            f"Populated {len(data_refs)} data_refs for "
                            f"{connector.connector_name}: {[r['table'] for r in data_refs]}"
                        )
                except Exception as e:
                    logger.warning(
                        f"Failed to query cached tables for data_refs "
                        f"(session={state.session_id[:8]}...): {e}"
                    )

            # Queue the final output
            execution_time_ms = (time.time() - start_time) * 1000
            await output_queue.put(
                SubgraphOutput(
                    connector_id=connector.connector_id,
                    connector_name=connector.connector_name,
                    findings=findings,
                    status="success",
                    execution_time_ms=execution_time_ms,
                    data_refs=data_refs,
                )
            )

        except TimeoutError:
            execution_time_ms = (time.time() - start_time) * 1000
            error_msg = (
                f"{connector.connector_name} timed out after {int(execution_time_ms / 1000)}s"
            )
            remediation = f"Check {connector.connector_name}"
            if connector.base_url:
                remediation += f" at {connector.base_url}"
            await output_queue.put(
                SubgraphOutput(
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
            )
        except UpstreamApiError as e:
            execution_time_ms = (time.time() - start_time) * 1000
            if e.status_code == 401:
                error_msg = f"{connector.connector_name} authentication failed"
                remediation = (
                    f"Verify credentials for {connector.connector_name} in connector settings"
                )
                error_type = "auth"
                severity = "permanent"
            elif e.status_code >= 500:
                error_msg = f"{connector.connector_name} returned server error ({e.status_code})"
                remediation = (
                    f"{connector.base_url or connector.connector_name} may be experiencing issues"
                )
                error_type = "connection"
                severity = "transient"
            elif e.status_code == 429:
                error_msg = f"{connector.connector_name} rate limited"
                remediation = (
                    f"Too many requests to {connector.connector_name} -- will retry on next query"
                )
                error_type = "rate_limit"
                severity = "transient"
            else:
                error_msg = f"{connector.connector_name} returned error {e.status_code}"
                remediation = f"Check {connector.connector_name} API at {connector.base_url or 'configured endpoint'}"
                error_type = "data_error"
                severity = "permanent" if e.status_code < 500 else "transient"
            logger.warning(f"Connector error for {connector.connector_name}: {error_msg}")
            await output_queue.put(
                SubgraphOutput(
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
            )
        except Exception as e:
            execution_time_ms = (time.time() - start_time) * 1000
            logger.exception(f"Error running agent for {connector.connector_name}")
            await output_queue.put(
                SubgraphOutput(
                    connector_id=connector.connector_id,
                    connector_name=connector.connector_name,
                    findings="",
                    status="failed",
                    error_message=f"{connector.connector_name} encountered an unexpected error: {type(e).__name__}",
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
            )

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
        # OTEL synthesis span (OBSV-03) - child of orchestrator.run
        _synth_span = tracer.start_span(
            "meho.orchestrator.synthesis",
            attributes={
                "meho.session_id": session_id or "",
                "meho.findings_count": len(state.all_findings),
                "meho.partial": state.final_answer is None and bool(state.all_findings),
            },
        )
        _synth_ctx = use_span(_synth_span, end_on_exit=False)
        _synth_ctx.__enter__()

        # ============================================================
        # PASSTHROUGH: Single successful connector -- skip synthesis
        # (ARCH-03: orchestrator NEVER re-summarizes single-connector)
        # ============================================================
        successful = [f for f in state.all_findings if f.status == "success"]
        failed = [f for f in state.all_findings if f.status != "success"]

        if len(successful) == 1 and not failed:
            # Single-connector passthrough -- return findings directly
            finding = successful[0]
            state.final_answer = finding.findings

            logger.info(
                f"Single-connector passthrough: {finding.connector_name} (skipping synthesis LLM)"
            )

            yield AgentEvent(
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
            )
            # Close OTEL synthesis span before early return (OBSV-03)
            _synth_ctx.__exit__(None, None, None)
            _synth_span.end()
            return

        # ============================================================
        # MULTI-CONNECTOR or PARTIAL: Continue with LLM synthesis
        # ============================================================
        if not state.all_findings:
            # Conversational response - also stream it
            prompt = build_conversational_prompt(state.user_goal)
        else:
            # Fetch memory summaries for synthesis (Phase 11)
            synthesis_memory_summary = ""
            try:
                from meho_app.modules.agents.orchestrator.synthesis import (
                    build_memory_summaries_for_synthesis,
                )

                synthesis_memory_summary = await build_memory_summaries_for_synthesis(
                    state, tenant_id=self._get_tenant_id()
                )
            except Exception as e:
                logger.warning(f"Failed to fetch memory summaries for synthesis: {e}")
            # Get orchestrator skill context if loaded during routing (Phase 52)
            skill_context = ""
            if hasattr(state, "orchestrator_skill_context") and state.orchestrator_skill_context:
                skill_context = state.orchestrator_skill_context

            prompt = build_synthesis_prompt(
                state, memory_summary=synthesis_memory_summary, skill_context=skill_context
            )
            # Add multi-turn context for follow-up queries
            if state.session_state:
                turn_count = getattr(state.session_state, "turn_count", 0)
                if turn_count > 0:
                    session_context = self._build_session_context(state.session_state)
                    prompt += build_multi_turn_synthesis_context(
                        state.session_state, turn_count, session_context
                    )

        # Build the agent (same config as _call_llm)
        import time as _time

        from pydantic_ai import Agent, InstrumentationSettings

        try:
            from meho_app.core.config import get_config as _get_config

            model_name = _get_config().llm_model
            if hasattr(self._config, "model") and hasattr(self._config.model, "name"):
                model_name = self._config.model.name

            from meho_app.modules.agents.agent_factories import get_model_settings

            model_settings = get_model_settings(task_type="synthesis")

            agent = Agent(
                model=model_name,
                model_settings=model_settings,
                instrument=InstrumentationSettings(),
            )

            accumulated_text = ""
            start_time = _time.perf_counter()
            _stream_ref = None  # Keep reference for usage logging

            # Retry the stream connection with backoff for transient LLM errors
            import random as _random

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
                    # If we have accumulated text, keep it and report interruption
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

            # Use accumulated text as the final answer
            final_text = accumulated_text

            # Phase 62: Parse structured synthesis output and emit events
            try:
                from meho_app.modules.agents.orchestrator.synthesis_parser import (
                    build_citation_map,
                    parse_synthesis_sections,
                )

                sections = parse_synthesis_sections(final_text)
                if sections:
                    # Emit follow-up suggestions
                    if sections["follow_ups"]:
                        yield AgentEvent(
                            type="follow_up_suggestions",
                            agent=self.agent_name,
                            data={"suggestions": sections["follow_ups"]},
                            session_id=session_id,
                        )

                    # Build and emit citation map
                    citation_data = build_citation_map(final_text, state.all_findings)
                    if citation_data:
                        yield AgentEvent(
                            type="citation_map",
                            agent=self.agent_name,
                            data={"citations": citation_data},
                            session_id=session_id,
                        )
            except Exception as e:
                logger.warning(f"Failed to parse structured synthesis: {e}")

            # Log usage stats to transcript collector (best-effort)
            try:
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
                if collector and _stream_ref:
                    usage = _stream_ref.usage()
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
                            llm_response=final_text,
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
                    await collector.add_detailed_event(event)
            except Exception:  # noqa: S110 -- intentional silent exception handling
                pass  # Usage logging is best-effort

            state.final_answer = final_text

        except LLMError:
            # Already classified -- re-raise for upstream handling
            raise
        except Exception as e:
            # Fallback: if streaming fails, fall back to blocking _call_llm
            logger.warning(f"Streaming synthesis failed, falling back to blocking: {e}")
            final_text = await self._call_llm(prompt)
            # Phase 62: Parse structured synthesis output in fallback path
            try:
                from meho_app.modules.agents.orchestrator.synthesis_parser import (
                    build_citation_map,
                    parse_synthesis_sections,
                )

                sections = parse_synthesis_sections(final_text)
                if sections:
                    if sections["follow_ups"]:
                        yield AgentEvent(
                            type="follow_up_suggestions",
                            agent=self.agent_name,
                            data={"suggestions": sections["follow_ups"]},
                            session_id=session_id,
                        )
                    citation_data = build_citation_map(final_text, state.all_findings)
                    if citation_data:
                        yield AgentEvent(
                            type="citation_map",
                            agent=self.agent_name,
                            data={"citations": citation_data},
                            session_id=session_id,
                        )
            except Exception as parse_err:
                logger.warning(f"Failed to parse structured synthesis (fallback): {parse_err}")
            state.final_answer = final_text
        finally:
            # Close OTEL synthesis span (OBSV-03)
            _synth_ctx.__exit__(None, None, None)
            _synth_span.end()

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
        except Exception as e:
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

        except Exception as e:
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

        # Check turn_count - if 0, this is a new conversation
        turn_count = getattr(session_state, "turn_count", 0)
        if turn_count == 0:
            return ""

        parts = []

        # Previous connectors used
        connectors = getattr(session_state, "connectors", {})
        if connectors:
            prev_conns = []
            for c in connectors.values():
                status_icon = "OK" if c.last_status == "success" else "ERROR"
                query_note = ""
                if c.last_query:
                    query_text = c.last_query[:50]
                    if len(c.last_query) > 50:
                        query_text += "..."
                    query_note = f" ({query_text})"
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
        except Exception as e:
            logger.error(f"Failed to get connectors: {e}", exc_info=True)
            return []

    def _get_tenant_id(self) -> str:
        """Get tenant ID from dependencies."""
        if hasattr(self.dependencies, "user_context"):
            return self.dependencies.user_context.tenant_id
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
                skills = [s for s in skills if getattr(s, 'skill_type', 'orchestrator') == skill_type]
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
        except Exception as e:
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
                return skill.content
            return None
        except Exception as e:
            logger.warning(f"Failed to read orchestrator skill '{skill_name}': {e}")
            return None


    async def _call_llm(self, prompt: str) -> str:
        """Call LLM for routing/synthesis decisions.

        Args:
            prompt: The prompt to send to the LLM.

        Returns:
            LLM response text.
        """
        import time
        from datetime import datetime
        from uuid import uuid4

        from pydantic_ai import Agent

        from meho_app.modules.agents.base.detailed_events import (
            DetailedEvent,
            EventDetails,
            TokenUsage,
            estimate_cost,
            serialize_pydantic_messages,
        )
        from meho_app.modules.agents.persistence.event_context import (
            get_transcript_collector,
        )

        try:
            from meho_app.core.config import get_config as _get_config

            model_name = _get_config().llm_model
            if hasattr(self._config, "model") and hasattr(self._config.model, "name"):
                model_name = self._config.model.name

            from pydantic_ai import InstrumentationSettings

            # Use centralized model settings factory (Phase 82)
            from meho_app.modules.agents.agent_factories import get_model_settings

            model_settings = get_model_settings(task_type="synthesis")

            agent = Agent(
                model=model_name,
                model_settings=model_settings,
                instrument=InstrumentationSettings(),
            )

            # Run with timing and retry for transient LLM errors
            start_time = time.perf_counter()
            result = await _retry_llm_call(
                lambda: agent.run(prompt),
                max_retries=3,
                logger_ref=logger,
            )
            duration_ms = (time.perf_counter() - start_time) * 1000

            # Extract output from PydanticAI v1.63+ result
            response = result.output

            # Extract token usage
            prompt_tokens = 0
            completion_tokens = 0
            total_tokens = 0

            if hasattr(result, "usage"):
                try:
                    usage = result.usage()
                    if usage:
                        prompt_tokens = getattr(usage, "request_tokens", 0) or 0
                        completion_tokens = getattr(usage, "response_tokens", 0) or 0
                        total_tokens = (
                            getattr(usage, "total_tokens", 0) or prompt_tokens + completion_tokens
                        )
                except Exception:  # noqa: S110 -- intentional silent exception handling
                    pass  # Token usage extraction is optional - continue without it

            # Emit LLM event to transcript collector if available
            collector = get_transcript_collector()
            if collector:
                cost = estimate_cost(model_name, prompt_tokens, completion_tokens)

                # Serialize messages from PydanticAI result
                messages = None
                if hasattr(result, "new_messages"):
                    try:  # noqa: SIM105 -- explicit error handling preferred
                        messages = serialize_pydantic_messages(result.new_messages())
                    except Exception:  # noqa: S110 -- intentional silent exception handling
                        pass  # Message serialization is optional - continue without it

                event = DetailedEvent(
                    id=str(uuid4()),
                    timestamp=datetime.now(tz=UTC),  # Use naive UTC for database compatibility
                    type="llm_call",
                    summary=f"Orchestrator LLM call ({model_name})",
                    details=EventDetails(
                        llm_prompt=prompt,  # Full prompt, no truncation
                        llm_response=str(response),  # Full response, no truncation
                        llm_messages=messages,  # Full messages array
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

            return response
        except LLMError:
            # Already classified by retry utility -- re-raise
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
