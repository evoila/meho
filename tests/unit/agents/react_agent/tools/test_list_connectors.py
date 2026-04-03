# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for ListConnectorsTool."""

from __future__ import annotations

from meho_app.modules.agents.base.tool import BaseTool
from meho_app.modules.agents.react_agent.tools.list_connectors import (
    ListConnectorsInput,
    ListConnectorsOutput,
    ListConnectorsTool,
)


class TestListConnectorsTool:
    """Tests for ListConnectorsTool class."""

    def test_is_base_tool_subclass(self) -> None:
        """Tool should be a BaseTool subclass."""
        assert issubclass(ListConnectorsTool, BaseTool)

    def test_tool_name(self) -> None:
        """Tool should have correct TOOL_NAME."""
        assert ListConnectorsTool.TOOL_NAME == "list_connectors"

    def test_tool_description_not_empty(self) -> None:
        """Tool should have non-empty TOOL_DESCRIPTION."""
        assert ListConnectorsTool.TOOL_DESCRIPTION
        assert len(ListConnectorsTool.TOOL_DESCRIPTION) > 10

    def test_input_schema(self) -> None:
        """Tool should have correct InputSchema."""
        assert ListConnectorsTool.InputSchema == ListConnectorsInput

    def test_output_schema(self) -> None:
        """Tool should have correct OutputSchema."""
        assert ListConnectorsTool.OutputSchema == ListConnectorsOutput

    def test_get_description_for_llm(self) -> None:
        """get_description_for_llm should return formatted string."""
        desc = ListConnectorsTool.get_description_for_llm()
        assert "list_connectors" in desc
        assert desc.startswith("- **")

    def test_validate_input_empty(self) -> None:
        """validate_input should accept empty dict."""
        result = ListConnectorsTool.validate_input({})
        assert isinstance(result, ListConnectorsInput)

    def test_instantiation(self) -> None:
        """Tool should be instantiable."""
        tool = ListConnectorsTool()
        assert tool.TOOL_NAME == "list_connectors"


class TestListConnectorsInput:
    """Tests for ListConnectorsInput schema."""

    def test_no_required_fields(self) -> None:
        """Input should have no required fields."""
        input_model = ListConnectorsInput()
        assert input_model is not None  # NOSONAR -- intentional identity check


class TestListConnectorsOutput:
    """Tests for ListConnectorsOutput schema."""

    def test_default_values(self) -> None:
        """Output should have sensible defaults."""
        output = ListConnectorsOutput()
        assert output.connectors == []
        assert output.total_count == 0

    def test_with_connectors(self) -> None:
        """Output should accept connector list."""
        from meho_app.modules.agents.react_agent.tools.list_connectors import ConnectorInfo

        output = ListConnectorsOutput(
            connectors=[
                ConnectorInfo(
                    connector_id="123",
                    name="Test",
                    connector_type="rest",
                )
            ],
            total_count=1,
        )
        assert len(output.connectors) == 1
        assert output.total_count == 1
