# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for ReduceDataTool."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meho_app.modules.agents.base.tool import BaseTool
from meho_app.modules.agents.react_agent.tools.reduce_data import (
    ReduceDataInput,
    ReduceDataOutput,
    ReduceDataTool,
)


class TestReduceDataTool:
    """Tests for ReduceDataTool class."""

    def test_is_base_tool_subclass(self) -> None:
        """Tool should be a BaseTool subclass."""
        assert issubclass(ReduceDataTool, BaseTool)

    def test_tool_name(self) -> None:
        """Tool should have correct TOOL_NAME."""
        assert ReduceDataTool.TOOL_NAME == "reduce_data"

    def test_tool_description_not_empty(self) -> None:
        """Tool should have non-empty TOOL_DESCRIPTION."""
        assert ReduceDataTool.TOOL_DESCRIPTION
        assert len(ReduceDataTool.TOOL_DESCRIPTION) > 10

    def test_input_schema(self) -> None:
        """Tool should have correct InputSchema."""
        assert ReduceDataTool.InputSchema == ReduceDataInput

    def test_output_schema(self) -> None:
        """Tool should have correct OutputSchema."""
        assert ReduceDataTool.OutputSchema == ReduceDataOutput

    def test_get_description_for_llm(self) -> None:
        """get_description_for_llm should return formatted string."""
        desc = ReduceDataTool.get_description_for_llm()
        assert "reduce_data" in desc

    def test_instantiation(self) -> None:
        """Tool should be instantiable."""
        tool = ReduceDataTool()
        assert tool.TOOL_NAME == "reduce_data"


class TestReduceDataInput:
    """Tests for ReduceDataInput schema."""

    def test_sql_required(self) -> None:
        """Input should require sql field."""
        with pytest.raises(ValidationError):
            ReduceDataInput()

    def test_sql_min_length(self) -> None:
        """SQL should have min length of 1."""
        with pytest.raises(ValidationError):
            ReduceDataInput(sql="")

    def test_valid_input(self) -> None:
        """Valid input should be accepted."""
        input_model = ReduceDataInput(sql="SELECT * FROM vms")
        assert input_model.sql == "SELECT * FROM vms"


class TestReduceDataOutput:
    """Tests for ReduceDataOutput schema."""

    def test_default_values(self) -> None:
        """Output should have sensible defaults."""
        output = ReduceDataOutput()
        assert output.rows == []
        assert output.columns == []
        assert output.row_count == 0
        assert output.success is True
        assert output.error is None

    def test_with_data(self) -> None:
        """Output should accept data."""
        output = ReduceDataOutput(
            rows=[{"id": 1, "name": "vm1"}],
            columns=["id", "name"],
            row_count=1,
        )
        assert len(output.rows) == 1
        assert output.row_count == 1
