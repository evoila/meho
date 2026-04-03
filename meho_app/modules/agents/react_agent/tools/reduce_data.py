# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Reduce data tool - Query cached data using SQL.

Part of the Brain-Muscle architecture for data reduction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter


class ReduceDataInput(BaseModel):
    """Input for reduce_data."""

    sql: str = Field(min_length=1, description="SQL query to execute on cached data")


class ReduceDataOutput(BaseModel):
    """Output from reduce_data."""

    rows: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    row_count: int = Field(default=0)
    success: bool = Field(default=True)
    error: str | None = Field(default=None)
    available_tables: list[str] | None = Field(default=None)


@dataclass
class ReduceDataTool(BaseTool[ReduceDataInput, ReduceDataOutput]):
    """Query cached data using SQL.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "reduce_data"
    TOOL_DESCRIPTION: ClassVar[str] = """Query cached API data using SQL.
Call operations first to cache data, then query with SQL.
Example: SELECT * FROM virtual_machines WHERE num_cpu > 8"""
    InputSchema: ClassVar[type[BaseModel]] = ReduceDataInput
    OutputSchema: ClassVar[type[BaseModel]] = ReduceDataOutput

    async def execute(  # NOSONAR (cognitive complexity)
        self,
        tool_input: ReduceDataInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> ReduceDataOutput:
        """Execute reduce_data tool."""
        import json

        await emitter.tool_start(self.TOOL_NAME)

        try:
            from meho_app.modules.agents.shared.handlers import reduce_data_handler

            result_str = await reduce_data_handler(deps, {"sql": tool_input.sql})
            result_data = json.loads(result_str)

            if "error" in result_data:
                await emitter.tool_complete(self.TOOL_NAME, success=False)

                # Build rich error with column info and types when available
                error_parts = [result_data["error"]]
                table_columns = result_data.get("table_columns")
                table_column_types = result_data.get("table_column_types", {})
                if table_columns:
                    for tbl, cols in table_columns.items():
                        types = table_column_types.get(tbl, {})
                        if types:
                            typed_cols = [f"{c}:{types.get(c, '?')}" for c in cols]
                            error_parts.append(f"Table '{tbl}' columns: {', '.join(typed_cols)}")
                        else:
                            error_parts.append(f"Table '{tbl}' columns: {cols}")
                sample_rows = result_data.get("sample_rows", {})
                if sample_rows:
                    for tbl, row in sample_rows.items():
                        error_parts.append(f"Sample row from '{tbl}': {row}")

                # Normalize available_tables to list (guard against string values)
                raw_tables = result_data.get("available_tables")
                if isinstance(raw_tables, str):
                    raw_tables = [t.strip() for t in raw_tables.split(",") if t.strip()]

                return ReduceDataOutput(
                    rows=[],
                    columns=[],
                    row_count=0,
                    success=False,
                    error="\n".join(error_parts),
                    available_tables=raw_tables,
                )

            rows = result_data.get("rows", result_data.get("data", []))
            columns = result_data.get("columns", [])
            if not columns and rows:
                columns = list(rows[0].keys()) if rows else []

            await emitter.tool_complete(self.TOOL_NAME, success=True)
            return ReduceDataOutput(
                rows=rows,
                columns=columns,
                row_count=result_data.get("count", len(rows)),
                success=True,
            )

        except Exception:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            raise
