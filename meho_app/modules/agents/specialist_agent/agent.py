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
from meho_app.modules.agents.shared.topology_utils import (
    extract_entity_mentions,
    extract_key_attributes,
    parse_verification_evidence,
)
from meho_app.modules.agents.specialist_agent.compressor import compress_observation
from meho_app.modules.agents.specialist_agent.summarizer import _extract_key_param
from meho_app.modules.agents.sse.emitter import EventEmitter

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)
tracer = get_tracer("meho.specialist")

# Step budget constants (Phase 36: STEP-03)
EXTENSION_STEPS = 4  # Grant +4 steps on extension request
ABSOLUTE_MAX_STEPS = 12  # Hard cap, never exceeded


# =============================================================================
# Module-level helpers for topology pre-population (Phase 19 - INT-01/FLOW-01)
# =============================================================================


def _format_topology_context(results: list) -> str:
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
        lines.append(f"### {entity.name} ({entity.entity_type})")

        # Show connector info
        connector_info = (
            entity.connector_name
            if hasattr(entity, "connector_name") and entity.connector_name
            else (f"connector:{entity.connector_id}" if entity.connector_id else "External/Unknown")
        )
        lines.append(f"- **Managed by connector**: {connector_info}")

        # Show key attributes
        if entity.raw_attributes:
            key_attrs = extract_key_attributes(entity.raw_attributes)
            if key_attrs:
                lines.append(f"- **Key identifiers**: {key_attrs}")

        # Topology chain (first 5 items)
        if result.topology_chain:
            chain_str = " -> ".join(
                [f"{item.entity} ({item.entity_type})" for item in result.topology_chain[:5]]
            )
            lines.append(f"- **Topology chain**: {chain_str}")

        # Cross-System Identity (SAME_AS) section
        lines.append("")
        lines.append("#### Cross-System Identity (SAME_AS)")
        lines.append("This entity IS the same physical resource as:")
        for correlated in result.same_as_entities[:5]:
            corr_entity = correlated.entity
            connector_display = correlated.connector_name or correlated.connector_type
            connector_id_str = (
                str(corr_entity.connector_id) if corr_entity.connector_id else "unknown"
            )
            lines.append(
                f"  - **{corr_entity.name}** ({corr_entity.entity_type}) via {connector_display}"
            )
            lines.append(f"    connector_id: {connector_id_str}")
            # Parse confidence and evidence from verified_via
            confidence, evidence_summary = parse_verification_evidence(correlated.verified_via)
            lines.append(f"    Match confidence: {confidence}")
            if evidence_summary:
                lines.append(f"    Evidence: {evidence_summary}")
            # Extract key identifiers from the correlated entity
            if corr_entity.raw_attributes:
                corr_key_attrs = extract_key_attributes(corr_entity.raw_attributes)
                if corr_key_attrs:
                    lines.append(f"    Key identifiers: {corr_key_attrs}")
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
        sections: list[str] = []

        # --- Cached tables with column types ---
        if state.cached_tables:
            table_lines = [
                "## Available Cached Tables",
                "Query these directly with reduce_data (no need to call_operation again):\n",
            ]
            for table_name, info in state.cached_tables.items():
                row_count = info.get("row_count", "?")
                columns = info.get("columns", [])
                # Try to include column types from Arrow metadata if available
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
                table_lines.append(f"- `{table_name}`: {row_count} rows [{col_str}]")
            sections.append("\n".join(table_lines))

        # --- Discovered correlations (SAME_AS from topology) ---
        if state.discovered_correlations:
            corr_lines = [
                "## Discovered Correlations (SAME_AS)",
                "Cross-system identity mappings found during this investigation:\n",
            ]
            for corr in state.discovered_correlations:
                source = corr.get("source_entity", "?")
                target = corr.get("target_entity", "?")
                connector = corr.get("connector_name", corr.get("connector_id", "?"))
                confidence = corr.get("confidence", "")
                line = f"- **{source}** == **{target}** (via {connector})"
                if confidence:
                    line += f" [{confidence}]"
                corr_lines.append(line)
            sections.append("\n".join(corr_lines))

        # --- Discovered entities (cross-system) ---
        if state.discovered_entities:
            ent_lines = [
                "## Discovered Entities",
                "Key entities found during this investigation:\n",
            ]
            for ent in state.discovered_entities:
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
                ent_lines.append(line)
            sections.append("\n".join(ent_lines))

        if not sections:
            return ""

        body = "\n\n".join(sections)
        return f"\n<investigation_state>\n{body}\n</investigation_state>"

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
        from meho_app.modules.agents.react_agent.tools.call_operation import (
            CallOperationOutput,
        )
        from meho_app.modules.agents.react_agent.tools.lookup_topology import (
            LookupTopologyOutput,
        )

        # --- Extract SAME_AS correlations from lookup_topology ---
        if tool_name == "lookup_topology" and isinstance(observation, LookupTopologyOutput):
            if observation.found and observation.same_as_entities:
                # Each same_as entry is {"name": ..., "connector": ...}
                entity_name = ""
                if observation.entity and isinstance(observation.entity, dict):
                    entity_name = observation.entity.get("name", "")

                for corr in observation.same_as_entities:
                    correlation = {
                        "source_entity": entity_name,
                        "target_entity": corr.get("name", "?"),
                        "connector_name": corr.get("connector", "?"),
                    }
                    # Avoid duplicates
                    if correlation not in state.discovered_correlations:
                        state.discovered_correlations.append(correlation)

            # Extract entities from topology chain as discovered entities
            if observation.topology_chain:
                for chain_item in observation.topology_chain:
                    entity_dict = {
                        "name": chain_item.name,
                        "type": chain_item.entity_type or "",
                        "context": f"depth {chain_item.depth} in topology chain",
                    }
                    if chain_item.connector_type:
                        entity_dict["connector_type"] = chain_item.connector_type
                    # Avoid duplicates by name
                    if not any(
                        e.get("name") == entity_dict["name"]
                        for e in state.discovered_entities
                    ):
                        state.discovered_entities.append(entity_dict)

        # --- Update cached_tables from call_operation ---
        if tool_name == "call_operation" and isinstance(observation, CallOperationOutput):
            if observation.success and observation.table:
                table_info: dict[str, Any] = {
                    "table": observation.table,
                    "row_count": observation.row_count,
                    "columns": observation.columns or [],
                }
                # Carry column types for investigation state prompt
                if observation.column_types:
                    table_info["column_types"] = observation.column_types
                state.cached_tables[observation.table] = table_info

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
        from meho_app.modules.agents.react_agent.tools import TOOL_REGISTRY
        from meho_app.modules.agents.specialist_agent.models import ReActStep
        from meho_app.modules.agents.specialist_agent.state import SpecialistReActState

        # OTEL specialist run span (OBSV-03) - child of orchestrator.dispatch
        # Manual span management for same reason as orchestrator: avoids re-indenting 600+ lines.
        _specialist_span = tracer.start_span(
            "meho.specialist.run",
            attributes={
                "meho.connector_id": self.connector_id,
                "meho.connector_name": self.connector_name,
                "meho.max_steps": self._config.max_steps,
                "meho.session_id": session_id or "",
            },
        )
        _specialist_ctx = use_span(_specialist_span, end_on_exit=False)
        _specialist_ctx.__enter__()

        ctx = context or {}
        agent_instance_name = f"{self.agent_name}_{self.connector_id}"

        # Load cached tables from session for multi-turn awareness
        cached_tables: dict[str, Any] = {}
        if session_id and hasattr(self.dependencies, "unified_executor"):
            try:
                table_infos = await self.dependencies.unified_executor.get_session_table_info_async(
                    session_id
                )
                cached_tables = {t["table"]: t for t in table_infos}
            except Exception as e:
                logger.warning(f"Failed to load cached tables: {e}")

        # Initialize ReAct state
        # Phase 99: Honor max_steps_override from orchestrator routing classification
        effective_max_steps = ctx.get("max_steps_override") or self._config.max_steps
        state = SpecialistReActState(
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

        # Fetch operator memories for context injection (Phase 99 - INV-05)
        # Only operator-provided memories are injected into the system prompt.
        # Auto-extracted memories are available via recall_memory tool.
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

        # Pre-populate topology context with SAME_AS edges (Phase 19 - INT-01/FLOW-01)
        topology_context = ""
        try:
            session_maker = create_openapi_session_maker()
            async with session_maker() as topo_db:
                from meho_app.modules.topology.schemas import LookupTopologyInput
                from meho_app.modules.topology.service import TopologyService

                topo_service = TopologyService(topo_db)
                tenant_id = self.dependencies.user_context.tenant_id

                entity_mentions = extract_entity_mentions(user_message)
                if entity_mentions:
                    context_parts = []
                    for mention in entity_mentions[:5]:  # Limit lookups
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

        # Create emitter with connector routing metadata
        emitter = EventEmitter(agent_name=agent_instance_name, session_id=session_id)
        event_queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        emitter.set_queue(event_queue)

        # Wire transcript collector
        collector = get_transcript_collector()
        if collector:
            emitter.set_transcript_collector(collector)

        # Emit agent_start
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

        # Build system prompt (reuse _build_system_prompt, updated for ReAct)
        system_prompt = self._build_system_prompt(
            user_message, memory_context=memory_context, topology_context=topology_context
        )

        # Build deps wrapper for tool execution (match ReactAgent's AgentDeps pattern)
        from meho_app.modules.agents.react_agent.agent import AgentDeps

        tool_deps = AgentDeps(
            external_deps=self.dependencies,
            agent_config=self._config,
            session_id=session_id,
        )

        # Connector-scoped tools: only the tools relevant to a connector-scoped agent
        # Exclude list_connectors (already scoped) and invalidate_topology (not needed)
        SPECIALIST_TOOLS = {
            "search_operations",
            "call_operation",
            "reduce_data",
            "lookup_topology",
            "search_knowledge",
            "store_memory",  # Phase 11: operator memory management
            "forget_memory",  # Phase 11: operator memory management
            "recall_memory",  # Phase 99: on-demand memory recall
            "dns_resolve",   # Phase 96.1: Network diagnostics
            "tcp_probe",     # Phase 96.1: Network diagnostics
            "http_probe",    # Phase 96.1: Network diagnostics
            "tls_check",     # Phase 96.1: Network diagnostics
        }

        # Phase 89: Feature-flag filtering -- remove tools for disabled modules
        from meho_app.core.feature_flags import get_feature_flags

        _flags = get_feature_flags()
        if not _flags.knowledge:
            SPECIALIST_TOOLS.discard("search_knowledge")
        if not _flags.topology:
            SPECIALIST_TOOLS.discard("lookup_topology")
        if not _flags.memory:
            SPECIALIST_TOOLS.discard("store_memory")
            SPECIALIST_TOOLS.discard("forget_memory")
            SPECIALIST_TOOLS.discard("recall_memory")
        if not _flags.network_diagnostics:
            SPECIALIST_TOOLS.discard("dns_resolve")
            SPECIALIST_TOOLS.discard("tcp_probe")
            SPECIALIST_TOOLS.discard("http_probe")
            SPECIALIST_TOOLS.discard("tls_check")

        # ==================================================================
        # PERSISTENT AGENT (Phase 35: Stateful Loop)
        # ==================================================================
        # Create ONE PydanticAI Agent before the loop. Reused across all
        # steps with message_history for incremental conversation.
        # Anthropic models get cache_instructions=True for prompt caching.
        model_name = self._config.model.name
        model_settings = {}
        if self._config.model.temperature is not None:
            model_settings["temperature"] = self._config.model.temperature

        from meho_app.modules.agents.agent_factories import get_model_settings

        factory_settings = get_model_settings(task_type="specialist")
        # Merge temperature override with provider-specific settings
        if factory_settings is not None:
            # Anthropic: factory returns AnthropicModelSettings, temperature set via dict merge
            effective_settings = factory_settings
        else:
            # Non-Anthropic: pass temperature dict or None
            effective_settings = model_settings if model_settings else None

        persistent_agent: Agent[None, ReActStep] = Agent(
            model_name,
            output_type=ReActStep,
            instrument=InstrumentationSettings(),
            model_settings=effective_settings,
        )

        # Build the FIRST user message (stable prefix).
        # memory_context and topology_context are in the system prompt.
        # Tables context lives in investigation_state (injected into instructions each step).
        first_message = (
            f"Operator Question: {user_message}\n\n"
            f"Step 1 of {state.max_steps}. What should you do next?"
        )

        # pending_user_message tracks what to send on the next agent.run() call.
        # First iteration: full context (stable prefix). Subsequent: observation only.
        pending_user_message = first_message

        try:
            # ==================================================================
            # REACT LOOP
            # ==================================================================
            while not state.is_complete() and state.step_count < state.max_steps:
                # OTEL step span (OBSV-03) - child of specialist.run
                _step_num = state.step_count + 1
                _step_span = tracer.start_span(
                    f"meho.specialist.step.{_step_num}",
                    attributes={
                        "meho.step_number": _step_num,
                        "meho.connector_id": self.connector_id,
                    },
                )
                _step_ctx = use_span(_step_span, end_on_exit=False)
                _step_ctx.__enter__()

                # REASON: Send pending message to persistent agent (Phase 35)
                # Step 1: pending_user_message = first_message (full context)
                # Step 2+: pending_user_message = observation text or error text
                #
                # Phase 89.1 (D-05): Instructions are now 100% stable across steps.
                # Investigation state (cached tables, correlations) is appended to
                # the user message instead of instructions, enabling full Anthropic
                # prompt prefix caching after step 1.
                _investigation_state = self._build_investigation_state(state)
                _effective_instructions = system_prompt  # Stable! 100% cache hit after step 1

                _llm_start = time.perf_counter()
                # Append investigation state to user message (not instructions) for cache stability
                _current_msg = pending_user_message
                if _investigation_state:
                    _current_msg = pending_user_message + "\n\n" + _investigation_state
                _current_instructions = _effective_instructions  # capture for lambda closure
                result = await retry_llm_call(
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
                # Capture message history for next step
                state.message_history = list(result.all_messages())

                # Emit DetailedEvent for transcript persistence (replaces
                # infer_structured's internal emission -- Phase 35)
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
                        model_name, _prompt_tokens, _completion_tokens,
                        _cache_read, _cache_write,
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
                        timestamp=datetime.now(tz=UTC),
                        type="llm_call",
                        summary=f"LLM call to {model_name} (ReActStep) [step {state.step_count + 1}]",
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

                # Emit thought
                await emitter.thought(react_step.thought)
                # Drain events
                while not event_queue.empty():
                    yield event_queue.get_nowait()

                # Check for final answer
                if react_step.response_type == "final_answer" and react_step.final_answer:
                    state.final_answer = react_step.final_answer
                    state.steps_executed.append(f"final_answer (step {state.step_count + 1})")
                    _step_ctx.__exit__(None, None, None)
                    _step_span.end()
                    break

                # Validate action
                if not react_step.action or react_step.action not in SPECIALIST_TOOLS:
                    error_text = (
                        f"Error: Invalid tool '{react_step.action}'. "
                        f"Use one of: {', '.join(sorted(SPECIALIST_TOOLS))}"
                    )
                    await emitter.error(
                        f"Invalid tool: {react_step.action}. "
                        f"Available: {', '.join(sorted(SPECIALIST_TOOLS))}"
                    )
                    # Backward compat: still populate flat scratchpad
                    state.scratchpad.append(error_text)
                    # Stateful loop: set error as next user message (Phase 35)
                    pending_user_message = error_text
                    state.step_count += 1
                    while not event_queue.empty():
                        yield event_queue.get_nowait()
                    _step_ctx.__exit__(None, None, None)
                    _step_span.end()
                    continue

                # Extract typed action as dict for handler compatibility.
                # The discriminated union guarantees required fields are present.
                if react_step.action_input is not None:
                    action_input = react_step.action_input.model_dump(exclude={"tool"})
                else:
                    action_input = {}

                # LOOP DETECTION: Check for duplicate action (Research Pitfall 2)
                if state.has_duplicate_action(react_step.action, action_input):
                    loop_text = (
                        f"LOOP DETECTED: You already called {react_step.action} "
                        f"with these exact parameters. You must provide a final answer now."
                    )
                    logger.warning(
                        f"Loop detected: {react_step.action} called with same args twice"
                    )
                    # Backward compat: still populate flat scratchpad
                    state.scratchpad.append(loop_text)
                    # Stateful loop: set loop detection as next user message (Phase 35)
                    pending_user_message = loop_text
                    state.step_count += 1
                    while not event_queue.empty():
                        yield event_queue.get_nowait()
                    _step_ctx.__exit__(None, None, None)
                    _step_span.end()
                    continue

                state.record_action(react_step.action, action_input)

                # Step budget extension (Phase 36: STEP-02)
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

                # SERVER-SIDE INJECTION: Always inject connector_id for
                # connector-scoped tools, regardless of what the LLM outputs
                if react_step.action in (
                    "search_operations",
                    "call_operation",
                    "search_knowledge",
                    "store_memory",
                    "forget_memory",
                    "recall_memory",
                ):
                    action_input["connector_id"] = self.connector_id

                # NOTE: Pre-flight checks for missing query/operation_id removed.
                # The typed discriminated union on action_input enforces required
                # fields at the PydanticAI output parsing level. If the LLM omits
                # a required field, PydanticAI retries automatically without
                # consuming a step from MEHO's budget.

                # ──────────────────────────────────────────────
                # TRUST CLASSIFICATION (Phase 5)
                # ──────────────────────────────────────────────
                # Initialize approval tracking at top of iteration
                # (always defined for audit outcome logging below)
                approval_id: str | None = None
                http_method = action_input.get("method")

                # Look up per-endpoint/operation trust override from DB
                trust_override = None
                if react_step.action == "call_operation":
                    operation_id_for_lookup = action_input.get("operation_id", "")
                    try:
                        from meho_app.api.database import create_openapi_session_maker
                        from meho_app.modules.agents.approval.trust_classifier import (
                            safety_level_to_trust_tier,
                        )

                        if self.connector_type == "rest":
                            # REST connector: look up EndpointDescriptor safety_level
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
                                    trust_override = safety_level_to_trust_tier(
                                        endpoint.safety_level
                                    )
                        else:
                            # Typed connector: look up ConnectorOperation safety_level
                            session_maker = create_openapi_session_maker()
                            async with session_maker() as db:
                                from meho_app.modules.connectors.repositories.operation_repository import (
                                    ConnectorOperationRepository,
                                )

                                repo = ConnectorOperationRepository(db)
                                operation = await repo.get_operation_by_op_id(
                                    connector_id=self.connector_id,
                                    operation_id=operation_id_for_lookup,
                                )
                                if operation and operation.safety_level:
                                    trust_override = safety_level_to_trust_tier(
                                        operation.safety_level
                                    )
                    except Exception as override_err:
                        logger.warning(f"Failed to look up trust override: {override_err}")
                        # Continue without override (safe fallback)

                trust_tier = classify_operation(
                    connector_type=self.connector_type,
                    operation_id=action_input.get("operation_id", react_step.action),
                    http_method=http_method,
                    override=trust_override,
                )

                if needs_approval(trust_tier) and react_step.action == "call_operation":
                    # PAUSE: Create DB approval request and register in-process event
                    from meho_app.modules.agents.approval.repository import ApprovalStore

                    # Map TrustTier to DangerLevel for DB compatibility
                    _TRUST_TO_DANGER = {
                        TrustTier.READ: "safe",
                        TrustTier.WRITE: "dangerous",
                        TrustTier.DESTRUCTIVE: "critical",
                    }
                    db_danger_level = _TRUST_TO_DANGER.get(trust_tier, "dangerous")

                    approval_request = None
                    try:
                        if self.dependencies.db_session:
                            store = ApprovalStore(self.dependencies.db_session)
                            from uuid import UUID as _UUID

                            from meho_app.core.config import get_config

                            # Phase 75: automated sessions use 24h expiry (1440 min)
                            _is_automated = self.dependencies.session_type != "interactive"
                            _expiry = 1440 if _is_automated else get_config().approval_expiry_minutes

                            op_id = action_input.get("operation_id", "")
                            _approval_desc = (
                                f"{op_id} on {self.connector_name}"
                                if op_id
                                else f"{react_step.action} on {self.connector_name}"
                            )

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
                                user_message=user_message,
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

                    # Register in-process pending approval
                    pending = register_pending(
                        session_id=session_id or "",
                        tool_name=react_step.action,
                        tool_args=action_input,
                        approval_id=approval_id,
                    )

                    # Emit approval_required SSE event
                    await emitter.approval_required(
                        tool=react_step.action,
                        args=action_input,
                        danger_level=trust_tier.value,
                        description=_approval_desc,
                        approval_id=approval_id,
                    )

                    while not event_queue.empty():
                        yield event_queue.get_nowait()

                    # Phase 75: Fire-and-forget notification for automated sessions
                    _notif_targets = getattr(self.dependencies, "notification_targets", None)
                    if _notif_targets and _is_automated:
                        from meho_app.modules.connectors.notification_dispatcher import (
                            dispatch_approval_notification,
                        )

                        asyncio.create_task(dispatch_approval_notification(
                            notification_targets=_notif_targets,
                            session_id=session_id or "",
                            session_title=_approval_desc,
                            tool_name=react_step.action,
                            danger_level=db_danger_level,
                            tenant_id=self.dependencies.user_context.tenant_id or "",
                            trigger_source=self.dependencies.trigger_type or "automated",
                        ))

                    # WAIT: Send keepalive every 15s until operator responds
                    try:
                        while not pending.event.is_set():
                            try:
                                await asyncio.wait_for(pending.event.wait(), timeout=15.0)
                            except TimeoutError:
                                await emitter.keepalive()
                                while not event_queue.empty():
                                    yield event_queue.get_nowait()
                    finally:
                        cleanup_pending(session_id or "")

                    # RESUME: Check approval decision
                    if pending.approved:
                        await emitter.approval_resolved(tool=react_step.action, approved=True)
                        while not event_queue.empty():
                            yield event_queue.get_nowait()
                        # Fall through to normal tool execution below
                    else:
                        # DENIED: Record in scratchpad, continue reasoning
                        await emitter.approval_resolved(tool=react_step.action, approved=False)
                        denial_msg = (
                            f"Operator DENIED the {trust_tier.value} operation "
                            f"'{action_input.get('operation_id', react_step.action)}'. "
                            f"You cannot execute this operation. Try a read-only "
                            f"alternative or explain what you cannot do."
                        )
                        # Backward compat: still populate flat scratchpad
                        state.scratchpad.append(f"Observation: {denial_msg}")
                        # Stateful loop: set denial as next user message (Phase 35)
                        pending_user_message = denial_msg
                        state.step_count += 1
                        state.steps_executed.append(
                            f"denied:{react_step.action} (step {state.step_count})"
                        )

                        # Emit audit entry for denial
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

                        while not event_queue.empty():
                            yield event_queue.get_nowait()
                        _step_ctx.__exit__(None, None, None)
                        _step_span.end()
                        continue  # Back to top of ReAct loop

                # ACT: Execute the tool
                await emitter.action(react_step.action, action_input)
                while not event_queue.empty():
                    yield event_queue.get_nowait()

                tool_class = TOOL_REGISTRY[react_step.action]
                tool_instance = tool_class()

                try:
                    validated_input = tool_class.validate_input(action_input)
                    observation = await tool_instance.execute(validated_input, tool_deps, emitter)

                    # AUDIT OUTCOME (Phase 5): Log success for approved operations
                    if needs_approval(trust_tier) and react_step.action == "call_operation":
                        outcome_summary = (
                            observation[:200]
                            if isinstance(observation, str)
                            else str(observation)[:200]
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

                        # Persist execution outcome to DB audit trail (Phase 7.1)
                        try:
                            if self.dependencies.db_session and approval_id:
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

                        while not event_queue.empty():
                            yield event_queue.get_nowait()

                except UpstreamApiError as tool_err:
                    if tool_err.status_code == 401:
                        observation = f"Tool error: {self.connector_name} authentication failed -- verify credentials in connector settings"
                    elif tool_err.status_code >= 500:
                        observation = f"Tool error: {self.connector_name} server error ({tool_err.status_code}) -- the target system may be down"
                    elif tool_err.status_code == 429:
                        observation = (
                            f"Tool error: {self.connector_name} rate limited -- too many requests"
                        )
                    else:
                        observation = f"Tool error: {self.connector_name} returned error {tool_err.status_code}"
                    logger.warning(f"Tool {react_step.action} connector error: {tool_err}")

                    # AUDIT OUTCOME (Phase 5): Log failure for approved operations
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

                        # Persist execution failure to DB audit trail (Phase 7.1)
                        try:
                            if self.dependencies.db_session and approval_id:
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

                        while not event_queue.empty():
                            yield event_queue.get_nowait()

                except ValidationError as val_err:
                    # Format hints for common tool input mistakes
                    _tool_format_hints: dict[str, str] = {
                        "call_operation": (
                            'Expected: {"connector_id": "<uuid>", "operation_id": "<id>", '
                            '"parameter_sets": [{}]}'
                        ),
                        "reduce_data": (
                            'Expected: {"sql": "SELECT ... FROM <table>"}'
                        ),
                        "search_operations": (
                            'Expected: {"connector_id": "<uuid>", "query": "<search terms>"}'
                        ),
                        "lookup_topology": (
                            'Expected: {"query": "<entity name>"}'
                        ),
                        "store_memory": (
                            'Expected: {"title": "<short title>", "body": "<details>", '
                            '"memory_type": "config|incident|architecture"}'
                        ),
                        "search_knowledge": (
                            'Expected: {"query": "<search terms>"}'
                        ),
                    }
                    format_hint = _tool_format_hints.get(react_step.action, "")

                    missing = [e["loc"][-1] for e in val_err.errors() if e["type"] == "missing"]
                    if missing:
                        observation = (
                            f"Tool {react_step.action} missing required parameters: {', '.join(str(f) for f in missing)}. "
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

                    # AUDIT OUTCOME (Phase 5): Log failure for approved operations
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

                        # Persist execution failure to DB audit trail (Phase 7.1)
                        try:
                            if self.dependencies.db_session and approval_id:
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

                        while not event_queue.empty():
                            yield event_queue.get_nowait()

                # EXTRACT: Populate investigation state from structured tool output
                # (before compression destroys the Pydantic model structure)
                self._extract_investigation_data(state, react_step.action, observation)

                # OBSERVE: Compress observation to essential signal before scratchpad (Phase 33)
                observation_str = await compress_observation(
                    observation=observation,
                    tool_name=react_step.action,
                    thought=react_step.thought,
                )
                # Backward compat: still populate flat scratchpad + completed_steps
                state.add_observation(
                    react_step.action,
                    observation_str,
                    action_input_key=_extract_key_param(react_step.action, action_input),
                )
                state.step_count += 1
                state.steps_executed.append(f"{react_step.action} (step {state.step_count})")

                # Build step counter with extension visibility (Phase 36)
                next_step = state.step_count + 1
                if state.budget_extended:
                    step_info = (
                        f"Step {next_step} of {state.max_steps} "
                        f"(extended from {state.max_steps - EXTENSION_STEPS})"
                    )
                else:
                    step_info = f"Step {next_step} of {state.max_steps}"

                pending_user_message = (
                    f"Observation from {react_step.action}: {observation_str}\n\n"
                    f"{step_info}. What should you do next?"
                )

                # Near-budget nudge on last 2 steps (Phase 36)
                steps_remaining = state.max_steps - next_step
                if steps_remaining <= 2 and not state.budget_extended:
                    pending_user_message += (
                        "\n\nBudget running low -- consider synthesizing your findings "
                        "or requesting an extension with extend_budget."
                    )
                elif steps_remaining <= 2 and state.budget_extended:
                    pending_user_message += (
                        "\n\nBudget running low -- consider synthesizing your findings."
                    )

                await emitter.observation(react_step.action, observation_str[:500])

                # Emit step_progress for UI
                await emitter.emit(
                    "step_progress",
                    {
                        "step": state.step_count,
                        "max_steps": state.max_steps,
                        "connector_id": self.connector_id,
                        "connector_name": self.connector_name,
                    },
                )

                # Drain events
                while not event_queue.empty():
                    yield event_queue.get_nowait()

                # Close OTEL step span at end of normal iteration (OBSV-03)
                _step_ctx.__exit__(None, None, None)
                _step_span.end()

            # ==================================================================
            # POST-LOOP: Budget exhaustion or completion
            # ==================================================================
            if state.step_count >= state.max_steps and not state.final_answer:
                # Forced synthesis -- one free extra step (Phase 36: STEP-03)
                _synth_span = tracer.start_span(
                    "meho.specialist.synthesis",
                    attributes={
                        "meho.connector_id": self.connector_id,
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
                    _synth_msg = synthesis_prompt
                    _synth_investigation_state = self._build_investigation_state(state)
                    _synth_instructions = system_prompt + _synth_investigation_state
                    synthesis_result = await retry_llm_call(
                        lambda msg=_synth_msg, instr=_synth_instructions: persistent_agent.run(
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
                        # LLM returned action instead of final_answer -- use thought as fallback
                        logger.warning(
                            f"[{self.connector_name}] Synthesis returned action instead of final_answer"
                        )
                        state.final_answer = synth_step.thought
                except Exception as synth_err:
                    logger.warning(f"[{self.connector_name}] Forced synthesis failed: {synth_err}")
                    # Fall back to raw observations summary
                    observations_summary = state.get_observations_summary()
                    state.final_answer = (
                        f"Note: I reached my investigation step limit, "
                        f"but here's what I found:\n\n{observations_summary}"
                    )

                state.steps_executed.append(f"forced_synthesis (after step {state.step_count})")

                _synth_ctx.__exit__(None, None, None)
                _synth_span.end()

            # Emit final_answer
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
                error_msg = f"AI service rate limited during {self.connector_name} investigation"
            elif isinstance(e, ModelHTTPError) and status_code == 401:
                from meho_app.core.config import get_config as _get_cfg
                _key_hint = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "ollama": "OLLAMA_BASE_URL"}.get(_get_cfg().llm_provider, "API key")
                error_msg = f"AI service authentication failed -- check {_key_hint}"
            elif isinstance(e, ModelHTTPError) and status_code and status_code >= 500:
                error_msg = (
                    f"AI service error ({status_code}) during {self.connector_name} investigation"
                )
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
                    "severity": "transient" if status_code in (429, 500, 502, 503) else "permanent",
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

        # Emit agent_complete
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

        # Close OTEL specialist run span (OBSV-03)
        _specialist_span.set_attribute("meho.steps_taken", state.step_count)
        _specialist_span.set_attribute(
            "meho.status",
            "success" if state.final_answer is not None else (state.error_message or "unknown"),
        )
        _specialist_ctx.__exit__(None, None, None)
        _specialist_span.end()
