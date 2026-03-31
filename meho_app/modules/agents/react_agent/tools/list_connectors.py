# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""List connectors tool - List all available system connectors.

This is typically the first tool called to discover available systems.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter


class ListConnectorsInput(BaseModel):
    """Input for list_connectors - no required parameters."""

    pass


class ConnectorInfo(BaseModel):
    """Information about a single connector."""

    connector_id: str = Field(description="Unique identifier for the connector")
    name: str = Field(description="Human-readable name")
    description: str | None = Field(default=None, description="Connector description")
    connector_type: str = Field(description="Type: rest, soap, vmware, kubernetes, etc.")
    is_active: bool = Field(default=True, description="Whether connector is active")


class ListConnectorsOutput(BaseModel):
    """Output from list_connectors."""

    connectors: list[ConnectorInfo] = Field(default_factory=list)
    total_count: int = Field(default=0)


@dataclass
class ListConnectorsTool(BaseTool[ListConnectorsInput, ListConnectorsOutput]):
    """List all available system connectors.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "list_connectors"
    TOOL_DESCRIPTION: ClassVar[str] = """List all available system connectors.
**CALL THIS FIRST** to get connector IDs for other tools.
Returns: connector_id, name, description, connector_type (rest/soap/vmware)"""
    InputSchema: ClassVar[type[BaseModel]] = ListConnectorsInput
    OutputSchema: ClassVar[type[BaseModel]] = ListConnectorsOutput

    async def execute(
        self,
        tool_input: ListConnectorsInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> ListConnectorsOutput:
        """Execute list_connectors tool."""
        import json

        await emitter.tool_start(self.TOOL_NAME)

        try:
            from meho_app.modules.agents.shared.handlers import list_connectors_handler

            result_str = await list_connectors_handler(deps, {})
            result_data = json.loads(result_str)

            if isinstance(result_data, dict) and "error" in result_data:
                await emitter.tool_complete(self.TOOL_NAME, success=False)
                return ListConnectorsOutput(connectors=[], total_count=0)

            connectors = [
                ConnectorInfo(
                    connector_id=c.get("id", ""),
                    name=c.get("name", ""),
                    description=c.get("description"),
                    connector_type=c.get("connector_type", "rest"),
                    is_active=c.get("is_active", True),
                )
                for c in result_data
            ]

            await emitter.tool_complete(self.TOOL_NAME, success=True)
            return ListConnectorsOutput(connectors=connectors, total_count=len(connectors))

        except Exception:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            raise
