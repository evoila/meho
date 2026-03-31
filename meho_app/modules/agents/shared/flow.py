# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Shared flow logic for MEHO agents.

This module contains the shared execute_workflow function that orchestrates
the deterministic workflow for connector-scoped agents. The workflow
is parametrized with agent-specific node classes and models.

The flow is linear and predictable:
    search_intent → search_operations → select_operation →
    call_operation → reduce_data → (return markdown)

The LLM decides WHAT (structured outputs), code enforces HOW and WHEN.
ReduceDataNode always returns markdown (tables or JSON code blocks).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from meho_app.core.otel import get_logger
from meho_app.modules.agents.shared.state import WorkflowResult, WorkflowState

if TYPE_CHECKING:
    from meho_app.modules.agents.persistence import OrchestratorSessionState
    from meho_app.modules.agents.sse.emitter import EventEmitter

logger = get_logger(__name__)


class SearchIntentNodeProtocol(Protocol):
    """Protocol for SearchIntentNode."""

    def __init__(self, connector_name: str, connector_type: str) -> None: ...
    async def run(self, state: WorkflowState, emitter: Any) -> Any: ...


class SearchOperationsNodeProtocol(Protocol):
    """Protocol for SearchOperationsNode."""

    def __init__(self, connector_id: str, connector_name: str, deps: Any) -> None: ...
    async def run(self, state: WorkflowState, emitter: Any, query: str) -> list[Any]: ...


class SelectOperationNodeProtocol(Protocol):
    """Protocol for SelectOperationNode."""

    def __init__(self, connector_name: str) -> None: ...
    async def run(self, state: WorkflowState, emitter: Any, operations: list[Any]) -> Any: ...


class CallOperationNodeProtocol(Protocol):
    """Protocol for CallOperationNode."""

    def __init__(
        self,
        connector_id: str,
        connector_name: str,
        deps: Any,
        session_id: str | None = None,
    ) -> None: ...
    async def run(
        self,
        state: WorkflowState,
        emitter: Any,
        operation_id: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]: ...


class ReduceDataNodeProtocol(Protocol):
    """Protocol for ReduceDataNode."""

    def __init__(
        self,
        connector_name: str,
        deps: Any,
        session_id: str | None = None,
    ) -> None: ...
    async def run(
        self,
        state: WorkflowState,
        emitter: Any,
        call_result: dict[str, Any],
    ) -> str: ...


async def execute_workflow(
    user_goal: str,
    connector_id: str,
    connector_name: str,
    connector_type: str,
    deps: Any,
    *,
    # Agent-specific node classes
    search_intent_node_cls: type[SearchIntentNodeProtocol],
    search_operations_node_cls: type[SearchOperationsNodeProtocol],
    select_operation_node_cls: type[SelectOperationNodeProtocol],
    call_operation_node_cls: type[CallOperationNodeProtocol],
    reduce_data_node_cls: type[ReduceDataNodeProtocol],
    # Agent-specific model classes
    no_relevant_operation_cls: type,
    operation_selection_cls: type,
    # Optional parameters
    emitter: EventEmitter | None = None,
    session_id: str | None = None,
    session_state: OrchestratorSessionState | None = None,
) -> WorkflowResult:
    """Execute the deterministic workflow.

    This is the main entry point that orchestrates all nodes in sequence.
    Each node does one thing well, and this function wires them together.

    Args:
        user_goal: The user's question/goal.
        connector_id: Connector to query.
        connector_name: Human-readable connector name.
        connector_type: Type of connector (kubernetes, vmware, etc.).
        deps: MEHODependencies for accessing services.
        search_intent_node_cls: Class for SearchIntentNode.
        search_operations_node_cls: Class for SearchOperationsNode.
        select_operation_node_cls: Class for SelectOperationNode.
        call_operation_node_cls: Class for CallOperationNode.
        reduce_data_node_cls: Class for ReduceDataNode.
        no_relevant_operation_cls: Model class for NoRelevantOperation.
        operation_selection_cls: Model class for OperationSelection.
        emitter: Optional event emitter for SSE streaming.
        session_id: Session ID for cache isolation and multi-turn context.
        session_state: Persistent session state for multi-turn context.

    Returns:
        WorkflowResult with findings or error.

    Flow:
        1. SearchIntentNode - LLM decides what to search
        2. SearchOperationsNode - Code executes search
        3. SelectOperationNode - LLM picks operation (may exit early)
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

    # Initialize state
    state = WorkflowState(
        user_goal=user_goal,
        connector_id=connector_id,
        connector_name=connector_name,
        session_state=session_state,
        session_id=session_id,
        cached_tables=cached_tables,
    )

    try:
        # Step 1: LLM decides what to search
        search_intent_node = search_intent_node_cls(
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
            # Set data_available=False to trigger reduce_data SQL fetching
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
                "from_cache": True,  # Flag for debugging
            }

            # Go directly to reduce_data (skip steps 2-4)
            reduce_node = reduce_data_node_cls(
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
        search_ops_node = search_operations_node_cls(
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

        # Step 3: LLM picks operation
        select_op_node = select_operation_node_cls(connector_name=connector_name)
        selection = await select_op_node.run(state, emitter, operations)

        # Early exit: No relevant operation
        if isinstance(selection, no_relevant_operation_cls):
            return WorkflowResult(
                success=True,
                findings=f"No relevant operation on {connector_name}: {selection.reasoning}",
                steps_executed=state.steps_executed,
            )

        # Type narrowing - we have an OperationSelection
        assert isinstance(selection, operation_selection_cls)  # noqa: S101 -- runtime assertion for invariant checking

        # Step 4: Code executes operation
        call_op_node = call_operation_node_cls(
            connector_id=connector_id,
            connector_name=connector_name,
            deps=deps,
            session_id=session_id,
        )
        call_result = await call_op_node.run(
            state, emitter, selection.operation_id, selection.parameters
        )

        # Step 5: Code fetches cached data if needed
        reduce_node = reduce_data_node_cls(
            connector_name=connector_name,
            deps=deps,
            session_id=session_id,
        )
        data = await reduce_node.run(state, emitter, call_result)

        # Step 6: Return markdown data as findings for the orchestrator.
        # ReduceDataNode always returns a markdown string.
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
