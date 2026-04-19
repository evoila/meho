# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Lookup topology tool - Look up entity in topology graph.

Enables cross-connector correlation and relationship traversal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter


class LookupTopologyInput(BaseModel):
    """Input for lookup_topology."""

    query: str = Field(description="Entity name to look up")
    traverse_depth: int = Field(default=10, ge=1, le=20, description="Max traversal depth")
    cross_connectors: bool = Field(default=True, description="Follow SAME_AS links")


class TopologyEntity(BaseModel):
    """A topology entity."""

    name: str
    entity_type: str | None = None
    connector_type: str | None = None
    depth: int = 0
    relationship: str | None = None


class LookupTopologyOutput(BaseModel):
    """Output from lookup_topology."""

    found: bool = Field(default=False)
    entity: dict[str, Any] | None = Field(default=None)
    topology_chain: list[TopologyEntity] = Field(default_factory=list)
    same_as_entities: list[dict[str, Any]] = Field(default_factory=list)
    possibly_related: list[dict[str, Any]] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


@dataclass
class LookupTopologyTool(BaseTool[LookupTopologyInput, LookupTopologyOutput]):
    """Look up an entity in the topology graph.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "lookup_topology"
    TOOL_DESCRIPTION: ClassVar[str] = """Look up entity in learned topology graph.
Returns related entities and cross-connector correlations.
Use to find what systems are connected."""
    InputSchema: ClassVar[type[BaseModel]] = LookupTopologyInput
    OutputSchema: ClassVar[type[BaseModel]] = LookupTopologyOutput

    async def execute(
        self,
        tool_input: LookupTopologyInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> LookupTopologyOutput:
        """Execute lookup_topology tool."""
        await emitter.tool_start(self.TOOL_NAME)

        try:
            # Get session from deps
            meho_deps = getattr(deps, "meho_deps", None)
            if not meho_deps:
                await emitter.tool_complete(self.TOOL_NAME, success=False)
                return LookupTopologyOutput(found=False, suggestions=["No dependencies available"])

            session = getattr(meho_deps, "db_session", None)
            if not session:
                await emitter.tool_complete(self.TOOL_NAME, success=False)
                return LookupTopologyOutput(found=False, suggestions=["No database session"])

            tenant_id = getattr(meho_deps, "tenant_id", "default")

            from meho_app.modules.topology.schemas import LookupTopologyInput as LookupInput
            from meho_app.modules.topology.service import TopologyService

            service = TopologyService(session)
            result = await service.lookup(
                input=LookupInput(
                    query=tool_input.query,
                    traverse_depth=tool_input.traverse_depth,
                    cross_connectors=tool_input.cross_connectors,
                ),
                tenant_id=tenant_id,
            )

            chain = [
                TopologyEntity(
                    name=item.entity,
                    entity_type=item.entity_type,
                    depth=item.depth,
                    relationship=item.relationship,
                )
                for item in result.topology_chain
            ]

            await emitter.tool_complete(self.TOOL_NAME, success=result.found)
            return LookupTopologyOutput(
                found=result.found,
                topology_chain=chain,
                same_as_entities=[
                    {"name": e.entity.name, "connector": e.connector_name}
                    for e in result.same_as_entities
                ],
                possibly_related=[
                    {"entity": r.entity, "similarity": r.similarity}
                    for r in result.possibly_related
                ],
                suggestions=result.suggestions,
            )

        except Exception:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            raise
