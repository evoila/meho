# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for SearchKnowledgeTool."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meho_app.modules.agents.base.tool import BaseTool
from meho_app.modules.agents.react_agent.tools.search_knowledge import (
    SearchKnowledgeInput,
    SearchKnowledgeOutput,
    SearchKnowledgeTool,
)


class TestSearchKnowledgeTool:
    """Tests for SearchKnowledgeTool class."""

    def test_is_base_tool_subclass(self) -> None:
        """Tool should be a BaseTool subclass."""
        assert issubclass(SearchKnowledgeTool, BaseTool)

    def test_tool_name(self) -> None:
        """Tool should have correct TOOL_NAME."""
        assert SearchKnowledgeTool.TOOL_NAME == "search_knowledge"

    def test_tool_description_not_empty(self) -> None:
        """Tool should have non-empty TOOL_DESCRIPTION."""
        assert SearchKnowledgeTool.TOOL_DESCRIPTION
        assert len(SearchKnowledgeTool.TOOL_DESCRIPTION) > 10

    def test_input_schema(self) -> None:
        """Tool should have correct InputSchema."""
        assert SearchKnowledgeTool.InputSchema == SearchKnowledgeInput

    def test_output_schema(self) -> None:
        """Tool should have correct OutputSchema."""
        assert SearchKnowledgeTool.OutputSchema == SearchKnowledgeOutput

    def test_get_description_for_llm(self) -> None:
        """get_description_for_llm should return formatted string."""
        desc = SearchKnowledgeTool.get_description_for_llm()
        assert "search_knowledge" in desc

    def test_instantiation(self) -> None:
        """Tool should be instantiable."""
        tool = SearchKnowledgeTool()
        assert tool.TOOL_NAME == "search_knowledge"


class TestSearchKnowledgeInput:
    """Tests for SearchKnowledgeInput schema."""

    def test_query_required(self) -> None:
        """Input should require query field."""
        with pytest.raises(ValidationError):
            SearchKnowledgeInput()

    def test_query_min_length(self) -> None:
        """Query should have min length of 1."""
        with pytest.raises(ValidationError):
            SearchKnowledgeInput(query="")

    def test_valid_input(self) -> None:
        """Valid input should be accepted."""
        input_model = SearchKnowledgeInput(query="test query")
        assert input_model.query == "test query"
        assert input_model.limit == 5  # default
        assert input_model.include_apis is False  # default

    def test_limit_bounds(self) -> None:
        """Limit should be within bounds."""
        with pytest.raises(ValidationError):
            SearchKnowledgeInput(query="test", limit=0)
        with pytest.raises(ValidationError):
            SearchKnowledgeInput(query="test", limit=21)


class TestSearchKnowledgeOutput:
    """Tests for SearchKnowledgeOutput schema."""

    def test_default_values(self) -> None:
        """Output should have sensible defaults."""
        output = SearchKnowledgeOutput()
        assert output.results == []
        assert output.total_found == 0
