# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Call Operation Node - Code executes the selected operation.

This is step 4 of the deterministic workflow.
Executes the call_operation tool with the selected operation and parameters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger

if TYPE_CHECKING:
    from meho_app.modules.agents.specialist_agent.state import WorkflowState
    from meho_app.modules.agents.sse.emitter import EventEmitter

logger = get_logger(__name__)


@dataclass
class CallOperationNode:
    """Node that executes call_operation tool.

    Emits:
        action: call_operation with operation_id and parameters
        observation: Result preview (if data available)
    """

    connector_id: str
    connector_name: str
    deps: Any  # MEHODependencies
    session_id: str = ""  # Session ID for cache isolation (security)

    async def run(
        self,
        state: WorkflowState,
        emitter: EventEmitter | None,
        operation_id: str,
        parameters: dict[str, Any],
    ) -> Any:
        """Execute the call operation step.

        Args:
            state: Current workflow state.
            emitter: Event emitter for SSE streaming.
            operation_id: Operation to call.
            parameters: Parameters for the operation.

        Returns:
            Operation result (may include data_available=False for large results).
        """
        import time

        from meho_app.modules.agents.shared.handlers.operation_handlers import (
            call_operation_handler,
        )

        tool_args = {
            "connector_id": self.connector_id,
            "operation_id": operation_id,
            "parameters": parameters,
        }

        # Create deps wrapper
        graph_deps = self._create_graph_deps()

        args = {
            "connector_id": self.connector_id,
            "operation_id": operation_id,
            "parameter_sets": [parameters] if parameters else [{}],
        }

        # Execute with timing
        start_time = time.perf_counter()
        result_json = await call_operation_handler(graph_deps, args)
        duration_ms = (time.perf_counter() - start_time) * 1000

        try:
            result = json.loads(result_json)
        except json.JSONDecodeError:
            result = {"raw": result_json}

        # Emit consolidated tool_call event with both input and output
        if emitter and emitter.has_transcript_collector:
            await emitter.tool_call_detailed(
                tool="call_operation",
                args=tool_args,
                result=result,
                summary=f"call_operation({operation_id}) on {self.connector_name}",
                duration_ms=duration_ms,
            )

        # Update state
        state.steps_executed.append(f"call_operation: {operation_id}")

        # Register cached data in session state for multi-turn awareness (Phase 4)
        if (
            state.session_state
            and isinstance(result, dict)
            and result.get("data_available") is False
        ):
            table_name = result.get("table")
            row_count = result.get("row_count", 0)
            if table_name:
                state.session_state.register_cached_data(
                    table_name=table_name,
                    connector_id=self.connector_id,
                    row_count=row_count,
                )
                logger.info(
                    f"[{self.connector_name}] Registered cached table: "
                    f"{table_name} ({row_count} rows)"
                )

        logger.debug(f"[{self.connector_name}] Called operation: {operation_id}")
        return result

    def _create_graph_deps(self) -> Any:
        """Create MEHOGraphDeps-compatible wrapper."""
        from dataclasses import dataclass as dc

        @dc
        class MinimalGraphDeps:
            meho_deps: Any
            user_id: str = ""
            tenant_id: str = ""
            session_id: str = ""

        user_id = ""
        tenant_id = ""
        if hasattr(self.deps, "user_context"):
            user_id = getattr(self.deps.user_context, "user_id", "")
            tenant_id = getattr(self.deps.user_context, "tenant_id", "")

        return MinimalGraphDeps(
            meho_deps=self.deps,
            user_id=user_id,
            tenant_id=tenant_id,
            session_id=self.session_id,
        )
