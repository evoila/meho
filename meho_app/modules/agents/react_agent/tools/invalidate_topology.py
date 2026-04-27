# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Invalidate topology tool - Mark topology entries as stale.

Called when the agent detects stored topology no longer matches reality.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter


class InvalidateTopologyInput(BaseModel):
    """Input for invalidate_topology."""

    entity_name: str = Field(description="Name of entity to invalidate")
    reason: str = Field(description="Why the entity is being invalidated")


class InvalidateTopologyOutput(BaseModel):
    """Output from invalidate_topology."""

    success: bool = Field(default=True)
    message: str = Field(default="")


@dataclass
class InvalidateTopologyTool(BaseTool[InvalidateTopologyInput, InvalidateTopologyOutput]):
    """Invalidate a stale topology entry.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "invalidate_topology"
    TOOL_DESCRIPTION: ClassVar[str] = """Mark a topology entity as stale/invalid.
Call when API returns 404 or entity no longer exists.
Helps keep topology accurate."""
    InputSchema: ClassVar[type[BaseModel]] = InvalidateTopologyInput
    OutputSchema: ClassVar[type[BaseModel]] = InvalidateTopologyOutput

    async def execute(
        self,
        tool_input: InvalidateTopologyInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> InvalidateTopologyOutput:
        """Execute invalidate_topology tool."""
        await emitter.tool_start(self.TOOL_NAME)

        try:
            # Get session from deps
            meho_deps = getattr(deps, "meho_deps", None)
            if not meho_deps:
                await emitter.tool_complete(self.TOOL_NAME, success=False)
                return InvalidateTopologyOutput(success=False, message="No dependencies available")

            session = getattr(meho_deps, "db_session", None)
            if not session:
                await emitter.tool_complete(self.TOOL_NAME, success=False)
                return InvalidateTopologyOutput(success=False, message="No database session")

            tenant_id = getattr(meho_deps, "tenant_id", "default")

            from meho_app.modules.topology.schemas import InvalidateTopologyInput as InvalidateInput
            from meho_app.modules.topology.service import TopologyService

            service = TopologyService(session)
            result = await service.invalidate(
                input=InvalidateInput(
                    entity_name=tool_input.entity_name,
                    reason=tool_input.reason,
                ),
                tenant_id=tenant_id,
            )

            await emitter.tool_complete(self.TOOL_NAME, success=result.invalidated)
            return InvalidateTopologyOutput(success=result.invalidated, message=result.message)

        except Exception:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            raise
