# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Search operations tool - Search API operations for a connector.

Works for all connector types: REST, SOAP, VMware, Kubernetes, etc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter

# Regex to extract default values embedded in descriptions like "Default: 100" or "(default: 1h)"
_DEFAULT_PATTERN = re.compile(r"[Dd]efault[:\s]+(\S+)", re.IGNORECASE)


class SearchOperationsInput(BaseModel):
    """Input for search_operations."""

    connector_id: str = Field(description="Connector UUID from list_connectors")
    query: str = Field(min_length=1, description="Search terms for operation names")
    limit: int = Field(default=10, ge=1, le=50, description="Max results")


class OperationParam(BaseModel):
    """Parameter definition for an API operation."""

    name: str
    type: str = "string"
    required: bool = False
    description: str | None = None
    enum: list[str] | None = None
    default: str | None = None


class OperationInfo(BaseModel):
    """Information about an API operation."""

    operation_id: str = Field(description="Operation identifier for call_operation")
    name: str = Field(description="Operation name")
    description: str | None = Field(default=None, description="Operation description")
    category: str | None = Field(default=None, description="Operation category")
    parameters: list[OperationParam] = Field(default_factory=list, description="Operation parameters")
    example: str | None = Field(default=None, description="Example usage")


class SearchOperationsOutput(BaseModel):
    """Output from search_operations."""

    operations: list[OperationInfo] = Field(default_factory=list)
    total_found: int = Field(default=0)
    connector_type: str | None = Field(default=None, description="Connector type")


def _parse_param(p: dict[str, Any]) -> OperationParam:
    """Parse a raw parameter dict into an OperationParam with enum/default extraction.

    Handles three sources of enum/default information:
    1. Explicit ``enum`` or ``allowed_values`` keys in the dict (REST spec parsing)
    2. Explicit ``default`` key in the dict
    3. Default values embedded in the description text (e.g., "Default: 100")
    """
    # Normalise enum: accept both "enum" and "allowed_values" keys
    enum_values = p.get("enum") or p.get("allowed_values")
    if enum_values and isinstance(enum_values, list):
        enum_values = [str(v) for v in enum_values]
    else:
        enum_values = None

    # Normalise default: explicit key first, then mine from description
    default_value = p.get("default")
    if default_value is not None:
        default_value = str(default_value)
    elif p.get("description"):
        m = _DEFAULT_PATTERN.search(p["description"])
        if m:
            default_value = m.group(1).rstrip(".),")

    return OperationParam(
        name=p.get("name", ""),
        type=p.get("type", "string"),
        required=p.get("required", False),
        description=p.get("description"),
        enum=enum_values,
        default=default_value,
    )


@dataclass
class SearchOperationsTool(BaseTool[SearchOperationsInput, SearchOperationsOutput]):
    """Search for API operations on a connector.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "search_operations"
    TOOL_DESCRIPTION: ClassVar[str] = """Search for API operations on a connector.
Works for REST, SOAP, VMware, Kubernetes connectors.
Returns operation_id to use with call_operation."""
    InputSchema: ClassVar[type[BaseModel]] = SearchOperationsInput
    OutputSchema: ClassVar[type[BaseModel]] = SearchOperationsOutput

    async def execute(
        self,
        tool_input: SearchOperationsInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> SearchOperationsOutput:
        """Execute search_operations tool."""
        import json

        await emitter.tool_start(self.TOOL_NAME)

        try:
            from meho_app.modules.agents.shared.handlers import search_operations_handler

            result_str = await search_operations_handler(
                deps,
                {
                    "connector_id": tool_input.connector_id,
                    "query": tool_input.query,
                    "limit": tool_input.limit,
                },
            )
            result_data = json.loads(result_str)

            if isinstance(result_data, dict) and "error" in result_data:
                await emitter.tool_complete(self.TOOL_NAME, success=False)
                return SearchOperationsOutput(operations=[], total_found=0)

            operations = [
                OperationInfo(
                    operation_id=op.get("operation_id", op.get("endpoint_id", "")),
                    name=op.get("name", ""),
                    description=op.get("description"),
                    category=op.get("category"),
                    parameters=[
                        _parse_param(p) if isinstance(p, dict) else OperationParam(name=str(p))
                        for p in (op.get("parameters") or [])
                    ],
                    example=op.get("example"),
                )
                for op in result_data
            ]

            await emitter.tool_complete(self.TOOL_NAME, success=True)
            return SearchOperationsOutput(
                operations=operations,
                total_found=len(operations),
            )

        except Exception:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            raise
