# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for CallOperationTool."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meho_app.modules.agents.base.tool import BaseTool
from meho_app.modules.agents.react_agent.tools.call_operation import (
    CallOperationInput,
    CallOperationOutput,
    CallOperationTool,
)


class TestCallOperationTool:
    """Tests for CallOperationTool class."""

    def test_is_base_tool_subclass(self) -> None:
        """Tool should be a BaseTool subclass."""
        assert issubclass(CallOperationTool, BaseTool)

    def test_tool_name(self) -> None:
        """Tool should have correct TOOL_NAME."""
        assert CallOperationTool.TOOL_NAME == "call_operation"

    def test_tool_description_not_empty(self) -> None:
        """Tool should have non-empty TOOL_DESCRIPTION."""
        assert CallOperationTool.TOOL_DESCRIPTION
        assert len(CallOperationTool.TOOL_DESCRIPTION) > 10

    def test_input_schema(self) -> None:
        """Tool should have correct InputSchema."""
        assert CallOperationTool.InputSchema == CallOperationInput

    def test_output_schema(self) -> None:
        """Tool should have correct OutputSchema."""
        assert CallOperationTool.OutputSchema == CallOperationOutput

    def test_get_description_for_llm(self) -> None:
        """get_description_for_llm should return formatted string."""
        desc = CallOperationTool.get_description_for_llm()
        assert "call_operation" in desc

    def test_instantiation(self) -> None:
        """Tool should be instantiable."""
        tool = CallOperationTool()
        assert tool.TOOL_NAME == "call_operation"


class TestCallOperationInput:
    """Tests for CallOperationInput schema."""

    def test_required_fields(self) -> None:
        """Input should require connector_id and operation_id."""
        with pytest.raises(ValidationError):
            CallOperationInput()

    def test_valid_input(self) -> None:
        """Valid input should be accepted."""
        input_model = CallOperationInput(
            connector_id="abc-123",
            operation_id="list_vms",
        )
        assert input_model.connector_id == "abc-123"
        assert input_model.operation_id == "list_vms"
        assert input_model.parameter_sets == [{}]  # default

    def test_with_parameter_sets(self) -> None:
        """Input should accept parameter_sets."""
        input_model = CallOperationInput(
            connector_id="abc-123",
            operation_id="get_vm",
            parameter_sets=[{"vm_id": "vm-1"}, {"vm_id": "vm-2"}],
        )
        assert len(input_model.parameter_sets) == 2


class TestCallOperationOutput:
    """Tests for CallOperationOutput schema."""

    def test_default_values(self) -> None:
        """Output should have sensible defaults."""
        output = CallOperationOutput()
        assert output.results == []
        assert output.data_available is False
        assert output.success is True
        assert output.error is None

    def test_with_data(self) -> None:
        """Output should accept data."""
        output = CallOperationOutput(
            results=[{"success": True, "data": []}],
            data_available=True,
            table="virtual_machines",
            row_count=10,
        )
        assert output.data_available is True
        assert output.table == "virtual_machines"
