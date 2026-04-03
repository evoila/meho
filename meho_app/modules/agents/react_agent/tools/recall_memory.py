# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Recall memory tool - Search connector memories for historical context.

Phase 99 (INV-05): Allows specialists to query memories on-demand instead
of receiving them in the system prompt. Auto-extracted memories are no longer
injected; this tool makes them opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter


class RecallMemoryInput(BaseModel):
    """Input for recall_memory."""

    query: str = Field(
        min_length=1,
        description="Natural language query describing what historical context you need.",
    )
    connector_id: str | None = Field(default=None, description="Injected server-side -- do not set")


class RecallMemoryOutput(BaseModel):
    """Output from recall_memory."""

    found: bool
    count: int = 0
    result: str = ""


@dataclass
class RecallMemoryTool(BaseTool[RecallMemoryInput, RecallMemoryOutput]):
    """Search connector memories for historical context.

    Use ONLY when you need historical context -- after checking current data first.
    Memories are hints, not facts. Always verify with fresh tool calls.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "recall_memory"
    TOOL_DESCRIPTION: ClassVar[str] = (
        "Search connector memories for historical context from past investigations. "
        "Use ONLY when you need historical context -- after checking current data first. "
        "Memories are hints, not facts. Always verify with fresh tool calls."
    )
    InputSchema: ClassVar[type[BaseModel]] = RecallMemoryInput
    OutputSchema: ClassVar[type[BaseModel]] = RecallMemoryOutput

    async def execute(
        self,
        tool_input: RecallMemoryInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> RecallMemoryOutput:
        """Execute recall_memory tool."""
        await emitter.tool_start(self.TOOL_NAME)

        try:
            from meho_app.api.database import create_openapi_session_maker
            from meho_app.modules.memory.service import get_memory_service

            tenant_id = deps.external_deps.user_context.tenant_id
            session_maker = create_openapi_session_maker()

            async with session_maker() as db:
                svc = get_memory_service(db)
                results = await svc.search(
                    query=tool_input.query,
                    connector_id=tool_input.connector_id or "",
                    tenant_id=tenant_id,
                    top_k=5,
                    score_threshold=0.5,
                )

            if not results:
                output = RecallMemoryOutput(
                    found=False,
                    count=0,
                    result="No relevant memories found for this query.",
                )
                await emitter.tool_complete(self.TOOL_NAME, success=True)
                return output

            lines = ["## Recalled Memories", ""]
            for r in results:
                mem = r.memory
                badge = {
                    "operator": "[operator]",
                    "confirmed_outcome": "[confirmed]",
                    "auto_extracted": "[auto]",
                }.get(mem.confidence_level, "[unknown]")
                lines.append(f"**{mem.title}** {badge}")
                lines.append(mem.body)
                lines.append("")

            lines.append("---")
            lines.append(
                "*Memories reflect past state. Verify with fresh data before relying on them.*"
            )

            output = RecallMemoryOutput(
                found=True,
                count=len(results),
                result="\n".join(lines),
            )

            await emitter.tool_complete(self.TOOL_NAME, success=True)
            return output

        except Exception:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            raise
