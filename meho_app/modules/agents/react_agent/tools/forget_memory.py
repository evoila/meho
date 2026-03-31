# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Forget memory tool - Find and remove memories for a connector.

Two-step flow: search to find matching memories, then delete by ID
after operator confirmation. Enables operators to correct or remove
wrong/outdated memories.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter


class ForgetMemoryInput(BaseModel):
    """Input for forget_memory."""

    action: str = Field(
        description="'search' to find matching memories, 'delete' to remove a specific memory by ID"
    )
    query: str | None = Field(
        default=None,
        description="What to search for when action='search'. Describe the memory to find.",
    )
    memory_id: str | None = Field(
        default=None,
        description="Memory ID to delete when action='delete'. Get this from a previous search result.",
    )
    connector_id: str | None = Field(default=None, description="Injected server-side -- do not set")


class ForgetMemoryOutput(BaseModel):
    """Output from forget_memory."""

    action: str  # "search_results", "deleted", "not_found", or "error"
    matches: list[dict] | None = None
    deleted_title: str | None = None
    message: str = ""


@dataclass
class ForgetMemoryTool(BaseTool[ForgetMemoryInput, ForgetMemoryOutput]):
    """Find and remove memories for this connector.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "forget_memory"
    TOOL_DESCRIPTION: ClassVar[str] = (
        "Find and remove memories for this connector. Two-step process: first call "
        "with action='search' and a query to find matching memories, then call with "
        "action='delete' and the memory_id to remove it. Use when the operator says "
        "to forget, remove, or delete a memory."
    )
    InputSchema: ClassVar[type[BaseModel]] = ForgetMemoryInput
    OutputSchema: ClassVar[type[BaseModel]] = ForgetMemoryOutput

    async def execute(
        self,
        tool_input: ForgetMemoryInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> ForgetMemoryOutput:
        """Execute forget_memory tool."""
        await emitter.tool_start(self.TOOL_NAME)

        try:
            from meho_app.api.database import create_openapi_session_maker

            tenant_id = deps.external_deps.user_context.tenant_id
            session_maker = create_openapi_session_maker()

            if tool_input.action == "search":
                output = await self._handle_search(session_maker, tool_input, tenant_id)
            elif tool_input.action == "delete":
                output = await self._handle_delete(session_maker, tool_input, tenant_id)
            else:
                output = ForgetMemoryOutput(
                    action="error",
                    message=f"Invalid action '{tool_input.action}'. Use 'search' or 'delete'.",
                )

            await emitter.tool_complete(self.TOOL_NAME, success=True)
            return output

        except Exception:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            raise

    async def _handle_search(
        self, session_maker: Any, tool_input: ForgetMemoryInput, tenant_id: str
    ) -> ForgetMemoryOutput:
        """Search for memories matching the query."""
        if not tool_input.query:
            return ForgetMemoryOutput(
                action="error",
                message="A 'query' is required when action='search'.",
            )

        from meho_app.modules.memory.service import get_memory_service

        async with session_maker() as db:
            svc = get_memory_service(db)
            results = await svc.search(
                query=tool_input.query,
                connector_id=tool_input.connector_id,
                tenant_id=tenant_id,
                top_k=5,
                score_threshold=0.6,
            )

        if not results:
            return ForgetMemoryOutput(
                action="not_found",
                message=f"No memories found matching '{tool_input.query}'.",
            )

        # Take top 3 matches (or fewer if less found)
        top_results = results[:3]
        matches = [
            {
                "id": r.memory.id,
                "title": r.memory.title,
                "body_preview": r.memory.body[:200],
                "memory_type": r.memory.memory_type,
                "confidence_level": r.memory.confidence_level,
            }
            for r in top_results
        ]

        return ForgetMemoryOutput(
            action="search_results",
            matches=matches,
            message=f"Found {len(matches)} matching memories. Ask the operator which one to delete.",
        )

    async def _handle_delete(
        self, session_maker: Any, tool_input: ForgetMemoryInput, tenant_id: str
    ) -> ForgetMemoryOutput:
        """Delete a specific memory by ID."""
        if not tool_input.memory_id:
            return ForgetMemoryOutput(
                action="error",
                message="A 'memory_id' is required when action='delete'.",
            )

        from meho_app.modules.memory.service import get_memory_service

        async with session_maker() as db:
            svc = get_memory_service(db)

            # Check if memory still exists
            existing = await svc.get_memory(memory_id=tool_input.memory_id, tenant_id=tenant_id)
            if not existing:
                return ForgetMemoryOutput(
                    action="not_found",
                    message="Memory was already removed or does not exist.",
                )

            # Hard delete
            await svc.delete_memory(memory_id=tool_input.memory_id, tenant_id=tenant_id)
            await db.commit()

        return ForgetMemoryOutput(
            action="deleted",
            deleted_title=existing.title,
            message=f"Memory '{existing.title}' has been permanently deleted.",
        )
