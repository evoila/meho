# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for LookupTopologyTool."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meho_app.modules.agents.base.tool import BaseTool
from meho_app.modules.agents.react_agent.tools.lookup_topology import (
    LookupTopologyInput,
    LookupTopologyOutput,
    LookupTopologyTool,
)


class TestLookupTopologyTool:
    """Tests for LookupTopologyTool class."""

    def test_is_base_tool_subclass(self) -> None:
        """Tool should be a BaseTool subclass."""
        assert issubclass(LookupTopologyTool, BaseTool)

    def test_tool_name(self) -> None:
        """Tool should have correct TOOL_NAME."""
        assert LookupTopologyTool.TOOL_NAME == "lookup_topology"

    def test_tool_description_not_empty(self) -> None:
        """Tool should have non-empty TOOL_DESCRIPTION."""
        assert LookupTopologyTool.TOOL_DESCRIPTION
        assert len(LookupTopologyTool.TOOL_DESCRIPTION) > 10

    def test_input_schema(self) -> None:
        """Tool should have correct InputSchema."""
        assert LookupTopologyTool.InputSchema == LookupTopologyInput

    def test_output_schema(self) -> None:
        """Tool should have correct OutputSchema."""
        assert LookupTopologyTool.OutputSchema == LookupTopologyOutput

    def test_get_description_for_llm(self) -> None:
        """get_description_for_llm should return formatted string."""
        desc = LookupTopologyTool.get_description_for_llm()
        assert "lookup_topology" in desc

    def test_instantiation(self) -> None:
        """Tool should be instantiable."""
        tool = LookupTopologyTool()
        assert tool.TOOL_NAME == "lookup_topology"


class TestLookupTopologyInput:
    """Tests for LookupTopologyInput schema."""

    def test_query_required(self) -> None:
        """Input should require query field."""
        with pytest.raises(ValidationError):
            LookupTopologyInput()

    def test_valid_input(self) -> None:
        """Valid input should be accepted."""
        input_model = LookupTopologyInput(query="shop.example.com")
        assert input_model.query == "shop.example.com"
        assert input_model.traverse_depth == 10  # default
        assert input_model.cross_connectors is True  # default

    def test_traverse_depth_bounds(self) -> None:
        """traverse_depth should be within bounds."""
        with pytest.raises(ValidationError):
            LookupTopologyInput(query="test", traverse_depth=0)
        with pytest.raises(ValidationError):
            LookupTopologyInput(query="test", traverse_depth=21)


class TestLookupTopologyOutput:
    """Tests for LookupTopologyOutput schema."""

    def test_default_values(self) -> None:
        """Output should have sensible defaults."""
        output = LookupTopologyOutput()
        assert output.found is False
        assert output.entity is None
        assert output.topology_chain == []
        assert output.same_as_entities == []
        assert output.possibly_related == []
        assert output.suggestions == []
