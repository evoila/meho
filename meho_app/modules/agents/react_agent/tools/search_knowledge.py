# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Search knowledge tool - Search the knowledge base for documentation.

Searches documentation and optionally API specs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter


class SearchKnowledgeInput(BaseModel):
    """Input for search_knowledge."""

    query: str = Field(min_length=1, description="Search query")
    limit: int = Field(default=5, ge=1, le=20, description="Max results")
    include_apis: bool = Field(default=False, description="Include API specs in search")
    connector_id: str | None = Field(
        default=None, description="Connector UUID for scoped search (injected server-side)"
    )


class KnowledgeResult(BaseModel):
    """A single knowledge search result."""

    content: str = Field(description="Text content")
    source: str | None = Field(default=None, description="Source URI")
    tags: list[str] | None = Field(default=None, description="Content tags")


class SearchKnowledgeOutput(BaseModel):
    """Output from search_knowledge."""

    results: list[KnowledgeResult] = Field(default_factory=list)
    total_found: int = Field(default=0)


@dataclass
class SearchKnowledgeTool(BaseTool[SearchKnowledgeInput, SearchKnowledgeOutput]):
    """Search the knowledge base for documentation.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "search_knowledge"
    TOOL_DESCRIPTION: ClassVar[str] = """Search documentation and knowledge base.
Use for general questions about systems, architecture, or procedures.
Set include_apis=true to also search OpenAPI specs."""
    InputSchema: ClassVar[type[BaseModel]] = SearchKnowledgeInput
    OutputSchema: ClassVar[type[BaseModel]] = SearchKnowledgeOutput

    async def execute(
        self,
        tool_input: SearchKnowledgeInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> SearchKnowledgeOutput:
        """Execute search_knowledge tool."""
        import json

        await emitter.tool_start(self.TOOL_NAME)

        try:
            from meho_app.modules.agents.shared.handlers import search_knowledge_handler

            result_str = await search_knowledge_handler(
                deps,
                {
                    "query": tool_input.query,
                    "limit": tool_input.limit,
                    "include_apis": tool_input.include_apis,
                    "connector_id": tool_input.connector_id,
                },
            )
            result_data = json.loads(result_str)

            if isinstance(result_data, dict) and "error" in result_data:
                await emitter.tool_complete(self.TOOL_NAME, success=False)
                return SearchKnowledgeOutput(results=[], total_found=0)

            results = [
                KnowledgeResult(
                    content=r.get("content", ""),
                    source=r.get("source"),
                    tags=r.get("tags"),
                )
                for r in result_data
            ]

            await emitter.tool_complete(self.TOOL_NAME, success=True)
            return SearchKnowledgeOutput(results=results, total_found=len(results))

        except Exception:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            raise
