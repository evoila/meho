# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""SpecialistAgent - Unified connector-scoped agent with ReAct loop.

This module implements the SpecialistAgent class, the primary connector-scoped
agent. A single SpecialistAgent is parameterized by injectable markdown skills
that become part of the system prompt.

Architecture (Phase 3):
- Uses a REACT LOOP (think-act-observe) instead of deterministic pipeline
- LLM decides WHAT to do, WHEN to stop, via structured output (ReActStep)
- Tool execution via TOOL_REGISTRY from react_agent/tools/
- Data reduction cache pattern preserved: LLM sees metadata only, full data goes to frontend
- Budget controls (8 steps default, extendable to 12) prevent runaway cost
- Loop detection prevents infinite cycling (same action+args twice)
- SSE events with connector_id routing metadata for frontend pane grouping

Structure:
    specialist_agent/
    ├── agent.py      # This file - ReAct loop orchestration
    ├── flow.py       # Legacy deterministic flow (deprecated, kept for imports)
    ├── models.py     # ReActStep + legacy Pydantic schemas
    ├── state.py      # SpecialistReActState + legacy WorkflowState
    ├── config.yaml   # Agent configuration (max_steps: 8)
    ├── prompts/
    │   └── system.md # ReAct system prompt template
    └── nodes/        # Legacy workflow nodes (deprecated, kept for imports)
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from meho_app.modules.topology.schemas import LookupTopologyResult
from uuid import uuid4

from opentelemetry.trace import use_span
from pydantic import ValidationError
from pydantic_ai import Agent, InstrumentationSettings
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from meho_app.core.errors import (
    UpstreamApiError,
    get_current_trace_id,
)
from meho_app.core.otel import get_logger, get_tracer
from meho_app.modules.agents.approval.pending_approvals import (
    cleanup_pending,
    register_pending,
)
from meho_app.modules.agents.approval.trust_classifier import (
    classify_operation,
)
from meho_app.modules.agents.approval.trust_classifier import (
    requires_approval as needs_approval,
)
from meho_app.modules.agents.base.agent import BaseAgent
from meho_app.modules.agents.base.detailed_events import (
    DetailedEvent,
    EventDetails,
    TokenUsage,
    estimate_cost,
    serialize_pydantic_messages,
)
from meho_app.modules.agents.base.events import AgentEvent
from meho_app.modules.agents.base.retry import retry_llm_call
from meho_app.modules.agents.config.loader import AgentConfig, load_yaml_config
from meho_app.modules.agents.models import TrustTier
from meho_app.modules.agents.persistence.event_context import get_transcript_collector
from meho_app.modules.agents.react_agent.agent import AgentDeps
from meho_app.modules.agents.shared.topology_utils import (
    extract_entity_mentions,
    extract_key_attributes,
    parse_verification_evidence,
)
from meho_app.modules.agents.specialist_agent.compressor import compress_observation
from meho_app.modules.agents.specialist_agent.models import ReActStep
from meho_app.modules.agents.specialist_agent.state import SpecialistReActState
from meho_app.modules.agents.specialist_agent.summarizer import _extract_key_param
from meho_app.modules.agents.sse.emitter import EventEmitter

logger = get_logger(__name__)

MEHO_CONNECTOR_ID_KEY = "meho.connector_id"

tracer = get_tracer("meho.specialist")

# Step budget constants (Phase 36: STEP-03)
EXTENSION_STEPS = 4  # Grant +4 steps on extension request
ABSOLUTE_MAX_STEPS = 12  # Hard cap, never exceeded

_TRUST_TO_DANGER: dict[TrustTier, str] = {
    TrustTier.READ: "safe",
    TrustTier.WRITE: "dangerous",
    TrustTier.DESTRUCTIVE: "critical",
}


# =============================================================================
# Module-level helpers for topology pre-population (Phase 19 - INT-01/FLOW-01)
# =============================================================================


def _format_entity_connector_name(entity: Any) -> str:
    """Return the display name for an entity's managing connector."""
    if hasattr(entity, "connector_name") and entity.connector_name:
        return str(entity.connector_name)
    if entity.connector_id:
        return f"connector:{entity.connector_id}"
    return "External/Unknown"


def _format_same_as_entry(correlated: Any) -> list[str]:
    """Build the SAME_AS lines for one correlated entity."""
    corr_entity = correlated.entity
    connector_display = correlated.connector_name or correlated.connector_type
    connector_id_str = str(corr_entity.connector_id) if corr_entity.connector_id else "unknown"
    confidence, evidence_summary = parse_verification_evidence(correlated.verified_via)
    entry = [
        f"  - **{corr_entity.name}** ({corr_entity.entity_type}) via {connector_display}",
        f"    connector_id: {connector_id_str}",
        f"    Match confidence: {confidence}",
    ]
    if evidence_summary:
        entry.append(f"    Evidence: {evidence_summary}")
    if corr_entity.raw_attributes:
        corr_key_attrs = extract_key_attributes(corr_entity.raw_attributes)
        if corr_key_attrs:
            entry.append(f"    Key identifiers: {corr_key_attrs}")
    return entry


def _format_topology_context(results: list[LookupTopologyResult]) -> str:
    """Format topology lookup results with SAME_AS correlations for prompt injection.

    Only includes entities that have SAME_AS correlations. Entities with only
    chain data but no SAME_AS links are intentionally excluded to avoid
    injecting irrelevant context.

    Ported and adapted from topology_lookup_node.py._format_context().

    Args:
        results: List of LookupTopologyResult objects that have same_as_entities.

    Returns:
        Formatted markdown string for injection into the system prompt.
    """
    lines = ["## Known Topology (from previous investigations)"]
    lines.append(
        "Use this information to skip discovery steps - go directly to relevant operations."
    )
    lines.append("")

    for result in results:
        entity = result.entity
        if entity is None:
            continue
        lines.append(f"### {entity.name} ({entity.entity_type})")
        lines.append(f"- **Managed by connector**: {_format_entity_connector_name(entity)}")

        if entity.raw_attributes:
            key_attrs = extract_key_attributes(entity.raw_attributes)
            if key_attrs:
                lines.append(f"- **Key identifiers**: {key_attrs}")

        if result.topology_chain:
            chain_str = " -> ".join(
                [f"{item.entity} ({item.entity_type})" for item in result.topology_chain[:5]]
            )
            lines.append(f"- **Topology chain**: {chain_str}")

        lines.append("")
        lines.append("#### Cross-System Identity (SAME_AS)")
        lines.append("This entity IS the same physical resource as:")
        for correlated in result.same_as_entities[:5]:
            lines.extend(_format_same_as_entry(correlated))
        lines.append(
            "  -> Investigate linked entities using their connector_id with search_operations and call_operation"
        )

        lines.append("")

    return "\n".join(lines)


@dataclass
class SpecialistAgent(BaseAgent):
    """Unified connector-scoped agent with ReAct loop.

    The primary connector-scoped agent. The only differentiator between
    connector types is the skill_content injected at construction time.
    Skills are markdown files that become part of the system prompt,
    teaching the agent domain-specific knowledge and tool usage patterns.

    The ReAct loop (Phase 3) replaces the deterministic 5-step pipeline:
    - LLM reasons about what to do next (structured output via ReActStep)
    - Tool execution via TOOL_REGISTRY (search_operations, call_operation, etc.)
    - Full observations passed to LLM (200k circuit breaker for memory safety)
    - Budget controls (8 steps default, extendable to 12) with best-effort answer on exhaustion
    - Loop detection prevents infinite cycling

    Attributes:
        agent_name: Class variable identifying this agent type.
        dependencies: Injected service container.
        connector_id: The connector this agent queries.
        connector_name: Human-readable connector name.
        connector_type: Type of connector (kubernetes, vmware, etc.).
        routing_description: What this connector manages (for context).
        skill_content: Markdown skill content injected into system prompt.
        iteration: Which orchestrator iteration this is part of.
        prior_findings: Findings from previous iterations for context.

    Example:
        >>> agent = SpecialistAgent(
        ...     dependencies=deps,
        ...     connector_id="k8s-prod",
        ...     connector_name="Production K8s",
        ...     connector_type="kubernetes",
        ...     skill_content=Path("skills/kubernetes.md").read_text(),
        ... )
        >>> async for event in agent.run_streaming("List all pods"):
        ...     print(event.type, event.data)
    """

    agent_name: ClassVar[str] = "specialist"

    # Connector scoping - set after initialization
    connector_id: str = ""
    connector_name: str = ""
    connector_type: str = ""  # kubernetes, vmware, etc.
    routing_description: str = ""

    # Skill injection -- the differentiator
    skill_content: str = ""

    # Orchestrator context
    iteration: int = 1
    prior_findings: list[str] = field(default_factory=list)

    def _load_config(self) -> AgentConfig:
        """Load configuration from config.yaml in agent folder."""
        return load_yaml_config(self.agent_folder / "config.yaml")

    def build_flow(self) -> str:
        """Build the node flow for this agent.

        Returns:
            Name of the entry node to start execution.
        """
        # Skip topology lookup for connector-scoped agent - go straight to reasoning
        return "reason"

    def _build_system_prompt(
        self, user_message: str, memory_context: str = "", topology_context: str = ""
    ) -> str:
        """Build the system prompt with skill content injected.

        Reads the prompt template from prompts/system.md and performs
        string replacement on all placeholders including {{skill_content}}.

        Args:
            user_message: The user's question/goal.
            memory_context: Formatted memory block for connector (Phase 11).
            topology_context: Formatted SAME_AS topology context (Phase 19).

        Returns:
            Fully rendered system prompt with no unresolved placeholders.
        """
        template_path = self.agent_folder / "prompts" / "system.md"
        template = template_path.read_text()

        # Build prior findings context
        prior_findings_context = ""
        if self.prior_findings:
            findings_lines = [
                f"[Previous Finding {i + 1}]: {finding}"
                for i, finding in enumerate(self.prior_findings)
            ]
            prior_findings_context = (
                "<prior_findings>\n" + "\n".join(findings_lines) + "\n</prior_findings>"
            )

        # Substitute all placeholders
        # Note: {{tables_context}} and {{topology_context}} are populated
        # at runtime via the reasoning prompt, not in the system prompt.
        # {{scratchpad}} is also passed separately via the reasoning prompt.
        substitutions = {
            "{{skill_content}}": self.skill_content,
            "{{connector_id}}": self.connector_id,
            "{{connector_name}}": self.connector_name,
            "{{connector_type}}": self.connector_type,
            "{{routing_description}}": self.routing_description,
            "{{user_goal}}": user_message,
            "{{prior_findings_context}}": prior_findings_context,
            "{{memory_context}}": memory_context,
            # tables_context is populated at runtime by the ReAct loop
            "{{tables_context}}": "",
            # topology_context is pre-populated from DB (Phase 19)
            "{{topology_context}}": topology_context,
        }

        rendered = template
        for placeholder, value in substitutions.items():
            rendered = rendered.replace(placeholder, value)

        return rendered

    @staticmethod
    def _build_tables_context(cached_tables: dict[str, Any]) -> str:
        """Build context string about cached tables for the reasoning prompt.

        Formats cached table information so the LLM knows it can call
        reduce_data directly on previously cached data.

        Args:
            cached_tables: Dictionary mapping table names to their metadata.
                Expected format: {"table_name": {"row_count": N, "columns": [...]}, ...}

        Returns:
            Formatted string describing cached tables, or empty string if none.
        """
        if not cached_tables:
            return ""

        lines = [
            "## Available Cached Tables",
            "You can query these directly with reduce_data (no need to call_operation again):\n",
        ]

        for table_name, info in cached_tables.items():
            row_count = info.get("row_count", "?")
            columns = info.get("columns", [])
            if columns:
                col_str = ", ".join(str(c) for c in columns[:8])
                if len(columns) > 8:
                    col_str += ", ..."
            else:
                col_str = "unknown columns"
            lines.append(f"- `{table_name}`: {row_count} rows, columns: [{col_str}]")

        return "\n".join(lines)

    @staticmethod
    def _build_cached_tables_section(cached_tables: dict[str, Any]) -> str | None:
        if not cached_tables:
            return None
        lines = [
            "## Available Cached Tables",
            "Query these directly with reduce_data (no need to call_operation again):\n",
        ]
        for table_name, info in cached_tables.items():
            row_count = info.get("row_count", "?")
            columns = info.get("columns", [])
            column_types = info.get("column_types", {})
            if columns:
                col_parts = []
                for c in columns[:10]:
                    c_str = str(c)
                    if c_str in column_types:
                        c_str += f":{column_types[c_str]}"
                    col_parts.append(c_str)
                col_str = ", ".join(col_parts)
                if len(columns) > 10:
                    col_str += f", ... ({len(columns)} total)"
            else:
                col_str = "unknown columns"
            lines.append(f"- `{table_name}`: {row_count} rows [{col_str}]")
        return "\n".join(lines)

    @staticmethod
    def _build_correlations_section(correlations: list[dict[str, Any]]) -> str | None:
        if not correlations:
            return None
        lines = [
            "## Discovered Correlations (SAME_AS)",
            "Cross-system identity mappings found during this investigation:\n",
        ]
        for corr in correlations:
            source = corr.get("source_entity", "?")
            target = corr.get("target_entity", "?")
            connector = corr.get("connector_name", corr.get("connector_id", "?"))
            confidence = corr.get("confidence", "")
            line = f"- **{source}** == **{target}** (via {connector})"
            if confidence:
                line += f" [{confidence}]"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _build_entities_section(entities: list[dict[str, Any]]) -> str | None:
        if not entities:
            return None
        lines = [
            "## Discovered Entities",
            "Key entities found during this investigation:\n",
        ]
        for ent in entities:
            name = ent.get("name", "?")
            etype = ent.get("type", "")
            connector_id = ent.get("connector_id", "")
            ctx = ent.get("context", "")
            line = f"- **{name}**"
            if etype:
                line += f" ({etype})"
            if connector_id:
                line += f" [connector: {connector_id}]"
            if ctx:
                line += f" -- {ctx}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _build_investigation_state(state: Any) -> str:
        """Build the investigation state block for injection into instructions.

        Rebuilds from structured data each step. Contains:
        - Cached tables with column types (from Arrow tables if available)
        - Discovered SAME_AS correlations (from topology lookups)
        - Discovered cross-system entities (from operation results)

        This is injected into PydanticAI `instructions` so it persists across
        every LLM call without consuming user-message tokens.

        Args:
            state: SpecialistReActState instance.

        Returns:
            Formatted investigation state wrapped in XML tags, or empty string.
        """
        sections = [
            s
            for s in [
                SpecialistAgent._build_cached_tables_section(state.cached_tables),
                SpecialistAgent._build_correlations_section(state.discovered_correlations),
                SpecialistAgent._build_entities_section(state.discovered_entities),
            ]
            if s
        ]
        if not sections:
            return ""
        body = "\n\n".join(sections)
        return f"\n<investigation_state>\n{body}\n</investigation_state>"

    @staticmethod
    def _extract_topology_correlations(state: Any, observation: Any) -> None:
        """Populate state.discovered_correlations and discovered_entities from lookup_topology."""
        if observation.found and observation.same_as_entities:
            entity_name = ""
            if observation.entity and isinstance(observation.entity, dict):
                entity_name = observation.entity.get("name", "")
            for corr in observation.same_as_entities:
                correlation = {
                    "source_entity": entity_name,
                    "target_entity": corr.get("name", "?"),
                    "connector_name": corr.get("connector", "?"),
                }
                if correlation not in state.discovered_correlations:
                    state.discovered_correlations.append(correlation)
        if observation.topology_chain:
            for chain_item in observation.topology_chain:
                entity_dict: dict[str, Any] = {
                    "name": chain_item.name,
                    "type": chain_item.entity_type or "",
                    "context": f"depth {chain_item.depth} in topology chain",
                }
                if chain_item.connector_type:
                    entity_dict["connector_type"] = chain_item.connector_type
                if not any(e.get("name") == entity_dict["name"] for e in state.discovered_entities):
                    state.discovered_entities.append(entity_dict)

    @staticmethod
    def _extract_cached_table(state: Any, observation: Any) -> None:
        """Update state.cached_tables from a successful call_operation result."""
        table_info: dict[str, Any] = {
            "table": observation.table,
            "row_count": observation.row_count,
            "columns": observation.columns or [],
        }
        if observation.column_types:
            table_info["column_types"] = observation.column_types
        state.cached_tables[observation.table] = table_info

    @staticmethod
    def _extract_investigation_data(state: Any, tool_name: str, observation: Any) -> None:
        """Extract structured investigation data from tool output before compression.

        Populates state.discovered_correlations from lookup_topology SAME_AS results,
        and updates state.cached_tables from call_operation cache metadata.
        Called before compress_observation destroys the Pydantic model structure.

        Args:
            state: SpecialistReActState instance.
            tool_name: Name of the tool that produced the observation.
            observation: Raw tool output (Pydantic model or string).
        """
        from meho_app.modules.agents.react_agent.tools.call_operation import CallOperationOutput
        from meho_app.modules.agents.react_agent.tools.lookup_topology import LookupTopologyOutput

        if tool_name == "lookup_topology" and isinstance(observation, LookupTopologyOutput):
            SpecialistAgent._extract_topology_correlations(state, observation)
        elif (
            tool_name == "call_operation"
            and isinstance(observation, CallOperationOutput)
            and observation.success
            and observation.table
        ):
            SpecialistAgent._extract_cached_table(state, observation)

    # =========================================================================
    # ReAct loop helpers
    # =========================================================================

    def _start_specialist_span(self, session_id: str | None) -> tuple[Any, Any]:
        """Create and enter the OTEL specialist run span.

        Returns:
            Tuple of (span, context_manager) — caller must call ctx.__exit__ and span.end().
        """
        span = tracer.start_span(
            "meho.specialist.run",
            attributes={
                MEHO_CONNECTOR_ID_KEY: self.connector_id,
                "meho.connector_name": self.connector_name,
                "meho.max_steps": self._config.max_steps,
                "meho.session_id": session_id or "",
            },
        )
        ctx = use_span(span, end_on_exit=False)
        ctx.__enter__()
        return span, ctx

    async def _load_cached_tables(self, session_id: str | None) -> dict[str, Any]:
        """Load cached table info from the session for multi-turn awareness.

        Returns:
            Dict mapping table name to table metadata, empty dict on failure.
        """
        cached_tables: dict[str, Any] = {}
        if session_id and hasattr(self.dependencies, "unified_executor"):
            try:
                table_infos = await self.dependencies.unified_executor.get_session_table_info_async(
                    session_id
                )
                cached_tables = {t["table"]: t for t in table_infos}
            except Exception as e:
                logger.warning(f"Failed to load cached tables: {e}")
        return cached_tables

    def _build_initial_state(
        self,
        user_message: str,
        ctx: dict[str, Any],
        cached_tables: dict[str, Any],
        session_id: str | None,
    ) -> SpecialistReActState:
        """Initialise SpecialistReActState from config, context, and cached tables.

        Returns:
            Initialised SpecialistReActState instance.
        """
        effective_max_steps = ctx.get("max_steps_override") or self._config.max_steps
        return SpecialistReActState(
            user_goal=user_message,
            connector_id=self.connector_id,
            connector_name=self.connector_name,
            skill_content=self.skill_content,
            max_steps=effective_max_steps,
            window_size=self._config.raw.get("window_size", 3),
            session_id=session_id,
            session_state=ctx.get("session_state"),
            cached_tables=cached_tables,
        )

    async def _fetch_operator_memory(self) -> str:
        """Fetch operator-provided memories for system prompt injection.

        Returns:
            Formatted memory context string, empty string on failure or no memories.
        """
        memory_context = ""
        try:
            from meho_app.modules.memory.context_builder import build_operator_memory_context

            memory_context = await build_operator_memory_context(
                connector_id=self.connector_id,
                tenant_id=self.dependencies.user_context.tenant_id,
            )
            if memory_context:
                logger.info(f"[{self.connector_name}] Loaded operator memories for context")
        except Exception as e:
            logger.warning(f"Failed to fetch operator memories: {e}")
        return memory_context

    async def _prefetch_topology_context(self, user_message: str) -> str:
        """Pre-populate SAME_AS topology context from DB for system prompt injection.

        Returns:
            Formatted topology context string, empty string on failure or no entities.
        """
        topology_context = ""
        try:
            from meho_app.api.database import create_openapi_session_maker

            session_maker = create_openapi_session_maker()
            async with session_maker() as topo_db:
                from meho_app.modules.topology.schemas import LookupTopologyInput
                from meho_app.modules.topology.service import TopologyService

                topo_service = TopologyService(topo_db)
                tenant_id = self.dependencies.user_context.tenant_id

                entity_mentions = extract_entity_mentions(user_message)
                if entity_mentions:
                    context_parts = []
                    for mention in entity_mentions[:5]:
                        result = await topo_service.lookup(
                            LookupTopologyInput(
                                query=mention, traverse_depth=5, cross_connectors=True
                            ),
                            tenant_id=tenant_id,
                        )
                        if result.found and result.same_as_entities:
                            context_parts.append(result)

                    if context_parts:
                        topology_context = _format_topology_context(context_parts)
                        logger.info(
                            f"[{self.connector_name}] Pre-populated topology context "
                            f"with {len(context_parts)} SAME_AS entities"
                        )
        except Exception as e:
            logger.warning(f"Topology pre-population failed (non-blocking): {e}")
        return topology_context

    def _create_event_emitter(
        self,
        agent_instance_name: str,
        session_id: str | None,
    ) -> tuple[EventEmitter, asyncio.Queue[AgentEvent]]:
        """Create the EventEmitter and SSE event queue for this run.

        Returns:
            Tuple of (emitter, event_queue).
        """
        emitter = EventEmitter(agent_name=agent_instance_name, session_id=session_id)
        event_queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        emitter.set_queue(event_queue)
        collector = get_transcript_collector()
        if collector:
            emitter.set_transcript_collector(collector)
        return emitter, event_queue

    def _setup_agent_runtime(
        self,
        session_id: str | None,
    ) -> tuple[AgentDeps, set[str], Agent[None, ReActStep], str]:
        """Build AgentDeps, feature-flag-filtered SPECIALIST_TOOLS, persistent agent, model name.

        Returns:
            Tuple of (tool_deps, specialist_tools, persistent_agent, model_name).
        """
        from meho_app.core.feature_flags import get_feature_flags
        from meho_app.modules.agents.agent_factories import get_model_settings

        tool_deps = AgentDeps(
            external_deps=self.dependencies,
            agent_config=self._config,
            session_id=session_id,
        )

        specialist_tools: set[str] = {
            "search_operations",
            "call_operation",
            "reduce_data",
            "lookup_topology",
            "search_knowledge",
            "store_memory",
            "forget_memory",
            "recall_memory",
            "dns_resolve",
            "tcp_probe",
            "http_probe",
            "tls_check",
        }

        _flags = get_feature_flags()
        if not _flags.knowledge:
            specialist_tools.discard("search_knowledge")
        if not _flags.topology:
            specialist_tools.discard("lookup_topology")
        if not _flags.memory:
            specialist_tools.discard("store_memory")
            specialist_tools.discard("forget_memory")
            specialist_tools.discard("recall_memory")
        if not _flags.network_diagnostics:
            specialist_tools.discard("dns_resolve")
            specialist_tools.discard("tcp_probe")
            specialist_tools.discard("http_probe")
            specialist_tools.discard("tls_check")

        model_name = self._config.model.name
        model_settings: dict[str, Any] = {}
        if self._config.model.temperature is not None:
            model_settings["temperature"] = self._config.model.temperature

        factory_settings = get_model_settings(task_type="specialist")
        effective_settings = (
            factory_settings
            if factory_settings is not None
            else (model_settings if model_settings else None)
        )

        persistent_agent: Agent[None, ReActStep] = Agent(
            model_name,
            output_type=ReActStep,
            instrument=InstrumentationSettings(),
            model_settings=effective_settings,  # type: ignore[arg-type]
        )

        return tool_deps, specialist_tools, persistent_agent, model_name

    async def _drain_event_queue(
        self, event_queue: asyncio.Queue[AgentEvent]
    ) -> AsyncIterator[AgentEvent]:
        """Yield all pending SSE events from the queue without blocking."""
        while not event_queue.empty():
            yield event_queue.get_nowait()

    async def _handle_tool_dispatch(
        self,
        react_step: ReActStep,
        action_input: dict[str, Any],
        state: SpecialistReActState,
        tool_deps: AgentDeps,
        emitter: EventEmitter,
        event_queue: asyncio.Queue[AgentEvent],
        session_id: str | None,
        dispatch_result: dict[str, Any],
    ) -> AsyncIterator[AgentEvent]:
        """Classify trust, gate approval, execute the tool, and log audit outcomes.

        Writes to dispatch_result:
          - "observation": Any — raw tool output (set on success or error). Usually a
            str from compressed observations, but tool return types are not narrowed,
            so a Pydantic model or dict is possible for unusual tool shapes.
          - "denied": True — set when operator denies the call (observation not set)
          - "trust_tier": TrustTier — always set
          - "approval_id": str | None — set when approval was requested
        """
        from meho_app.modules.agents.approval.repository import ApprovalStore

        # Invariant: this helper is only called when response_type == "action".
        # Guard satisfies mypy narrowing for all react_step.action usages below.
        if react_step.action is None:
            return

        # ── Trust classification ──────────────────────────────────────────────
        http_method = action_input.get("method")
        approval_id: str | None = None
        trust_override = None

        if react_step.action == "call_operation":
            operation_id_for_lookup = action_input.get("operation_id", "")
            try:
                from meho_app.api.database import create_openapi_session_maker
                from meho_app.modules.agents.approval.trust_classifier import (
                    safety_level_to_trust_tier,
                )

                if self.connector_type == "rest":
                    session_maker = create_openapi_session_maker()
                    async with session_maker() as db:
                        from meho_app.modules.connectors.rest.repository import (
                            EndpointDescriptorRepository,
                        )

                        repo = EndpointDescriptorRepository(db)
                        endpoint = await repo.get_endpoint_by_operation_id(
                            connector_id=self.connector_id,
                            operation_id=operation_id_for_lookup,
                        )
                        if endpoint and endpoint.safety_level:
                            trust_override = safety_level_to_trust_tier(endpoint.safety_level)
                else:
                    session_maker = create_openapi_session_maker()
                    async with session_maker() as db:
                        from meho_app.modules.connectors.repositories.operation_repository import (
                            ConnectorOperationRepository,
                        )

                        op_repo = ConnectorOperationRepository(db)
                        operation = await op_repo.get_operation_by_op_id(
                            connector_id=self.connector_id,
                            operation_id=operation_id_for_lookup,
                        )
                        if operation and operation.safety_level:
                            trust_override = safety_level_to_trust_tier(operation.safety_level)
            except Exception as override_err:
                logger.warning(f"Failed to look up trust override: {override_err}")

        trust_tier = classify_operation(
            connector_type=self.connector_type,
            operation_id=action_input.get("operation_id", react_step.action),
            http_method=http_method,
            override=trust_override,
        )
        dispatch_result["trust_tier"] = trust_tier

        # ── Approval gating ───────────────────────────────────────────────────
        if needs_approval(trust_tier) and react_step.action == "call_operation":
            op_id = action_input.get("operation_id", "")
            _approval_desc = (
                f"{op_id} on {self.connector_name}"
                if op_id
                else f"{react_step.action} on {self.connector_name}"
            )
            db_danger_level = _TRUST_TO_DANGER.get(trust_tier, "dangerous")
            _is_automated = self.dependencies.session_type != "interactive"

            approval_request = None
            try:
                if self.dependencies.db_session:
                    store = ApprovalStore(self.dependencies.db_session)
                    from uuid import UUID as _UUID

                    from meho_app.core.config import get_config

                    _expiry = 1440 if _is_automated else get_config().approval_expiry_minutes
                    approval_request = await store.create_pending(
                        session_id=_UUID(session_id) if session_id else _UUID(int=0),
                        tenant_id=self.dependencies.user_context.tenant_id or "",
                        user_id=self.dependencies.user_context.user_id or "",
                        tool_name=react_step.action,
                        tool_args=action_input,
                        danger_level=db_danger_level,
                        http_method=http_method,
                        endpoint_path=op_id,
                        description=_approval_desc,
                        user_message=state.user_goal,
                        expiry_minutes=_expiry,
                    )
                    await self.dependencies.db_session.commit()
            except Exception as db_err:
                logger.warning(f"Failed to create approval request in DB: {db_err}")

            if approval_request:
                approval_id = str(approval_request.id)
            else:
                import uuid as _uuid_mod

                approval_id = str(_uuid_mod.uuid4())
                logger.warning(
                    f"DB approval write failed/skipped; using fallback UUID {approval_id}"
                )

            dispatch_result["approval_id"] = approval_id
            pending = register_pending(
                session_id=session_id or "",
                tool_name=react_step.action,
                tool_args=action_input,
                approval_id=approval_id,
            )

            await emitter.approval_required(
                tool=react_step.action,
                args=action_input,
                danger_level=trust_tier.value,
                description=_approval_desc,
                approval_id=approval_id,
            )
            async for event in self._drain_event_queue(event_queue):
                yield event

            _notif_targets = getattr(self.dependencies, "notification_targets", None)
            if _notif_targets and _is_automated:
                from meho_app.modules.connectors.notification_dispatcher import (
                    dispatch_approval_notification,
                )

                asyncio.create_task(
                    dispatch_approval_notification(
                        notification_targets=_notif_targets,
                        session_id=session_id or "",
                        session_title=_approval_desc,
                        tool_name=react_step.action,
                        danger_level=db_danger_level,
                        tenant_id=self.dependencies.user_context.tenant_id or "",
                        trigger_source=self.dependencies.trigger_type or "automated",
                    )
                )

            try:
                while not pending.event.is_set():
                    try:
                        await asyncio.wait_for(pending.event.wait(), timeout=15.0)
                    except TimeoutError:
                        await emitter.keepalive()
                        async for event in self._drain_event_queue(event_queue):
                            yield event
            finally:
                cleanup_pending(session_id or "")

            if pending.approved:
                await emitter.approval_resolved(tool=react_step.action, approved=True)
                async for event in self._drain_event_queue(event_queue):
                    yield event
            else:
                await emitter.approval_resolved(tool=react_step.action, approved=False)
                denial_msg = (
                    f"Operator DENIED the {trust_tier.value} operation "
                    f"'{action_input.get('operation_id', react_step.action)}'. "
                    f"You cannot execute this operation. Try a read-only "
                    f"alternative or explain what you cannot do."
                )
                state.scratchpad.append(f"Observation: {denial_msg}")
                state.pending_user_message = denial_msg
                state.step_count += 1
                state.steps_executed.append(f"denied:{react_step.action} (step {state.step_count})")
                await emitter.audit_entry(
                    {
                        "approval_id": approval_id,
                        "tool": react_step.action,
                        "trust_tier": trust_tier.value,
                        "decision": "denied",
                        "outcome_status": "skipped",
                        "outcome_summary": "Operator denied the operation",
                        "connector_name": self.connector_name,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "user_id": self.dependencies.user_context.user_id or "",
                    }
                )
                async for event in self._drain_event_queue(event_queue):
                    yield event
                dispatch_result["denied"] = True
                return

        # ── Tool execution ────────────────────────────────────────────────────
        from meho_app.modules.agents.react_agent.tools import TOOL_REGISTRY

        await emitter.action(react_step.action, action_input)
        async for event in self._drain_event_queue(event_queue):
            yield event

        tool_class = TOOL_REGISTRY[react_step.action]
        tool_instance = tool_class()
        observation: Any

        try:
            validated_input = tool_class.validate_input(action_input)
            observation = await tool_instance.execute(validated_input, tool_deps, emitter)

            if needs_approval(trust_tier) and react_step.action == "call_operation":
                outcome_summary = (
                    observation[:200] if isinstance(observation, str) else str(observation)[:200]
                )
                await emitter.audit_entry(
                    {
                        "approval_id": approval_id,
                        "tool": react_step.action,
                        "trust_tier": trust_tier.value,
                        "decision": "approved",
                        "outcome_status": "success",
                        "outcome_summary": outcome_summary,
                        "connector_name": self.connector_name,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "user_id": self.dependencies.user_context.user_id or "",
                    }
                )
                try:
                    if self.dependencies.db_session and approval_id:
                        from uuid import UUID as _UUID

                        store = ApprovalStore(self.dependencies.db_session)
                        await store.log_execution_outcome(
                            approval_request_id=_UUID(approval_id),
                            session_id=session_id,
                            tenant_id=self.dependencies.user_context.tenant_id or "",
                            tool_name=react_step.action,
                            tool_args=action_input,
                            outcome_status="success",
                            outcome_summary=outcome_summary,
                            actor_id=self.dependencies.user_context.user_id or "system",
                        )
                        await self.dependencies.db_session.commit()
                except Exception as db_err:
                    logger.warning(f"Failed to log execution outcome: {db_err}")
                async for event in self._drain_event_queue(event_queue):
                    yield event

        except UpstreamApiError as tool_err:
            if tool_err.status_code == 401:
                observation = f"Tool error: {self.connector_name} authentication failed -- verify credentials in connector settings"
            elif tool_err.status_code >= 500:
                observation = f"Tool error: {self.connector_name} server error ({tool_err.status_code}) -- the target system may be down"
            elif tool_err.status_code == 429:
                observation = f"Tool error: {self.connector_name} rate limited -- too many requests"
            else:
                observation = (
                    f"Tool error: {self.connector_name} returned error {tool_err.status_code}"
                )
            logger.warning(f"Tool {react_step.action} connector error: {tool_err}")

            if needs_approval(trust_tier) and react_step.action == "call_operation":
                await emitter.audit_entry(
                    {
                        "approval_id": approval_id,
                        "tool": react_step.action,
                        "trust_tier": trust_tier.value,
                        "decision": "approved",
                        "outcome_status": "failure",
                        "outcome_summary": str(tool_err)[:200],
                        "connector_name": self.connector_name,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "user_id": self.dependencies.user_context.user_id or "",
                    }
                )
                try:
                    if self.dependencies.db_session and approval_id:
                        from uuid import UUID as _UUID

                        store = ApprovalStore(self.dependencies.db_session)
                        await store.log_execution_outcome(
                            approval_request_id=_UUID(approval_id),
                            session_id=session_id,
                            tenant_id=self.dependencies.user_context.tenant_id or "",
                            tool_name=react_step.action,
                            tool_args=action_input,
                            outcome_status="failure",
                            outcome_summary=str(tool_err)[:200],
                            actor_id=self.dependencies.user_context.user_id or "system",
                        )
                        await self.dependencies.db_session.commit()
                except Exception as db_err:
                    logger.warning(f"Failed to log execution failure outcome: {db_err}")
                async for event in self._drain_event_queue(event_queue):
                    yield event

        except ValidationError as val_err:
            _tool_format_hints: dict[str, str] = {
                "call_operation": (
                    'Expected: {"connector_id": "<uuid>", "operation_id": "<id>", '
                    '"parameter_sets": [{}]}'
                ),
                "reduce_data": ('Expected: {"sql": "SELECT ... FROM <table>"}'),
                "search_operations": (
                    'Expected: {"connector_id": "<uuid>", "query": "<search terms>"}'
                ),
                "lookup_topology": ('Expected: {"query": "<entity name>"}'),
                "store_memory": (
                    'Expected: {"title": "<short title>", "body": "<details>", '
                    '"memory_type": "config|incident|architecture"}'
                ),
                "search_knowledge": ('Expected: {"query": "<search terms>"}'),
            }
            format_hint = _tool_format_hints.get(react_step.action, "")
            missing = [e["loc"][-1] for e in val_err.errors() if e["type"] == "missing"]
            if missing:
                observation = (
                    f"Tool {react_step.action} missing required parameters: "
                    f"{', '.join(str(f) for f in missing)}. "
                    f"Provide them in action_input. "
                    f"Refer to the <tools> section for required arguments."
                )
            else:
                observation = (
                    f"Tool {react_step.action} invalid parameters: "
                    f"{'; '.join(e['msg'] for e in val_err.errors()[:3])}. "
                    f"Fix action_input and try again."
                )
            if format_hint:
                observation += f"\n{format_hint}"
            logger.warning(f"Tool {react_step.action} validation failed: {val_err}")

        except Exception as tool_err:
            observation = f"Tool error: {tool_err}"
            logger.warning(f"Tool {react_step.action} failed: {tool_err}")

            if needs_approval(trust_tier) and react_step.action == "call_operation":
                await emitter.audit_entry(
                    {
                        "approval_id": approval_id,
                        "tool": react_step.action,
                        "trust_tier": trust_tier.value,
                        "decision": "approved",
                        "outcome_status": "failure",
                        "outcome_summary": str(tool_err)[:200],
                        "connector_name": self.connector_name,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "user_id": self.dependencies.user_context.user_id or "",
                    }
                )
                try:
                    if self.dependencies.db_session and approval_id:
                        from uuid import UUID as _UUID

                        store = ApprovalStore(self.dependencies.db_session)
                        await store.log_execution_outcome(
                            approval_request_id=_UUID(approval_id),
                            session_id=session_id,
                            tenant_id=self.dependencies.user_context.tenant_id or "",
                            tool_name=react_step.action,
                            tool_args=action_input,
                            outcome_status="failure",
                            outcome_summary=str(tool_err)[:200],
                            actor_id=self.dependencies.user_context.user_id or "system",
                        )
                        await self.dependencies.db_session.commit()
                except Exception as db_err:
                    logger.warning(f"Failed to log execution failure outcome: {db_err}")
                async for event in self._drain_event_queue(event_queue):
                    yield event

        dispatch_result["observation"] = observation

    async def _execute_react_step(
        self,
        state: SpecialistReActState,
        persistent_agent: Agent[None, ReActStep],
        system_prompt: str,
        specialist_tools: set[str],
        tool_deps: AgentDeps,
        emitter: EventEmitter,
        event_queue: asyncio.Queue[AgentEvent],
        model_name: str,
        agent_instance_name: str,
        session_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute one complete iteration of the ReAct loop.

        Reads state.pending_user_message at entry; writes the next value before returning.
        Opens and closes the OTEL step span via try/finally — all early-exit paths
        (final answer, invalid action, loop detection, denial) use plain return statements.
        """
        _step_num = state.step_count + 1
        _step_span = tracer.start_span(
            f"meho.specialist.step.{_step_num}",
            attributes={
                "meho.step_number": _step_num,
                MEHO_CONNECTOR_ID_KEY: self.connector_id,
            },
        )
        _step_ctx = use_span(_step_span, end_on_exit=False)
        _step_ctx.__enter__()

        try:
            # ── REASON ────────────────────────────────────────────────────────
            _investigation_state = self._build_investigation_state(state)
            _current_msg = state.pending_user_message
            if _investigation_state:
                _current_msg = state.pending_user_message + "\n\n" + _investigation_state
            _current_instructions = system_prompt  # stable across steps for cache hits

            _llm_start = time.perf_counter()
            result: Any = await retry_llm_call(
                lambda msg=_current_msg, instr=_current_instructions: persistent_agent.run(
                    msg,
                    instructions=instr,
                    message_history=state.message_history if state.message_history else None,
                ),
                max_retries=3,
                logger_ref=logger,
            )
            _llm_duration_ms = (time.perf_counter() - _llm_start) * 1000
            react_step = result.output
            state.message_history = list(result.all_messages())

            # ── Transcript emission ───────────────────────────────────────────
            _collector = get_transcript_collector()
            if _collector:
                _prompt_tokens = 0
                _completion_tokens = 0
                _cache_read = 0
                _cache_write = 0
                if hasattr(result, "usage"):
                    try:
                        _usage = result.usage()
                        if _usage:
                            _prompt_tokens = getattr(_usage, "request_tokens", 0) or 0
                            _completion_tokens = getattr(_usage, "response_tokens", 0) or 0
                            _details = getattr(_usage, "details", None)
                            if _details and isinstance(_details, dict):
                                _cache_read = _details.get("cache_read_input_tokens", 0) or 0
                                _cache_write = _details.get("cache_creation_input_tokens", 0) or 0
                    except Exception:  # noqa: S110 -- intentional silent exception handling
                        pass
                _cost = estimate_cost(
                    model_name,
                    _prompt_tokens,
                    _completion_tokens,
                    _cache_read,
                    _cache_write,
                )
                _messages = None
                if hasattr(result, "new_messages"):
                    with contextlib.suppress(Exception):
                        _messages = serialize_pydantic_messages(result.new_messages())
                _parsed = None
                if hasattr(react_step, "model_dump"):
                    with contextlib.suppress(Exception):
                        _parsed = react_step.model_dump()
                _event = DetailedEvent(
                    id=str(uuid4()),
                    timestamp=datetime.now(UTC),
                    type="llm_call",
                    summary=(f"LLM call to {model_name} (ReActStep) [step {state.step_count + 1}]"),
                    details=EventDetails(
                        llm_prompt=_current_msg,
                        llm_response=str(_parsed) if _parsed else str(react_step),
                        llm_messages=_messages,
                        llm_parsed=_parsed,
                        token_usage=TokenUsage(
                            prompt_tokens=_prompt_tokens,
                            completion_tokens=_completion_tokens,
                            total_tokens=_prompt_tokens + _completion_tokens,
                            estimated_cost_usd=_cost,
                            cache_read_tokens=_cache_read,
                            cache_write_tokens=_cache_write,
                        ),
                        llm_duration_ms=_llm_duration_ms,
                        model=model_name,
                    ),
                )
                await _collector.add(_event)

            await emitter.thought(react_step.thought)
            async for event in self._drain_event_queue(event_queue):
                yield event

            # ── Final answer ──────────────────────────────────────────────────
            if react_step.response_type == "final_answer" and react_step.final_answer:
                state.final_answer = react_step.final_answer
                state.steps_executed.append(f"final_answer (step {state.step_count + 1})")
                return

            # ── Action validation ─────────────────────────────────────────────
            if not react_step.action or react_step.action not in specialist_tools:
                error_text = (
                    f"Error: Invalid tool '{react_step.action}'. "
                    f"Use one of: {', '.join(sorted(specialist_tools))}"
                )
                await emitter.error(
                    f"Invalid tool: {react_step.action}. "
                    f"Available: {', '.join(sorted(specialist_tools))}"
                )
                state.scratchpad.append(error_text)
                state.pending_user_message = error_text
                state.step_count += 1
                async for event in self._drain_event_queue(event_queue):
                    yield event
                return

            action_input = (
                react_step.action_input.model_dump(exclude={"tool"})
                if react_step.action_input is not None
                else {}
            )

            # ── Loop detection ────────────────────────────────────────────────
            if state.has_duplicate_action(react_step.action, action_input):
                loop_text = (
                    f"LOOP DETECTED: You already called {react_step.action} "
                    f"with these exact parameters. You must provide a final answer now."
                )
                logger.warning(f"Loop detected: {react_step.action} called with same args twice")
                state.scratchpad.append(loop_text)
                state.pending_user_message = loop_text
                state.step_count += 1
                async for event in self._drain_event_queue(event_queue):
                    yield event
                return

            state.record_action(react_step.action, action_input)

            # ── Budget extension ──────────────────────────────────────────────
            if (
                react_step.response_type == "action"
                and react_step.extend_budget
                and not state.budget_extended
            ):
                old_max = state.max_steps
                state.max_steps = min(state.max_steps + EXTENSION_STEPS, ABSOLUTE_MAX_STEPS)
                state.budget_extended = True
                logger.info(
                    f"[{self.connector_name}] Budget extended: {old_max} -> {state.max_steps}"
                )
                yield AgentEvent(
                    type="budget_extended",
                    agent=agent_instance_name,
                    data={
                        "old_max": old_max,
                        "new_max": state.max_steps,
                        "connector_id": self.connector_id,
                        "connector_name": self.connector_name,
                    },
                    session_id=session_id,
                )

            # ── Server-side connector_id injection ────────────────────────────
            if react_step.action in (
                "search_operations",
                "call_operation",
                "search_knowledge",
                "store_memory",
                "forget_memory",
                "recall_memory",
            ):
                action_input["connector_id"] = self.connector_id

            # ── Tool dispatch (trust + approval + execution + audit) ──────────
            dispatch_result: dict[str, Any] = {}
            async for event in self._handle_tool_dispatch(
                react_step,
                action_input,
                state,
                tool_deps,
                emitter,
                event_queue,
                session_id,
                dispatch_result,
            ):
                yield event

            if dispatch_result.get("denied"):
                return

            observation = dispatch_result["observation"]

            # ── Observation ───────────────────────────────────────────────────
            self._extract_investigation_data(state, react_step.action, observation)
            observation_str = await compress_observation(
                observation=observation,
                tool_name=react_step.action,
                thought=react_step.thought,
            )
            state.add_observation(
                react_step.action,
                observation_str,
                action_input_key=_extract_key_param(react_step.action, action_input),
            )
            state.step_count += 1
            state.steps_executed.append(f"{react_step.action} (step {state.step_count})")

            # ── Next pending message ──────────────────────────────────────────
            next_step = state.step_count + 1
            step_info = (
                f"Step {next_step} of {state.max_steps} "
                f"(extended from {state.max_steps - EXTENSION_STEPS})"
                if state.budget_extended
                else f"Step {next_step} of {state.max_steps}"
            )
            state.pending_user_message = (
                f"Observation from {react_step.action}: {observation_str}\n\n"
                f"{step_info}. What should you do next?"
            )
            steps_remaining = state.max_steps - next_step
            if steps_remaining <= 2 and not state.budget_extended:
                state.pending_user_message += (
                    "\n\nBudget running low -- consider synthesizing your findings "
                    "or requesting an extension with extend_budget."
                )
            elif steps_remaining <= 2 and state.budget_extended:
                state.pending_user_message += (
                    "\n\nBudget running low -- consider synthesizing your findings."
                )

            await emitter.observation(react_step.action, observation_str[:500])
            await emitter.emit(
                "step_progress",
                {
                    "step": state.step_count,
                    "max_steps": state.max_steps,
                    "connector_id": self.connector_id,
                    "connector_name": self.connector_name,
                },
            )
            async for event in self._drain_event_queue(event_queue):
                yield event

        finally:
            _step_ctx.__exit__(None, None, None)
            _step_span.end()

    async def _forced_synthesis(
        self,
        state: SpecialistReActState,
        persistent_agent: Agent[None, ReActStep],
        system_prompt: str,
    ) -> None:
        """Run one free LLM call on budget exhaustion to synthesise a final answer.

        Mutates state.final_answer on success; falls back to observations summary on error.
        """
        _synth_span = tracer.start_span(
            "meho.specialist.synthesis",
            attributes={
                MEHO_CONNECTOR_ID_KEY: self.connector_id,
                "meho.synthesis_type": "budget_exhaustion",
                "meho.steps_used": state.step_count,
                "meho.max_steps": state.max_steps,
            },
        )
        _synth_ctx = use_span(_synth_span, end_on_exit=False)
        _synth_ctx.__enter__()

        synthesis_prompt = (
            "Budget exhausted. Synthesize your findings into a final answer now.\n\n"
            "Start your answer with: "
            '"Note: I reached my investigation step limit, '
            "but here's what I found:\"\n\n"
            "Provide a complete, actionable answer based on everything you have learned."
        )

        try:
            _synth_investigation_state = self._build_investigation_state(state)
            _synth_instructions = system_prompt + _synth_investigation_state
            synthesis_result: Any = await retry_llm_call(
                lambda msg=synthesis_prompt, instr=_synth_instructions: persistent_agent.run(
                    msg,
                    instructions=instr,
                    message_history=state.message_history,
                ),
                max_retries=3,
                logger_ref=logger,
            )
            synth_step = synthesis_result.output
            state.message_history = list(synthesis_result.all_messages())

            if synth_step.final_answer:
                state.final_answer = synth_step.final_answer
            else:
                logger.warning(
                    f"[{self.connector_name}] Synthesis returned action instead of final_answer"
                )
                state.final_answer = synth_step.thought
        except Exception as synth_err:
            logger.warning(f"[{self.connector_name}] Forced synthesis failed: {synth_err}")
            observations_summary = state.get_observations_summary()
            state.final_answer = (
                f"Note: I reached my investigation step limit, "
                f"but here's what I found:\n\n{observations_summary}"
            )
        finally:
            state.steps_executed.append(f"forced_synthesis (after step {state.step_count})")
            _synth_ctx.__exit__(None, None, None)
            _synth_span.end()

    def _finalize_react_run(self, state: SpecialistReActState, span: Any, ctx: Any) -> None:
        """Set final OTEL span attributes and close the specialist run span."""
        span.set_attribute("meho.steps_taken", state.step_count)
        span.set_attribute(
            "meho.status",
            "success" if state.final_answer is not None else (state.error_message or "unknown"),
        )
        ctx.__exit__(None, None, None)
        span.end()

    async def run_streaming(
        self,
        user_message: str,
        session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute connector-scoped agent with SSE streaming output.

        Uses the ReAct loop to dynamically investigate the connector.
        The LLM decides what tools to call, in what order, and when to stop.

        Args:
            user_message: The user's input message (or orchestrator sub-goal).
            session_id: Optional session ID for conversation tracking.
            context: Optional context including prior_findings, iteration, etc.

        Yields:
            AgentEvent objects for SSE streaming.
        """
        async for event in self._run_react(user_message, session_id, context):
            yield event

    async def _run_react(
        self,
        user_message: str,
        session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute using ReAct loop (think-act-observe).

        Replaces the deterministic 5-step workflow with dynamic investigation.
        The LLM reasons about what to do next via structured output (ReActStep),
        executes tools from TOOL_REGISTRY, and observes results until it has
        enough information to provide a final answer.

        Budget controls enforce a step limit (default 8, extendable to 12). Loop detection
        prevents infinite cycling. The data reduction cache pattern is strictly
        preserved: call_operation stores raw data in cache, LLM only sees
        metadata (table name, row count, columns).

        Args:
            user_message: The user's question/goal.
            session_id: Optional session ID for tracking.
            context: Optional context dict.

        Yields:
            AgentEvent objects for SSE streaming.
        """
        ctx = context or {}
        agent_instance_name = f"{self.agent_name}_{self.connector_id}"

        _specialist_span, _specialist_ctx = self._start_specialist_span(session_id)
        state = None
        try:
            cached_tables = await self._load_cached_tables(session_id)
            state = self._build_initial_state(user_message, ctx, cached_tables, session_id)
            memory_context = await self._fetch_operator_memory()
            topology_context = await self._prefetch_topology_context(user_message)
            emitter, event_queue = self._create_event_emitter(agent_instance_name, session_id)

            yield AgentEvent(
                type="agent_start",
                agent=agent_instance_name,
                data={
                    "user_message": user_message,
                    "connector_id": self.connector_id,
                    "connector_name": self.connector_name,
                    "mode": "react",
                    "max_steps": state.max_steps,
                },
                session_id=session_id,
            )

            logger.info(
                f"[{self.connector_name}] Starting ReAct loop "
                f"for goal: {user_message[:100]}... "
                f"(max_steps={state.max_steps})"
            )

            system_prompt = self._build_system_prompt(
                user_message, memory_context=memory_context, topology_context=topology_context
            )
            tool_deps, specialist_tools, persistent_agent, model_name = self._setup_agent_runtime(
                session_id
            )

            state.pending_user_message = (
                f"Operator Question: {user_message}\n\n"
                f"Step 1 of {state.max_steps}. What should you do next?"
            )

            try:
                while not state.is_complete() and state.step_count < state.max_steps:
                    async for event in self._execute_react_step(
                        state,
                        persistent_agent,
                        system_prompt,
                        specialist_tools,
                        tool_deps,
                        emitter,
                        event_queue,
                        model_name,
                        agent_instance_name,
                        session_id,
                    ):
                        yield event

                if state.step_count >= state.max_steps and not state.final_answer:
                    await self._forced_synthesis(state, persistent_agent, system_prompt)

                if state.final_answer:
                    yield AgentEvent(
                        type="final_answer",
                        agent=agent_instance_name,
                        data={
                            "content": state.final_answer,
                            "connector_id": self.connector_id,
                            "connector_name": self.connector_name,
                            "steps_used": state.step_count,
                            "max_steps": state.max_steps,
                            "budget_extended": state.budget_extended,
                        },
                        session_id=session_id,
                    )
                elif state.error_message:
                    yield AgentEvent(
                        type="error",
                        agent=agent_instance_name,
                        data={"message": state.error_message},
                        session_id=session_id,
                    )

            except (ModelHTTPError, ModelAPIError) as e:
                status_code = getattr(e, "status_code", None)
                if isinstance(e, ModelHTTPError) and status_code == 429:
                    error_msg = (
                        f"AI service rate limited during {self.connector_name} investigation"
                    )
                elif isinstance(e, ModelHTTPError) and status_code == 401:
                    from meho_app.core.config import get_config as _get_cfg

                    _key_hint = {
                        "anthropic": "ANTHROPIC_API_KEY",
                        "openai": "OPENAI_API_KEY",
                        "ollama": "OLLAMA_BASE_URL",
                    }.get(_get_cfg().llm_provider, "API key")
                    error_msg = f"AI service authentication failed -- check {_key_hint}"
                elif isinstance(e, ModelHTTPError) and status_code and status_code >= 500:
                    error_msg = f"AI service error ({status_code}) during {self.connector_name} investigation"
                else:
                    error_msg = f"AI service unavailable during {self.connector_name} investigation"
                logger.exception(error_msg)
                yield AgentEvent(
                    type="error",
                    agent=agent_instance_name,
                    data={
                        "message": error_msg,
                        "error_source": "llm",
                        "error_type": "rate_limit" if status_code == 429 else "connection",
                        "severity": "transient"
                        if status_code in (429, 500, 502, 503)
                        else "permanent",
                        "trace_id": get_current_trace_id(),
                    },
                    session_id=session_id,
                )
            except UpstreamApiError as e:
                error_msg = f"{self.connector_name} API error ({e.status_code}): {e.message}"
                logger.exception(error_msg)
                yield AgentEvent(
                    type="error",
                    agent=agent_instance_name,
                    data={
                        "message": error_msg,
                        "error_source": "connector",
                        "error_type": "auth" if e.status_code == 401 else "connection",
                        "severity": "permanent" if e.status_code < 500 else "transient",
                        "connector_name": self.connector_name,
                        "trace_id": get_current_trace_id(),
                    },
                    session_id=session_id,
                )
            except Exception as e:
                error_msg = f"ReAct loop error: {e}"
                logger.exception(error_msg)
                yield AgentEvent(
                    type="error",
                    agent=agent_instance_name,
                    data={
                        "message": error_msg,
                        "error_source": "internal",
                        "trace_id": get_current_trace_id(),
                    },
                    session_id=session_id,
                )

            yield AgentEvent(
                type="agent_complete",
                agent=agent_instance_name,
                data={
                    "success": state.final_answer is not None,
                    "connector_id": self.connector_id,
                    "mode": "react",
                    "steps": state.step_count,
                    "max_steps": state.max_steps,
                },
                session_id=session_id,
            )

            logger.info(
                f"[{self.connector_name}] ReAct loop finished: "
                f"success={state.final_answer is not None}, "
                f"steps={state.step_count}/{state.max_steps}"
            )

        finally:
            if state is not None:
                self._finalize_react_run(state, _specialist_span, _specialist_ctx)
            else:
                _specialist_ctx.__exit__(None, None, None)
                _specialist_span.end()
