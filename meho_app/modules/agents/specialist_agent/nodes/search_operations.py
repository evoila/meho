# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Search Operations Node - Code executes operation search.

This is step 2 of the deterministic workflow.
Executes the search_operations tool to find matching API operations.
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
class SearchOperationsNode:
    """Node that executes search_operations tool.

    Emits:
        action: search_operations with connector_id and query
        observation: "Found {n} operations"
    """

    connector_id: str
    connector_name: str
    deps: Any  # MEHODependencies

    async def run(
        self,
        state: WorkflowState,
        emitter: EventEmitter | None,
        query: str,
    ) -> list[dict[str, Any]]:
        """Execute the search operations step.

        Args:
            state: Current workflow state.
            emitter: Event emitter for SSE streaming.
            query: Search query from SearchIntentNode.

        Returns:
            List of matching operations.
        """
        from meho_app.modules.agents.shared.handlers.operation_handlers import (
            search_operations_handler,
        )

        # Emit action event
        if emitter:
            await emitter.action(
                "search_operations",
                {"connector_id": self.connector_id, "query": query},
            )

        # Create deps wrapper for handler compatibility
        graph_deps = self._create_graph_deps()

        args = {
            "connector_id": self.connector_id,
            "query": query,
            "limit": 10,
        }

        result_json = await search_operations_handler(graph_deps, args)

        try:
            operations: list[dict[str, Any]] = json.loads(result_json)
        except json.JSONDecodeError:
            operations = []

        # Emit observation event with full data
        if emitter:
            await emitter.observation(
                "search_operations",
                {
                    "count": len(operations),
                    "operations": operations,  # Full raw output
                },
            )

        # Update state
        state.steps_executed.append(f"search_operations: found {len(operations)} operations")

        logger.debug(f"[{self.connector_name}] Found {len(operations)} operations")
        return operations

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
        )
