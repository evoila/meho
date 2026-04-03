# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for BaseTool abstract base class.

These tests verify:
1. BaseTool is an ABC and cannot be instantiated directly
2. Concrete implementations must implement execute()
3. validate_input() works with Pydantic models
4. get_description_for_llm() formats correctly
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ValidationError

from meho_app.modules.agents.base import BaseTool


class SampleInput(BaseModel):
    """Sample input schema for testing."""

    query: str
    limit: int = 10


class SampleOutput(BaseModel):
    """Sample output schema for testing."""

    results: list[str]
    total: int


class TestBaseToolContract:
    """Tests for the BaseTool contract."""

    def test_base_tool_is_abc(self) -> None:
        """BaseTool should be an ABC."""
        assert issubclass(BaseTool, ABC)

    def test_base_tool_cannot_be_instantiated(self) -> None:
        """BaseTool cannot be instantiated directly without implementing execute."""

        @dataclass
        class IncompleteTool(BaseTool[SampleInput, SampleOutput]):
            TOOL_NAME = "incomplete"
            TOOL_DESCRIPTION = "A tool without execute"
            InputSchema = SampleInput
            OutputSchema = SampleOutput
            # Missing execute() implementation

        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            IncompleteTool()  # type: ignore[abstract]

    def test_base_tool_importable_from_base(self) -> None:
        """BaseTool should be importable from meho_app.modules.agents.base."""
        from meho_app.modules.agents.base import BaseTool as ImportedBaseTool

        assert ImportedBaseTool is BaseTool

    def test_base_tool_has_required_abstract_methods(self) -> None:
        """BaseTool should have the execute abstract method."""
        abstract_methods = BaseTool.__abstractmethods__
        assert "execute" in abstract_methods


class TestConcreteTool:
    """Tests for concrete tool implementations."""

    def test_concrete_tool_can_be_created(self) -> None:
        """A concrete tool implementing execute can be instantiated."""

        @dataclass
        class TestTool(BaseTool[SampleInput, SampleOutput]):
            TOOL_NAME = "test_tool"
            TOOL_DESCRIPTION = "A test tool"
            InputSchema = SampleInput
            OutputSchema = SampleOutput

            def execute(
                self,
                tool_input: SampleInput,
                deps: Any,
                emitter: Any,
            ) -> SampleOutput:
                return SampleOutput(results=["result1"], total=1)

        # Should not raise
        tool = TestTool()
        assert tool.TOOL_NAME == "test_tool"
        assert tool.TOOL_DESCRIPTION == "A test tool"

    @pytest.mark.asyncio
    def test_concrete_tool_execute(self) -> None:
        """Concrete tool's execute method should work."""

        @dataclass
        class TestTool(BaseTool[SampleInput, SampleOutput]):
            TOOL_NAME = "test_tool"
            TOOL_DESCRIPTION = "A test tool"
            InputSchema = SampleInput
            OutputSchema = SampleOutput

            def execute(
                self,
                tool_input: SampleInput,
                deps: Any,
                emitter: Any,
            ) -> SampleOutput:
                return SampleOutput(
                    results=[f"Found: {tool_input.query}"],
                    total=tool_input.limit,
                )

        tool = TestTool()
        input_data = SampleInput(query="test query", limit=5)
        result = tool.execute(input_data, MagicMock(), MagicMock())

        assert result.results == ["Found: test query"]
        assert result.total == 5


class TestToolValidation:
    """Tests for tool validation methods."""

    def test_validate_input_with_valid_data(self) -> None:
        """validate_input should return validated model for valid data."""

        @dataclass
        class TestTool(BaseTool[SampleInput, SampleOutput]):
            TOOL_NAME = "test"
            TOOL_DESCRIPTION = "Test"
            InputSchema = SampleInput
            OutputSchema = SampleOutput

            def execute(
                self,
                tool_input: SampleInput,
                deps: Any,
                emitter: Any,
            ) -> SampleOutput:
                return SampleOutput(results=[], total=0)

        validated = TestTool.validate_input({"query": "test", "limit": 20})
        assert isinstance(validated, SampleInput)
        assert validated.query == "test"
        assert validated.limit == 20

    def test_validate_input_with_defaults(self) -> None:
        """validate_input should use defaults for missing optional fields."""

        @dataclass
        class TestTool(BaseTool[SampleInput, SampleOutput]):
            TOOL_NAME = "test"
            TOOL_DESCRIPTION = "Test"
            InputSchema = SampleInput
            OutputSchema = SampleOutput

            def execute(
                self,
                tool_input: SampleInput,
                deps: Any,
                emitter: Any,
            ) -> SampleOutput:
                return SampleOutput(results=[], total=0)

        validated = TestTool.validate_input({"query": "test"})
        assert validated.limit == 10  # Default value

    def test_validate_input_with_invalid_data(self) -> None:
        """validate_input should raise ValidationError for invalid data."""

        @dataclass
        class TestTool(BaseTool[SampleInput, SampleOutput]):
            TOOL_NAME = "test"
            TOOL_DESCRIPTION = "Test"
            InputSchema = SampleInput
            OutputSchema = SampleOutput

            def execute(
                self,
                tool_input: SampleInput,
                deps: Any,
                emitter: Any,
            ) -> SampleOutput:
                return SampleOutput(results=[], total=0)

        with pytest.raises(ValidationError):
            TestTool.validate_input({})  # Missing required 'query' field

    def test_validate_input_with_wrong_type(self) -> None:
        """validate_input should raise ValidationError for wrong types."""

        @dataclass
        class TestTool(BaseTool[SampleInput, SampleOutput]):
            TOOL_NAME = "test"
            TOOL_DESCRIPTION = "Test"
            InputSchema = SampleInput
            OutputSchema = SampleOutput

            def execute(
                self,
                tool_input: SampleInput,
                deps: Any,
                emitter: Any,
            ) -> SampleOutput:
                return SampleOutput(results=[], total=0)

        with pytest.raises(ValidationError):
            TestTool.validate_input({"query": "test", "limit": "not a number"})


class TestToolDescription:
    """Tests for tool description methods."""

    def test_get_description_for_llm(self) -> None:
        """get_description_for_llm should format correctly."""

        @dataclass
        class TestTool(BaseTool[SampleInput, SampleOutput]):
            TOOL_NAME = "search_items"
            TOOL_DESCRIPTION = "Search for items by query string."
            InputSchema = SampleInput
            OutputSchema = SampleOutput

            def execute(
                self,
                tool_input: SampleInput,
                deps: Any,
                emitter: Any,
            ) -> SampleOutput:
                return SampleOutput(results=[], total=0)

        description = TestTool.get_description_for_llm()
        assert "**search_items**" in description
        assert "Search for items by query string." in description

    def test_get_input_schema_json(self) -> None:
        """get_input_schema_json should return valid JSON schema."""

        @dataclass
        class TestTool(BaseTool[SampleInput, SampleOutput]):
            TOOL_NAME = "test"
            TOOL_DESCRIPTION = "Test"
            InputSchema = SampleInput
            OutputSchema = SampleOutput

            def execute(
                self,
                tool_input: SampleInput,
                deps: Any,
                emitter: Any,
            ) -> SampleOutput:
                return SampleOutput(results=[], total=0)

        schema = TestTool.get_input_schema_json()
        assert "properties" in schema
        assert "query" in schema["properties"]
        assert "limit" in schema["properties"]

    def test_get_output_schema_json(self) -> None:
        """get_output_schema_json should return valid JSON schema."""

        @dataclass
        class TestTool(BaseTool[SampleInput, SampleOutput]):
            TOOL_NAME = "test"
            TOOL_DESCRIPTION = "Test"
            InputSchema = SampleInput
            OutputSchema = SampleOutput

            def execute(
                self,
                tool_input: SampleInput,
                deps: Any,
                emitter: Any,
            ) -> SampleOutput:
                return SampleOutput(results=[], total=0)

        schema = TestTool.get_output_schema_json()
        assert "properties" in schema
        assert "results" in schema["properties"]
        assert "total" in schema["properties"]
