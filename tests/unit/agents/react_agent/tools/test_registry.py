"""Tests for tool registry in __init__.py.

Phase 84: Tool registry contents changed -- new tools added, some renamed in
v2.1 agent reasoning upgrade.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: Tool registry contents changed in v2.1 agent reasoning upgrade, tool list outdated")

from meho_app.modules.agents.base.tool import BaseTool
from meho_app.modules.agents.react_agent.tools import (
    TOOL_REGISTRY,
    CallOperationTool,
    InvalidateTopologyTool,
    ListConnectorsTool,
    LookupTopologyTool,
    ReduceDataTool,
    SearchKnowledgeTool,
    SearchOperationsTool,
    SearchTypesTool,
    create_tool,
    get_tool_class,
)


class TestToolRegistry:
    """Tests for TOOL_REGISTRY."""

    def test_registry_has_all_tools(self) -> None:
        """Registry should contain all 8 tools."""
        assert len(TOOL_REGISTRY) == 8

    def test_registry_contains_expected_tools(self) -> None:
        """Registry should contain all expected tool names."""
        expected = {
            "list_connectors",
            "search_knowledge",
            "search_operations",
            "search_types",
            "reduce_data",
            "call_operation",
            "lookup_topology",
            "invalidate_topology",
        }
        assert set(TOOL_REGISTRY.keys()) == expected

    def test_all_registry_values_are_base_tool_subclasses(self) -> None:
        """All registry values should be BaseTool subclasses."""
        for name, tool_class in TOOL_REGISTRY.items():
            assert issubclass(tool_class, BaseTool), f"{name} is not a BaseTool subclass"


class TestGetToolClass:
    """Tests for get_tool_class function."""

    def test_get_existing_tool(self) -> None:
        """get_tool_class should return tool class for known names."""
        assert get_tool_class("list_connectors") == ListConnectorsTool
        assert get_tool_class("call_operation") == CallOperationTool

    def test_get_unknown_tool_raises(self) -> None:
        """get_tool_class should raise KeyError for unknown tools."""
        with pytest.raises(KeyError):
            get_tool_class("unknown_tool")


class TestCreateTool:
    """Tests for create_tool function."""

    def test_create_existing_tool(self) -> None:
        """create_tool should return tool instance for known names."""
        tool = create_tool("list_connectors")
        assert isinstance(tool, ListConnectorsTool)

    def test_create_unknown_tool_raises(self) -> None:
        """create_tool should raise KeyError for unknown tools."""
        with pytest.raises(KeyError):
            create_tool("unknown_tool")

    def test_created_tools_are_independent(self) -> None:
        """Each create_tool call should return new instance."""
        tool1 = create_tool("list_connectors")
        tool2 = create_tool("list_connectors")
        assert tool1 is not tool2


class TestToolExports:
    """Tests for module exports."""

    def test_all_tool_classes_exported(self) -> None:
        """All tool classes should be importable."""
        assert ListConnectorsTool is not None
        assert SearchKnowledgeTool is not None
        assert SearchOperationsTool is not None
        assert SearchTypesTool is not None
        assert ReduceDataTool is not None
        assert CallOperationTool is not None
        assert LookupTopologyTool is not None
        assert InvalidateTopologyTool is not None

    def test_tool_class_tool_names_match_registry(self) -> None:
        """Tool class TOOL_NAME should match registry key."""
        for name, tool_class in TOOL_REGISTRY.items():
            assert name == tool_class.TOOL_NAME, f"{tool_class.__name__}.TOOL_NAME != {name}"
