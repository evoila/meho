# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for InvalidateTopologyTool."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meho_app.modules.agents.base.tool import BaseTool
from meho_app.modules.agents.react_agent.tools.invalidate_topology import (
    InvalidateTopologyInput,
    InvalidateTopologyOutput,
    InvalidateTopologyTool,
)


class TestInvalidateTopologyTool:
    """Tests for InvalidateTopologyTool class."""

    def test_is_base_tool_subclass(self) -> None:
        """Tool should be a BaseTool subclass."""
        assert issubclass(InvalidateTopologyTool, BaseTool)

    def test_tool_name(self) -> None:
        """Tool should have correct TOOL_NAME."""
        assert InvalidateTopologyTool.TOOL_NAME == "invalidate_topology"

    def test_tool_description_not_empty(self) -> None:
        """Tool should have non-empty TOOL_DESCRIPTION."""
        assert InvalidateTopologyTool.TOOL_DESCRIPTION
        assert len(InvalidateTopologyTool.TOOL_DESCRIPTION) > 10

    def test_input_schema(self) -> None:
        """Tool should have correct InputSchema."""
        assert InvalidateTopologyTool.InputSchema == InvalidateTopologyInput

    def test_output_schema(self) -> None:
        """Tool should have correct OutputSchema."""
        assert InvalidateTopologyTool.OutputSchema == InvalidateTopologyOutput

    def test_get_description_for_llm(self) -> None:
        """get_description_for_llm should return formatted string."""
        desc = InvalidateTopologyTool.get_description_for_llm()
        assert "invalidate_topology" in desc

    def test_instantiation(self) -> None:
        """Tool should be instantiable."""
        tool = InvalidateTopologyTool()
        assert tool.TOOL_NAME == "invalidate_topology"


class TestInvalidateTopologyInput:
    """Tests for InvalidateTopologyInput schema."""

    def test_required_fields(self) -> None:
        """Input should require entity_name and reason."""
        with pytest.raises(ValidationError):
            InvalidateTopologyInput()

    def test_valid_input(self) -> None:
        """Valid input should be accepted."""
        input_model = InvalidateTopologyInput(
            entity_name="shop-ingress",
            reason="404 from K8s API",
        )
        assert input_model.entity_name == "shop-ingress"
        assert input_model.reason == "404 from K8s API"


class TestInvalidateTopologyOutput:
    """Tests for InvalidateTopologyOutput schema."""

    def test_default_values(self) -> None:
        """Output should have sensible defaults."""
        output = InvalidateTopologyOutput()
        assert output.success is True
        assert output.message == ""

    def test_with_message(self) -> None:
        """Output should accept message."""
        output = InvalidateTopologyOutput(
            success=True,
            message="Entity marked as stale",
        )
        assert output.success is True
        assert output.message == "Entity marked as stale"
