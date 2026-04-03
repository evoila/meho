# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for shared flow module.

Tests the execute_workflow function and its protocol classes.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_app.modules.agents.shared import WorkflowResult, execute_workflow


class TestExecuteWorkflowSignature:
    """Tests for execute_workflow function signature and basic behavior."""

    @pytest.mark.asyncio
    async def test_returns_workflow_result(self):
        """execute_workflow returns a WorkflowResult."""
        # Create mock node classes
        mock_search_intent_node = MagicMock()
        mock_search_intent_instance = AsyncMock()
        mock_search_intent_instance.run = AsyncMock(
            return_value=MagicMock(
                use_cached_data=False,
                cached_table_name=None,
                query="list vms",
            )
        )
        mock_search_intent_node.return_value = mock_search_intent_instance

        mock_search_ops_node = MagicMock()
        mock_search_ops_instance = AsyncMock()
        mock_search_ops_instance.run = AsyncMock(return_value=[])  # No operations
        mock_search_ops_node.return_value = mock_search_ops_instance

        mock_deps = MagicMock()
        mock_deps.unified_executor = None  # No unified executor

        result = await execute_workflow(
            user_goal="List all VMs",
            connector_id="conn-123",
            connector_name="vSphere",
            connector_type="vmware",
            deps=mock_deps,
            search_intent_node_cls=mock_search_intent_node,
            search_operations_node_cls=mock_search_ops_node,
            select_operation_node_cls=MagicMock(),
            call_operation_node_cls=MagicMock(),
            reduce_data_node_cls=MagicMock(),
            no_relevant_operation_cls=type("NoOp", (), {}),
            operation_selection_cls=type("OpSel", (), {}),
        )

        assert isinstance(result, WorkflowResult)
        assert result.success is True
        assert "No operations found" in result.findings

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        """execute_workflow handles exceptions gracefully."""
        mock_search_intent_node = MagicMock()
        mock_search_intent_instance = AsyncMock()
        mock_search_intent_instance.run = AsyncMock(side_effect=RuntimeError("Test error"))
        mock_search_intent_node.return_value = mock_search_intent_instance

        mock_deps = MagicMock()
        mock_deps.unified_executor = None

        result = await execute_workflow(
            user_goal="List all VMs",
            connector_id="conn-123",
            connector_name="vSphere",
            connector_type="vmware",
            deps=mock_deps,
            search_intent_node_cls=mock_search_intent_node,
            search_operations_node_cls=MagicMock(),
            select_operation_node_cls=MagicMock(),
            call_operation_node_cls=MagicMock(),
            reduce_data_node_cls=MagicMock(),
            no_relevant_operation_cls=type("NoOp", (), {}),
            operation_selection_cls=type("OpSel", (), {}),
        )

        assert isinstance(result, WorkflowResult)
        assert result.success is False
        assert result.error == "Test error"


class TestExecuteWorkflowCachedDataPath:
    """Tests for the cached data early exit path."""

    @pytest.mark.asyncio
    async def test_early_exit_with_cached_data(self):
        """execute_workflow takes early exit when use_cached_data is True."""
        # Mock search intent that indicates cached data should be used
        mock_search_intent_node = MagicMock()
        mock_search_intent_instance = AsyncMock()
        mock_search_intent_instance.run = AsyncMock(
            return_value=MagicMock(
                use_cached_data=True,
                cached_table_name="vms",
                query="list vms",
            )
        )
        mock_search_intent_node.return_value = mock_search_intent_instance

        # Mock reduce node that will be called for early exit
        mock_reduce_node = MagicMock()
        mock_reduce_instance = AsyncMock()
        mock_reduce_instance.run = AsyncMock(return_value="| name |\n| --- |\n| vm1 |\n| vm2 |")
        mock_reduce_node.return_value = mock_reduce_instance

        # Mock deps with unified_executor that returns cached table info
        mock_deps = MagicMock()
        mock_deps.unified_executor = MagicMock()
        mock_deps.unified_executor.get_session_table_info_async = AsyncMock(
            return_value=[
                {
                    "table": "vms",
                    "row_count": 10,
                    "columns": ["name", "id"],
                    "entity_type": "vm",
                    "identifier_field": "id",
                    "display_name_field": "name",
                }
            ]
        )

        result = await execute_workflow(
            user_goal="Show VMs from cache",
            connector_id="conn-123",
            connector_name="vSphere",
            connector_type="vmware",
            deps=mock_deps,
            search_intent_node_cls=mock_search_intent_node,
            search_operations_node_cls=MagicMock(),  # Should not be called
            select_operation_node_cls=MagicMock(),  # Should not be called
            call_operation_node_cls=MagicMock(),  # Should not be called
            reduce_data_node_cls=mock_reduce_node,
            no_relevant_operation_cls=type("NoOp", (), {}),
            operation_selection_cls=type("OpSel", (), {}),
            session_id="session-123",
        )

        assert result.success is True
        assert "vm1" in result.findings
        # Verify reduce node was called
        mock_reduce_instance.run.assert_called_once()
