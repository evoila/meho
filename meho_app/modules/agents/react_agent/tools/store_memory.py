# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Store memory tool - Create operator-level memories for a connector.

Allows operators to teach the agent domain-specific knowledge that persists
across conversations, making each connector progressively smarter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter


class StoreMemoryInput(BaseModel):
    """Input for store_memory."""

    title: str = Field(
        min_length=1, max_length=500, description="Short descriptive title for the memory"
    )
    body: str = Field(
        min_length=1, description="Full context to remember -- include all relevant details"
    )
    memory_type: str = Field(
        default="config",
        description=(
            "Classification: entity, pattern, outcome, or config. "
            "Use 'entity' for specific resources/names, "
            "'pattern' for recurring behaviors, "
            "'outcome' for diagnostic results, "
            "'config' for settings and constraints."
        ),
    )
    tags: list[str] = Field(default_factory=list, description="Optional tags for categorization")
    connector_id: str | None = Field(default=None, description="Injected server-side -- do not set")


class StoreMemoryOutput(BaseModel):
    """Output from store_memory."""

    success: bool
    memory_id: str | None = None
    title: str = ""
    merged: bool = False
    message: str = ""


@dataclass
class StoreMemoryTool(BaseTool[StoreMemoryInput, StoreMemoryOutput]):
    """Store a memory for this connector.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "store_memory"
    TOOL_DESCRIPTION: ClassVar[str] = (
        "Store a memory for this connector. Use when the operator asks to remember, "
        "note, or keep track of something. Creates an operator-level memory that "
        "persists across conversations. The memory will be available in all future "
        "interactions with this connector."
    )
    InputSchema: ClassVar[type[BaseModel]] = StoreMemoryInput
    OutputSchema: ClassVar[type[BaseModel]] = StoreMemoryOutput

    async def execute(
        self,
        tool_input: StoreMemoryInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> StoreMemoryOutput:
        """Execute store_memory tool."""
        await emitter.tool_start(self.TOOL_NAME)

        try:
            from meho_app.api.database import create_openapi_session_maker
            from meho_app.modules.memory.models import ConfidenceLevel
            from meho_app.modules.memory.schemas import MemoryCreate
            from meho_app.modules.memory.service import get_memory_service

            session_maker = create_openapi_session_maker()
            async with session_maker() as db:
                svc = get_memory_service(db)

                memory_create = MemoryCreate(
                    title=tool_input.title,
                    body=tool_input.body,
                    memory_type=tool_input.memory_type,
                    tags=tool_input.tags,
                    confidence_level=ConfidenceLevel.OPERATOR,
                    source_type="operator",
                    created_by=deps.external_deps.user_context.user_id or "operator",
                    connector_id=tool_input.connector_id,
                    tenant_id=deps.external_deps.user_context.tenant_id,
                    provenance_trail=[],
                )

                response = await svc.create_with_dedup(memory_create)
                await db.commit()

            # Build confirmation message
            if response.merged:
                confirmation = f"Updated existing memory: {response.title}"
            else:
                confirmation = f"Stored new memory: {response.title}"

            output = StoreMemoryOutput(
                success=True,
                memory_id=response.id,
                title=response.title,
                merged=response.merged,
                message=confirmation,
            )

            await emitter.tool_complete(self.TOOL_NAME, success=True)
            return output

        except Exception:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            raise
