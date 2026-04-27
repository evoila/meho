# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Base Reduce Data Node - Shared logic for all agents.

TASK-195 Phase 2: Replaced the hand-rolled LLM SQL generation (mini-ReAct
loop with QueryDecision) with JSONFlux-powered NLQ-to-SQL aggregation.

TASK-195 Phase 3: Multi-table support.  When aggregating, ALL session tables
are loaded from Redis and registered with QueryEngine, enabling cross-table
JOINs (e.g. "which pods run on nodes with high CPU?").

TASK-195 Phase 6: Removed raw-data SSE streaming to frontend.  Data is now
delivered exclusively through the bot as markdown (JSONFlux) or as row dicts
for small datasets.

The node's job is now:
1. Load cached data from DuckDB
2. For small datasets: return rows directly
3. For large datasets: load ALL session tables from Redis, register in
   QueryEngine, call jsonflux_aggregate() with the user's goal as a
   natural language query, return markdown

The agent never touches raw data for large datasets -- it sees schema
previews and receives markdown tables from aggregation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter

logger = get_logger(__name__)


@dataclass
class BaseReduceDataNode:
    """Base node that fetches cached data using reduce_data tool.

    This is step 5 of the deterministic workflow.
    If call_operation returned data_available=False, this node fetches the
    cached data, optionally aggregates it via JSONFlux, and returns either
    raw rows (small data) or a markdown table (large data).

    Emits:
        action: reduce_data with table and row_count
        observation: result_type "aggregated" (markdown from JSONFlux)
    """

    connector_name: str
    deps: Any  # MEHODependencies
    session_id: str = ""  # Session ID for cache isolation (security)
    max_rows: int = 100
    token_threshold: int = 4000  # Skip aggregation if data fits in this many tokens

    async def run(
        self,
        state: Any,
        emitter: EventEmitter | None,
        call_result: Any,
    ) -> str:
        """Execute the reduce data step if needed.

        Always returns a markdown string so that downstream consumers
        (flow.py, orchestrator) never need to branch on the return type.

        Args:
            state: Current workflow state.
            emitter: Event emitter for SSE streaming.
            call_result: Result from CallOperationNode.

        Returns:
            A markdown string: either a formatted table from JSONFlux
            aggregation, a markdown table from small data, or a JSON
            code block for direct API responses.
        """
        # Check if we need to fetch cached data
        if not (isinstance(call_result, dict) and call_result.get("data_available") is False):
            # Data is available directly - no caching needed
            return self._to_markdown(call_result)

        table_name = call_result.get("table") or "data"
        row_count = call_result.get("row_count") or 0

        # Early exit for empty tables
        if row_count == 0:
            if emitter:
                await emitter.thought(f"Table {table_name} is empty - no data to process")
            state.steps_executed.append(f"auto_reduce_data: {table_name} (0 rows - empty)")
            return "No data returned."

        # Emit action event
        if emitter:
            await emitter.action_detailed(
                tool="reduce_data",
                args={"table": table_name, "row_count": row_count},
                summary=f"Fetching cached {table_name} ({row_count} rows)",
            )

        # Create deps wrapper
        graph_deps = self._create_graph_deps()

        # Fetch full data from DuckDB
        sql_full = f"SELECT * FROM {table_name}"  # noqa: S608 -- static SQL query, no user input
        full_result = await self._execute_sql(graph_deps, sql_full)
        full_rows = self._extract_rows(full_result)

        # Update state
        state.steps_executed.append(f"auto_reduce_data: {table_name} ({row_count} rows)")

        logger.debug(f"[{self.connector_name}] Fetched {row_count} rows from {table_name}")

        # Check if data fits in context - skip aggregation for small datasets
        estimated_tokens = self._estimate_tokens(full_rows)
        if estimated_tokens <= self.token_threshold:
            logger.debug(
                f"[{self.connector_name}] Data fits in context "
                f"({estimated_tokens} tokens <= {self.token_threshold}), "
                f"skipping aggregation"
            )
            # Return full data (capped at max_rows) as markdown table
            return self._rows_to_markdown(full_rows[: self.max_rows])

        # --- JSONFlux aggregation for large datasets ---
        return await self._aggregate_with_jsonflux(
            table_name=table_name,
            full_rows=full_rows,
            user_goal=state.user_goal,
            emitter=emitter,
            state=state,
        )

    async def _aggregate_with_jsonflux(
        self,
        *,
        table_name: str,
        full_rows: list[dict[str, Any]],
        user_goal: str,
        emitter: EventEmitter | None,
        state: Any,
    ) -> str:
        """Register data in QueryEngine and run JSONFlux NLQ aggregation.

        TASK-195 Phase 3: Loads ALL session tables from Redis so the
        QueryEngine can support cross-table JOINs.  The current table's
        ``full_rows`` are used directly (already in memory); other session
        tables are loaded from Redis via ``get_session_tables_async()``.

        Falls back to single-table registration if Redis is unavailable
        or the session has no other tables.

        Args:
            table_name: Name of the cached table.
            full_rows: All rows fetched from DuckDB.
            user_goal: The user's natural language question.
            emitter: SSE emitter for frontend events.
            state: Workflow state for step tracking.

        Returns:
            Markdown string (aggregated table or fallback markdown table).
        """
        from meho_app.jsonflux import QueryEngine
        from meho_app.modules.agents.base.jsonflux_aggregate import (
            GENERIC_NAMES,
            _infer_table_name,
            generate_data_preview,
            jsonflux_aggregate,
        )

        engine = QueryEngine()
        try:
            # Register the current table (always available in memory)
            engine.register(table_name, full_rows)

            # --- Phase 3: Load additional session tables for JOINs ---
            extra_tables = await self._load_session_tables(table_name)
            for name, rows in extra_tables.items():
                # Improve generic names using JSON structure heuristics
                final_name = (
                    _infer_table_name(rows, fallback=name) if name in GENERIC_NAMES else name
                )
                # Avoid collisions with current table or already-registered
                if final_name == table_name:
                    final_name = name  # Keep original Redis-derived name
                engine.register(final_name, rows)

            registered_count = 1 + len(extra_tables)
            logger.debug(
                f"[{self.connector_name}] Registered {registered_count} table(s) in QueryEngine"
            )

            preview = generate_data_preview(engine)
            logger.debug(f"[{self.connector_name}] Schema preview:\n{preview}")

            result = await jsonflux_aggregate(
                engine=engine,
                natural_language_query=user_goal,
                max_rows=self.max_rows,
            )

            if result.success:
                # Emit "aggregated" observation for frontend with SQL details
                if emitter:
                    await emitter.observation_detailed(
                        tool="reduce_data",
                        result={
                            "table": table_name,
                            "row_count": result.row_count,
                            "sql": result.sql,
                            "result_type": "aggregated",
                            "markdown": result.markdown,
                        },
                        summary=f"Aggregated {table_name}: {result.row_count} rows via SQL",
                    )
                state.steps_executed.append(
                    f"reduce_data_aggregate: {result.sql} ({result.row_count} rows)"
                )
                return result.markdown  # Markdown string, not rows
            else:
                logger.warning(f"[{self.connector_name}] Aggregation failed: {result.error}")
                if emitter:
                    await emitter.thought(f"Aggregation failed: {result.error}")
                return self._rows_to_markdown(full_rows[: self.max_rows])
        finally:
            engine.close()

    async def _load_session_tables(
        self,
        current_table_name: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """Load all *other* session tables from Redis for cross-table JOINs.

        Skips the ``current_table_name`` (already in memory).  Returns an
        empty dict gracefully when Redis is unavailable, session_id is
        empty, or the load fails for any reason.

        Args:
            current_table_name: Table name to skip (already registered).

        Returns:
            Dict mapping table_name -> list of row dicts.
        """
        redis_client = getattr(self.deps, "redis", None)
        if not redis_client or not self.session_id:
            logger.debug(
                f"[{self.connector_name}] Skipping multi-table load: "
                f"{'no Redis' if not redis_client else 'no session_id'}"
            )
            return {}

        try:
            from meho_app.modules.agents.unified_executor import (
                get_unified_executor,
            )

            executor = get_unified_executor(redis_client)
            cached_tables = await executor.get_session_tables_async(self.session_id)

            extra: dict[str, list[dict[str, Any]]] = {}
            for name, cached_table in cached_tables.items():
                if name == current_table_name:
                    continue  # Already registered from full_rows
                rows = (
                    cached_table.arrow_table.to_pylist()
                    if cached_table.arrow_table is not None
                    else []
                )
                if rows:  # Skip empty tables
                    extra[name] = rows

            if extra:
                logger.info(
                    f"[{self.connector_name}] Loaded {len(extra)} additional "
                    f"session table(s) for JOINs: {list(extra.keys())}"
                )
            return extra

        except Exception:
            logger.warning(
                f"[{self.connector_name}] Failed to load session tables "
                f"from Redis, continuing with single table",
                exc_info=True,
            )
            return {}

    # ------------------------------------------------------------------
    # Markdown formatting
    # ------------------------------------------------------------------

    def _rows_to_markdown(self, rows: list[dict[str, Any]]) -> str:
        """Convert a list of row dicts to a markdown table.

        Args:
            rows: List of dicts with uniform keys.

        Returns:
            Markdown table string, or "No data returned." if empty.
        """
        if not rows:
            return "No data returned."
        headers = list(rows[0].keys())
        lines = [
            "| " + " | ".join(str(h) for h in headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
        ]
        for row in rows:
            values = [str(row.get(h, "")) for h in headers]
            lines.append("| " + " | ".join(values) + " |")
        return "\n".join(lines)

    def _to_markdown(self, data: Any) -> str:
        """Convert arbitrary data to a markdown string.

        Used for the passthrough case where data was available directly
        from the API response (no caching).

        Args:
            data: Raw API response (dict, list, or other).

        Returns:
            Markdown string -- a table for list-of-dicts, or a JSON
            code block for anything else.
        """
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return self._rows_to_markdown(data)
        return f"```json\n{json.dumps(data, indent=2, default=str)}\n```"

    # ------------------------------------------------------------------
    # Helpers (kept from original)
    # ------------------------------------------------------------------

    async def _execute_sql(self, graph_deps: Any, sql: str) -> dict[str, Any]:
        """Execute SQL via the reduce_data_handler."""
        from meho_app.modules.agents.shared.handlers.knowledge_handlers import (
            reduce_data_handler,
        )

        result_json = await reduce_data_handler(graph_deps, {"sql": sql})
        try:
            parsed: dict[str, Any] = json.loads(result_json)
            return parsed
        except json.JSONDecodeError:
            return {"raw": result_json}

    def _extract_rows(self, result: Any) -> list[dict[str, Any]]:
        if isinstance(result, dict):
            rows = result.get("rows")
            if isinstance(rows, list):
                return rows
            data = result.get("data")
            if isinstance(data, list):
                return data
        if isinstance(result, list):
            return result
        return []

    def _estimate_tokens(self, rows: list[dict[str, Any]]) -> int:
        """Estimate tokens by measuring JSON size.

        Uses ~4 chars per token heuristic (conservative for English/JSON).
        Samples first 10 rows to estimate average row size.
        """
        if not rows:
            return 0
        sample_size = min(len(rows), 10)
        sample_json = json.dumps(rows[:sample_size], default=str)
        chars_per_row = len(sample_json) / sample_size
        total_chars = len(rows) * chars_per_row
        return int(total_chars / 4)

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
