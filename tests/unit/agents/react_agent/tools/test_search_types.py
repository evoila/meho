# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for SearchTypesTool."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meho_app.modules.agents.base.tool import BaseTool
from meho_app.modules.agents.react_agent.tools.search_types import (
    SearchTypesInput,
    SearchTypesOutput,
    SearchTypesTool,
)


class TestSearchTypesTool:
    """Tests for SearchTypesTool class."""

    def test_is_base_tool_subclass(self) -> None:
        """Tool should be a BaseTool subclass."""
        assert issubclass(SearchTypesTool, BaseTool)

    def test_tool_name(self) -> None:
        """Tool should have correct TOOL_NAME."""
        assert SearchTypesTool.TOOL_NAME == "search_types"

    def test_tool_description_not_empty(self) -> None:
        """Tool should have non-empty TOOL_DESCRIPTION."""
        assert SearchTypesTool.TOOL_DESCRIPTION
        assert len(SearchTypesTool.TOOL_DESCRIPTION) > 10

    def test_input_schema(self) -> None:
        """Tool should have correct InputSchema."""
        assert SearchTypesTool.InputSchema == SearchTypesInput

    def test_output_schema(self) -> None:
        """Tool should have correct OutputSchema."""
        assert SearchTypesTool.OutputSchema == SearchTypesOutput

    def test_get_description_for_llm(self) -> None:
        """get_description_for_llm should return formatted string."""
        desc = SearchTypesTool.get_description_for_llm()
        assert "search_types" in desc

    def test_instantiation(self) -> None:
        """Tool should be instantiable."""
        tool = SearchTypesTool()
        assert tool.TOOL_NAME == "search_types"


class TestSearchTypesInput:
    """Tests for SearchTypesInput schema."""

    def test_required_fields(self) -> None:
        """Input should require connector_id and query."""
        with pytest.raises(ValidationError):
            SearchTypesInput()

    def test_valid_input(self) -> None:
        """Valid input should be accepted."""
        input_model = SearchTypesInput(
            connector_id="abc-123",
            query="VirtualMachine",
        )
        assert input_model.connector_id == "abc-123"
        assert input_model.query == "VirtualMachine"
        assert input_model.limit == 10  # default


class TestSearchTypesOutput:
    """Tests for SearchTypesOutput schema."""

    def test_default_values(self) -> None:
        """Output should have sensible defaults."""
        output = SearchTypesOutput()
        assert output.types == []
        assert output.total_found == 0
