# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Call operation tool - Execute operations on connectors.

The most complex tool - handles REST, SOAP, VMware, Kubernetes, etc.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter


class CallOperationInput(BaseModel):
    """Input for call_operation."""

    connector_id: str = Field(description="Connector UUID")
    operation_id: str = Field(description="Operation ID from search_operations")
    parameter_sets: list[dict[str, Any]] = Field(
        default_factory=lambda: [dict[str, Any]()],
        description="List of parameter sets for batch execution",
    )


class CallOperationOutput(BaseModel):
    """Output from call_operation."""

    results: list[dict[str, Any]] = Field(default_factory=list)
    data_available: bool = Field(default=False, description="Whether data is cached")
    table: str | None = Field(default=None, description="Table name for reduce_data")
    row_count: int | None = Field(default=None, description="Number of rows cached")
    columns: list[str] | None = Field(default=None, description="Column names")
    column_types: dict[str, str] | None = Field(
        default=None, description="Column name -> Arrow type (e.g. VARCHAR, BIGINT)"
    )
    success: bool = Field(default=True)
    error: str | None = Field(default=None)


@dataclass
class CallOperationTool(BaseTool[CallOperationInput, CallOperationOutput]):
    """Execute an operation on a connector.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "call_operation"
    TOOL_DESCRIPTION: ClassVar[str] = """Execute an operation on any connector.
Works for REST, SOAP, VMware, Kubernetes.
Results are cached for SQL queries via reduce_data."""
    InputSchema: ClassVar[type[BaseModel]] = CallOperationInput
    OutputSchema: ClassVar[type[BaseModel]] = CallOperationOutput

    async def execute(
        self,
        tool_input: CallOperationInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> CallOperationOutput:
        """Execute call_operation tool."""
        import json

        await emitter.tool_start(self.TOOL_NAME)

        try:
            from meho_app.modules.agents.shared.handlers import call_operation_handler

            result_str = await call_operation_handler(
                deps,
                {
                    "connector_id": tool_input.connector_id,
                    "operation_id": tool_input.operation_id,
                    "parameter_sets": tool_input.parameter_sets,
                },
                state=None,
            )
            result_data = json.loads(result_str)

            # Handle error case
            if isinstance(result_data, dict) and "error" in result_data:
                await emitter.tool_complete(self.TOOL_NAME, success=False)
                return CallOperationOutput(
                    results=[],
                    data_available=False,
                    success=False,
                    error=result_data.get("error"),
                )

            # Handle batch results
            if "batch_results" in result_data:
                await emitter.tool_complete(self.TOOL_NAME, success=True)
                return CallOperationOutput(
                    results=result_data.get("batch_results", []),
                    data_available=True,
                    success=True,
                )

            # Single result - extract cache info
            await emitter.tool_complete(self.TOOL_NAME, success=True)
            return CallOperationOutput(
                results=[result_data] if result_data.get("data") else [],
                data_available=result_data.get("data_available", bool(result_data.get("data"))),
                table=result_data.get("table"),
                row_count=result_data.get("row_count") or result_data.get("count"),
                columns=result_data.get("columns"),
                column_types=result_data.get("column_types"),
                success=result_data.get("success", True),
            )

        except Exception:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            raise
