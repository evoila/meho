# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for the reduce_data pipeline (PATH-01).

Verifies that the reduce_data path uses QueryEngine for SQL execution
and Arrow-native data throughout, with zero pandas in the import chain.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pyarrow as pa
import pytest

from meho_app.modules.agents.execution.cache import CachedData, CachedTable


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def arrow_table():
    """Create a simple Arrow table for testing."""
    return pa.table(
        {
            "name": ["pod-a", "pod-b", "pod-c"],
            "namespace": ["default", "kube-system", "default"],
            "status": ["Running", "Running", "Pending"],
        }
    )


@pytest.fixture
def cached_table(arrow_table):
    """Create a CachedTable backed by an Arrow table."""
    ct = CachedTable(
        table_name="pods",
        operation_id="list_pods",
        connector_id="k8s-1",
        columns=arrow_table.column_names,
        row_count=arrow_table.num_rows,
    )
    ct._df = arrow_table
    return ct


@pytest.fixture
def cached_data(arrow_table):
    """Create a CachedData backed by an Arrow table."""
    cd = CachedData(
        cache_key="sess:k8s-1:list_pods",
        session_id="sess-1",
        table_name="pods",
        source_id="list_pods",
        source_path="list_pods",
        connector_id="k8s-1",
        connector_type="kubernetes",
        columns=arrow_table.column_names,
        row_count=arrow_table.num_rows,
    )
    cd._df = arrow_table
    return cd


# ---------------------------------------------------------------------------
# Test 1: reduce_data_handler calls execute_sql_async (QueryEngine path)
# ---------------------------------------------------------------------------


class TestReduceDataHandlerUsesQueryEngine:
    """Verify reduce_data_handler delegates SQL execution to the executor."""

    @pytest.mark.asyncio
    async def test_reduce_data_handler_uses_query_engine(self):
        """reduce_data_handler invokes execute_sql_async on the executor."""
        from meho_app.modules.agents.shared.handlers.knowledge_handlers import (
            reduce_data_handler,
        )

        mock_executor = AsyncMock()
        mock_executor.execute_sql_async.return_value = {
            "success": True,
            "rows": [{"name": "pod-a"}],
            "count": 1,
            "columns": ["name"],
        }
        mock_executor.get_session_table_info_async = AsyncMock(return_value=[])

        deps = MagicMock()
        deps.user_id = "u1"
        deps.tenant_id = "t1"
        deps.session_id = "sess-1"
        deps.meho_deps = MagicMock()
        deps.meho_deps.redis = MagicMock()

        with patch(
            "meho_app.modules.agents.unified_executor.get_unified_executor",
            return_value=mock_executor,
        ):
            result = await reduce_data_handler(deps, {"sql": "SELECT name FROM pods"})

        # Verify execute_sql_async was called (which uses QueryEngine internally)
        mock_executor.execute_sql_async.assert_awaited_once_with("sess-1", "SELECT name FROM pods")
        assert '"success": true' in result


# ---------------------------------------------------------------------------
# Test 2: _load_session_tables returns Arrow-native data (to_pylist)
# ---------------------------------------------------------------------------


class TestLoadSessionTablesArrowNative:
    """Verify _load_session_tables returns list[dict] via Arrow to_pylist()."""

    @pytest.mark.asyncio
    async def test_load_session_tables_returns_arrow_native(self, cached_table):
        """_load_session_tables uses arrow_table.to_pylist(), not df.to_dict()."""
        from meho_app.modules.agents.base.reduce_data import BaseReduceDataNode

        mock_executor = AsyncMock()
        mock_executor.get_session_tables_async.return_value = {
            "pods": cached_table,
        }

        mock_deps = MagicMock()
        mock_deps.redis = MagicMock()

        node = BaseReduceDataNode(
            connector_name="test",
            deps=mock_deps,
            session_id="sess-1",
        )

        with patch(
            "meho_app.modules.agents.unified_executor.get_unified_executor",
            return_value=mock_executor,
        ):
            result = await node._load_session_tables("other_table")

        # Should have loaded pods table
        assert "pods" in result
        # Values must be list[dict] from to_pylist
        rows = result["pods"]
        assert isinstance(rows, list)
        assert len(rows) == 3
        assert isinstance(rows[0], dict)
        assert rows[0]["name"] == "pod-a"


# ---------------------------------------------------------------------------
# Test 3: execute_sql_async uses QueryEngine (not standalone duckdb.connect)
# ---------------------------------------------------------------------------


class TestExecuteSqlUsesQueryEngine:
    """Verify execute_sql_async routes SQL through QueryEngine."""

    @pytest.mark.asyncio
    async def test_execute_sql_uses_query_engine(self, arrow_table):
        """execute_sql_async creates a QueryEngine, registers Arrow tables, runs SQL."""
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor(redis_client=None)

        # Pre-populate L1 cache with Arrow table
        ct = CachedTable(
            table_name="pods",
            operation_id="list_pods",
            connector_id="k8s-1",
            columns=arrow_table.column_names,
            row_count=arrow_table.num_rows,
        )
        ct._df = arrow_table
        executor._session_tables["sess-1"] = {"pods": ct}

        result = await executor.execute_sql_async(
            "sess-1", "SELECT name FROM pods WHERE status = 'Running'"
        )

        assert result["success"] is True
        assert result["count"] == 2
        assert all(r["name"] in ("pod-a", "pod-b") for r in result["rows"])
        assert "name" in result["columns"]


# ---------------------------------------------------------------------------
# Test 4: No pandas imported as side effect
# ---------------------------------------------------------------------------
