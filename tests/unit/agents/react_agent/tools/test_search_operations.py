# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for SearchOperationsTool."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meho_app.modules.agents.base.tool import BaseTool
from meho_app.modules.agents.react_agent.tools.search_operations import (
    SearchOperationsInput,
    SearchOperationsOutput,
    SearchOperationsTool,
)


class TestSearchOperationsTool:
    """Tests for SearchOperationsTool class."""

    def test_is_base_tool_subclass(self) -> None:
        """Tool should be a BaseTool subclass."""
        assert issubclass(SearchOperationsTool, BaseTool)

    def test_tool_name(self) -> None:
        """Tool should have correct TOOL_NAME."""
        assert SearchOperationsTool.TOOL_NAME == "search_operations"

    def test_tool_description_not_empty(self) -> None:
        """Tool should have non-empty TOOL_DESCRIPTION."""
        assert SearchOperationsTool.TOOL_DESCRIPTION
        assert len(SearchOperationsTool.TOOL_DESCRIPTION) > 10

    def test_input_schema(self) -> None:
        """Tool should have correct InputSchema."""
        assert SearchOperationsTool.InputSchema == SearchOperationsInput

    def test_output_schema(self) -> None:
        """Tool should have correct OutputSchema."""
        assert SearchOperationsTool.OutputSchema == SearchOperationsOutput

    def test_get_description_for_llm(self) -> None:
        """get_description_for_llm should return formatted string."""
        desc = SearchOperationsTool.get_description_for_llm()
        assert "search_operations" in desc

    def test_instantiation(self) -> None:
        """Tool should be instantiable."""
        tool = SearchOperationsTool()
        assert tool.TOOL_NAME == "search_operations"


class TestSearchOperationsInput:
    """Tests for SearchOperationsInput schema."""

    def test_required_fields(self) -> None:
        """Input should require connector_id and query."""
        with pytest.raises(ValidationError):
            SearchOperationsInput()

    def test_valid_input(self) -> None:
        """Valid input should be accepted."""
        input_model = SearchOperationsInput(
            connector_id="abc-123",
            query="list virtual machines",
        )
        assert input_model.connector_id == "abc-123"
        assert input_model.query == "list virtual machines"
        assert input_model.limit == 10  # default


class TestSearchOperationsOutput:
    """Tests for SearchOperationsOutput schema."""

    def test_default_values(self) -> None:
        """Output should have sensible defaults."""
        output = SearchOperationsOutput()
        assert output.operations == []
        assert output.total_found == 0
        assert output.connector_type is None
