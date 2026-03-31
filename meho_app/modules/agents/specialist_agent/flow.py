# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Flow definition for SpecialistAgent - binds nodes together.

This module defines the deterministic workflow that orchestrates
the individual nodes. The flow is linear and predictable:

    search_intent -> search_operations -> select_operation ->
    call_operation -> reduce_data -> (return raw JSON data)

The LLM decides WHAT (structured outputs), code enforces HOW and WHEN.
The orchestrator handles final formatting into markdown.

This flow creates its own WorkflowState with the skill_content field so that
nodes can inject domain knowledge into their LLM prompts. The workflow logic
mirrors shared/flow.py exactly -- only the state creation differs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.agents.specialist_agent.models import (
    NoRelevantOperation,
    OperationSelection,
)
from meho_app.modules.agents.specialist_agent.nodes import (
    CallOperationNode,
    ReduceDataNode,
    SearchIntentNode,
    SearchOperationsNode,
    SelectOperationNode,
)
from meho_app.modules.agents.specialist_agent.state import (
    WorkflowResult,
    WorkflowState,
)

if TYPE_CHECKING:
    from meho_app.modules.agents.persistence import OrchestratorSessionState
    from meho_app.modules.agents.sse.emitter import EventEmitter

logger = get_logger(__name__)


async def execute_workflow(
    user_goal: str,
    connector_id: str,
    connector_name: str,
    connector_type: str,
    deps: Any,
    emitter: EventEmitter | None = None,
    session_id: str | None = None,
    session_state: OrchestratorSessionState | None = None,
    skill_content: str = "",
) -> WorkflowResult:
    """Execute the deterministic workflow for specialist agent.

    This is the main entry point that orchestrates all nodes in sequence.
    Each node does one thing well, and this function wires them together.

    Creates a specialist WorkflowState with skill_content so nodes can
    inject domain knowledge into their LLM prompts.

    Args:
        user_goal: The user's question/goal.
        connector_id: Connector to query.
        connector_name: Human-readable connector name.
        connector_type: Type of connector (kubernetes, vmware, etc.).
        deps: MEHODependencies for accessing services.
        emitter: Optional event emitter for SSE streaming.
        session_id: Session ID for cache isolation and multi-turn context.
        session_state: Persistent session state for multi-turn context.
        skill_content: Markdown skill content for domain knowledge injection.

    Returns:
        WorkflowResult with findings or error.

    Flow:
        1. SearchIntentNode - LLM decides what to search (with skill context)
        2. SearchOperationsNode - Code executes search
        3. SelectOperationNode - LLM picks operation (with skill context)
        4. CallOperationNode - Code executes operation
        5. ReduceDataNode - Code fetches cached data if needed
        6. Return markdown data from ReduceDataNode
    """
    # Load cached tables from session for multi-turn context awareness
    cached_tables: dict[str, Any] = {}
    if session_id and hasattr(deps, "unified_executor"):
        try:
            table_infos = await deps.unified_executor.get_session_table_info_async(session_id)
            # Convert list to dict for easy lookup by table name
            cached_tables = {t["table"]: t for t in table_infos}
            if cached_tables:
                logger.info(
                    f"[{connector_name}] Loaded {len(cached_tables)} cached tables: "
                    f"{list(cached_tables.keys())}"
                )
        except Exception as e:
            logger.warning(f"Failed to load cached tables for session {session_id}: {e}")

    # Initialize state with skill_content for node-level prompt injection
    state = WorkflowState(
        user_goal=user_goal,
        connector_id=connector_id,
        connector_name=connector_name,
        session_state=session_state,
        session_id=session_id,
        cached_tables=cached_tables,
        skill_content=skill_content,
    )

    try:
        # Step 1: LLM decides what to search (skill_content injected via state)
        search_intent_node = SearchIntentNode(
            connector_name=connector_name,
            connector_type=connector_type,
        )
        search_intent = await search_intent_node.run(state, emitter)

        # ============================================================
        # EARLY EXIT: Cached data query detected
        # Skip operation selection and go directly to ReduceData
        # ============================================================
        if (
            search_intent.use_cached_data
            and search_intent.cached_table_name
            and search_intent.cached_table_name in cached_tables
        ):
            cached_table = cached_tables[search_intent.cached_table_name]

            if emitter:
                await emitter.thought(
                    f"Using cached '{search_intent.cached_table_name}' table "
                    f"({cached_table.get('row_count', 0)} rows)"
                )

            # Build virtual call_result for ReduceDataNode
            call_result = {
                "data_available": False,
                "table": search_intent.cached_table_name,
                "row_count": cached_table.get("row_count", 0),
                "columns": cached_table.get("columns", []),
                "schema": {
                    "entity_type": cached_table.get("entity_type"),
                    "identifier": cached_table.get("identifier_field"),
                    "display_name": cached_table.get("display_name_field"),
                },
                "from_cache": True,
            }

            # Go directly to reduce_data (skip steps 2-4)
            reduce_node = ReduceDataNode(
                connector_name=connector_name,
                deps=deps,
            )
            data = await reduce_node.run(state, emitter, call_result)

            return WorkflowResult(
                success=True,
                findings=data,
                steps_executed=state.steps_executed,
            )

        # ============================================================
        # NORMAL FLOW: Continue with operation search/selection
        # ============================================================

        # Step 2: Code executes search
        search_ops_node = SearchOperationsNode(
            connector_id=connector_id,
            connector_name=connector_name,
            deps=deps,
        )
        operations = await search_ops_node.run(state, emitter, search_intent.query)

        # Early exit: No operations found
        if not operations:
            if emitter:
                await emitter.thought(f"No operations found for '{search_intent.query}'")
            return WorkflowResult(
                success=True,
                findings=f"No operations found on {connector_name} for '{search_intent.query}'",
                steps_executed=state.steps_executed,
            )

        # Step 3: LLM picks operation (skill_content injected via state)
        select_op_node = SelectOperationNode(connector_name=connector_name)
        selection = await select_op_node.run(state, emitter, operations)

        # Early exit: No relevant operation
        if isinstance(selection, NoRelevantOperation):
            return WorkflowResult(
                success=True,
                findings=f"No relevant operation on {connector_name}: {selection.reasoning}",
                steps_executed=state.steps_executed,
            )

        # Type narrowing - we have an OperationSelection
        assert isinstance(selection, OperationSelection)  # noqa: S101 -- runtime assertion for invariant checking

        # Step 4: Code executes operation
        call_op_node = CallOperationNode(
            connector_id=connector_id,
            connector_name=connector_name,
            deps=deps,
            session_id=session_id,
        )
        call_result = await call_op_node.run(
            state, emitter, selection.operation_id, selection.parameters
        )

        # Step 5: Code fetches cached data if needed
        reduce_node = ReduceDataNode(
            connector_name=connector_name,
            deps=deps,
            session_id=session_id,
        )
        data = await reduce_node.run(state, emitter, call_result)

        # Step 6: Return markdown data as findings for the orchestrator.
        return WorkflowResult(
            success=True,
            findings=data,
            steps_executed=state.steps_executed,
        )

    except Exception as e:
        logger.exception(f"Workflow error: {e}")
        if emitter:
            await emitter.error(str(e))
        return WorkflowResult(
            success=False,
            findings="",
            steps_executed=state.steps_executed,
            error=str(e),
        )
