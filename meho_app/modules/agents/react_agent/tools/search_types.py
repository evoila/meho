# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Search types tool - Search entity type definitions.

Useful for SOAP and VMware connectors with complex type hierarchies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter


class SearchTypesInput(BaseModel):
    """Input for search_types."""

    connector_id: str = Field(description="Connector UUID")
    query: str = Field(min_length=1, description="Type name to search")
    limit: int = Field(default=10, ge=1, le=50, description="Max results")


class TypeInfo(BaseModel):
    """Information about an entity type."""

    type_name: str = Field(description="Type name")
    description: str | None = Field(default=None)
    category: str | None = Field(default=None)
    properties_count: int = Field(default=0)
    properties_preview: list[str] = Field(default_factory=list)


class SearchTypesOutput(BaseModel):
    """Output from search_types."""

    types: list[TypeInfo] = Field(default_factory=list)
    total_found: int = Field(default=0)


@dataclass
class SearchTypesTool(BaseTool[SearchTypesInput, SearchTypesOutput]):
    """Search for entity type definitions on a connector.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "search_types"
    TOOL_DESCRIPTION: ClassVar[str] = """Search entity type definitions.
Useful for SOAP/VMware connectors with complex types.
Returns type names and their properties."""
    InputSchema: ClassVar[type[BaseModel]] = SearchTypesInput
    OutputSchema: ClassVar[type[BaseModel]] = SearchTypesOutput

    async def execute(
        self,
        tool_input: SearchTypesInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> SearchTypesOutput:
        """Execute search_types tool."""
        import json

        await emitter.tool_start(self.TOOL_NAME)

        try:
            from meho_app.modules.agents.shared.handlers import search_types_handler

            result_str = await search_types_handler(
                deps,
                {
                    "connector_id": tool_input.connector_id,
                    "query": tool_input.query,
                    "limit": tool_input.limit,
                },
            )
            result_data = json.loads(result_str)

            if isinstance(result_data, dict) and (
                "error" in result_data or "message" in result_data
            ):
                await emitter.tool_complete(self.TOOL_NAME, success=False)
                return SearchTypesOutput(types=[], total_found=0)

            types = [
                TypeInfo(
                    type_name=t.get("type_name", ""),
                    description=t.get("description"),
                    category=t.get("category"),
                    properties_count=t.get("properties_count", 0),
                    properties_preview=t.get("properties_preview", []),
                )
                for t in result_data
            ]

            await emitter.tool_complete(self.TOOL_NAME, success=True)
            return SearchTypesOutput(types=types, total_found=len(types))

        except Exception:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            raise
