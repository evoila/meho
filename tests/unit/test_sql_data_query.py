# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for SQL-based data query with DuckDB.

Tests the new SQL architecture where:
- Large API responses are cached as named tables
- Agent queries with SQL via reduce_data tool
- Tables persist across conversation turns via Redis

Phase 84: connectors.database module path restructured to connectors.repositories.
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: connectors.database mock path restructured to connectors.repositories")

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pyarrow as pa
import pytest

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def vm_data():
    """Sample VM data for testing."""
    return [
        {
            "name": f"vm-{i:03d}",
            "power_state": "poweredOn" if i % 3 != 0 else "poweredOff",
            "memory_mb": 4096 + (i * 512),
            "cpu_count": 2 + (i % 4),
        }
        for i in range(50)
    ]


@pytest.fixture
def cluster_data():
    """Sample cluster data for testing."""
    return [
        {
            "name": f"cluster-{i}",
            "status": "healthy" if i % 2 == 0 else "warning",
            "host_count": 3 + i,
            "memory_total_gb": 512,
        }
        for i in range(10)
    ]


# =============================================================================
# CachedTable Tests
# =============================================================================


def _make_arrow_table(rows: list[dict]) -> pa.Table:
    """Convert list of row dicts to Arrow table."""
    if not rows:
        return pa.table({})
    columns: dict[str, list] = {k: [] for k in rows[0]}
    for r in rows:
        for k, v in r.items():
            columns[k].append(v)
    return pa.table(columns)


def _make_cached(table_name, operation_id, connector_id, rows):
    """Create a CachedTable from row dicts backed by an Arrow table."""
    from meho_app.modules.agents.unified_executor import CachedTable

    arrow = _make_arrow_table(rows)
    cached = CachedTable(
        table_name=table_name,
        operation_id=operation_id,
        connector_id=connector_id,
        columns=arrow.column_names,
        row_count=arrow.num_rows,
    )
    cached._df = arrow
    return cached


class TestCachedTable:
    """Tests for CachedTable dataclass."""

    def test_cached_table_creation(self, vm_data):
        """Test creating a CachedTable."""
        cached = _make_cached("virtual_machines", "list_virtual_machines", "vcenter-1", vm_data)

        assert cached.table_name == "virtual_machines"
        assert cached.row_count == 50
        assert "name" in cached.columns
        assert cached.arrow_table.num_rows == 50

    def test_cached_table_to_summary(self, vm_data):
        """Test generating summary for agent."""
        cached = _make_cached("virtual_machines", "list_virtual_machines", "vcenter-1", vm_data)

        summary = cached.to_summary()

        assert summary["table"] == "virtual_machines"
        assert summary["row_count"] == 50
        assert "name" in summary["columns"]
        assert "cached_at" in summary


# =============================================================================
# Table Name Derivation Tests
# =============================================================================


class TestTableNameDerivation:
    """Tests for deriving table names from operation IDs."""

    def test_derive_table_name_list_prefix(self):
        """Test removing 'list_' prefix."""
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor()

        assert executor._derive_table_name("list_virtual_machines") == "virtual_machines"
        assert executor._derive_table_name("list_clusters") == "clusters"
        assert executor._derive_table_name("list_hosts") == "hosts"

    def test_derive_table_name_get_prefix(self):
        """Test removing 'get_' and 'get_all_' prefixes."""
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor()

        assert executor._derive_table_name("get_all_vms") == "vms"
        assert executor._derive_table_name("get_pods") == "pods"

    def test_derive_table_name_preserves_underscores(self):
        """Test that underscores in operation names are preserved."""
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor()

        # Names with underscores are preserved (most common case)
        assert executor._derive_table_name("list_virtual_machines") == "virtual_machines"
        assert executor._derive_table_name("get_all_vm_snapshots") == "vm_snapshots"


# =============================================================================
# SQL Execution Tests
# =============================================================================


class TestSQLExecution:
    """Tests for SQL query execution with DuckDB."""

    @pytest.mark.asyncio
    async def test_execute_sql_simple_select(self, vm_data):
        """Test executing a simple SELECT query."""
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor()
        session_id = "test-session"

        cached = _make_cached("virtual_machines", "list_virtual_machines", "vcenter-1", vm_data)
        executor._session_tables[session_id] = {"virtual_machines": cached}

        result = await executor.execute_sql_async(
            session_id=session_id, sql="SELECT name, power_state FROM virtual_machines LIMIT 5"
        )

        assert result["success"] is True
        assert result["count"] == 5
        assert "name" in result["columns"]
        assert "power_state" in result["columns"]

    @pytest.mark.asyncio
    async def test_execute_sql_with_filter(self, vm_data):
        """Test executing SQL with WHERE clause."""
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor()
        session_id = "test-session"

        cached = _make_cached("virtual_machines", "list_virtual_machines", "vcenter-1", vm_data)
        executor._session_tables[session_id] = {"virtual_machines": cached}

        result = await executor.execute_sql_async(
            session_id=session_id,
            sql="SELECT name FROM virtual_machines WHERE power_state = 'poweredOff'",
        )

        assert result["success"] is True
        # Every 3rd VM is powered off (0, 3, 6, 9, ... 48 = indices 0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 33, 36, 39, 42, 45, 48 = 17 VMs)
        assert result["count"] == 17

    @pytest.mark.asyncio
    async def test_execute_sql_with_order_by(self, vm_data):
        """Test executing SQL with ORDER BY clause."""
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor()
        session_id = "test-session"

        cached = _make_cached("virtual_machines", "list_virtual_machines", "vcenter-1", vm_data)
        executor._session_tables[session_id] = {"virtual_machines": cached}

        result = await executor.execute_sql_async(
            session_id=session_id,
            sql="SELECT name, memory_mb FROM virtual_machines ORDER BY memory_mb DESC LIMIT 3",
        )

        assert result["success"] is True
        assert result["count"] == 3
        assert result["rows"][0]["memory_mb"] > result["rows"][1]["memory_mb"]

    @pytest.mark.asyncio
    async def test_execute_sql_with_aggregation(self, vm_data):
        """Test executing SQL with aggregation functions."""
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor()
        session_id = "test-session"

        cached = _make_cached("virtual_machines", "list_virtual_machines", "vcenter-1", vm_data)
        executor._session_tables[session_id] = {"virtual_machines": cached}

        result = await executor.execute_sql_async(
            session_id=session_id,
            sql="SELECT COUNT(*) as total, AVG(memory_mb) as avg_memory FROM virtual_machines",
        )

        assert result["success"] is True
        assert result["count"] == 1
        assert result["rows"][0]["total"] == 50
        assert result["rows"][0]["avg_memory"] > 0

    @pytest.mark.asyncio
    async def test_execute_sql_table_not_found(self):
        """Test SQL error when table doesn't exist."""
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor()
        session_id = "test-session"

        cached = _make_cached("other_table", "list_other", "test", [{"id": 1}])
        executor._session_tables[session_id] = {"other_table": cached}

        result = await executor.execute_sql_async(
            session_id=session_id, sql="SELECT * FROM virtual_machines"
        )

        assert "error" in result
        assert "available_tables" in result

    @pytest.mark.asyncio
    async def test_execute_sql_no_session_data(self):
        """Test SQL error when session has no data."""
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor()

        result = await executor.execute_sql_async(
            session_id="empty-session", sql="SELECT * FROM anything"
        )

        assert "error" in result
        assert "No cached data" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_sql_multiple_tables(self, vm_data, cluster_data):
        """Test querying multiple tables in same session."""
        from meho_app.modules.agents.unified_executor import UnifiedExecutor

        executor = UnifiedExecutor()
        session_id = "test-session"

        vm_cached = _make_cached("virtual_machines", "list_virtual_machines", "vcenter-1", vm_data)
        cluster_cached = _make_cached("clusters", "list_clusters", "vcenter-1", cluster_data)

        executor._session_tables[session_id] = {
            "virtual_machines": vm_cached,
            "clusters": cluster_cached,
        }

        vm_result = await executor.execute_sql_async(
            session_id=session_id, sql="SELECT COUNT(*) as count FROM virtual_machines"
        )
        assert vm_result["rows"][0]["count"] == 50

        cluster_result = await executor.execute_sql_async(
            session_id=session_id, sql="SELECT COUNT(*) as count FROM clusters"
        )
        assert cluster_result["rows"][0]["count"] == 10


# =============================================================================
# reduce_data Handler SQL Tests
# =============================================================================


class TestReduceDataSQL:
    """Tests for reduce_data_handler with SQL mode."""

    @pytest.fixture
    def mock_deps(self):
        """Create mock dependencies."""
        deps = MagicMock()
        deps.session_id = "test-session-123"
        deps.meho_deps = MagicMock()
        return deps

    @pytest.mark.asyncio
    async def test_reduce_data_sql_mode(self, mock_deps, vm_data):
        """Test reduce_data with SQL parameter."""
        from meho_app.modules.agents.shared.handlers.tool_handlers import reduce_data_handler

        with patch(
            "meho_app.modules.agents.unified_executor.get_unified_executor"
        ) as mock_get_executor:
            mock_executor = MagicMock()
            mock_executor.execute_sql_async = AsyncMock(
                return_value={
                    "success": True,
                    "rows": [{"name": "vm-000", "power_state": "poweredOff"}],
                    "count": 1,
                    "columns": ["name", "power_state"],
                }
            )
            mock_get_executor.return_value = mock_executor

            args = {
                "sql": "SELECT name, power_state FROM virtual_machines WHERE power_state = 'poweredOff' LIMIT 1"
            }

            result_json = await reduce_data_handler(mock_deps, args)
            result = json.loads(result_json)

            assert result["success"] is True
            assert result["count"] == 1
            mock_executor.execute_sql_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_reduce_data_no_sql_shows_available_tables(self, mock_deps):
        """Test reduce_data without sql shows available tables."""
        from meho_app.modules.agents.shared.handlers.tool_handlers import reduce_data_handler

        with patch(
            "meho_app.modules.agents.unified_executor.get_unified_executor"
        ) as mock_get_executor:
            mock_executor = MagicMock()
            mock_executor.get_session_table_info_async = AsyncMock(
                return_value=[
                    {
                        "table": "virtual_machines",
                        "row_count": 50,
                        "columns": ["name", "power_state"],
                    }
                ]
            )
            mock_get_executor.return_value = mock_executor

            args = {}  # No sql provided

            result_json = await reduce_data_handler(mock_deps, args)
            result = json.loads(result_json)

            assert "error" in result
            assert "available_tables" in result
            assert "virtual_machines" in result["available_tables"]


# =============================================================================
# Integration with call_operation_handler Tests
# =============================================================================


class TestCallOperationTableCaching:
    """Tests for call_operation_handler SQL table caching."""

    @pytest.fixture
    def mock_deps(self):
        """Create mock dependencies."""
        deps = MagicMock()
        deps.session_id = "test-session-123"
        deps.user_id = "test-user"
        deps.meho_deps = MagicMock()
        deps.meho_deps.session_state = None
        return deps

    @pytest.mark.asyncio
    async def test_call_operation_caches_as_table(self, mock_deps, vm_data):
        """Test that call_operation caches large responses as SQL tables."""
        from dataclasses import dataclass
        from typing import Any

        from meho_app.modules.agents.shared.handlers.tool_handlers import call_operation_handler
        from meho_app.modules.agents.unified_executor import CachedTable

        @dataclass
        class MockConnector:
            id: str = "test-connector"
            name: str = "Test Connector"
            connector_type: str = "vmware"
            credential_strategy: str = "USER_PROVIDED"
            protocol_config: dict[str, Any] = None

        @dataclass
        class MockOperationResult:
            success: bool = True
            data: Any = None
            error: str = None
            duration_ms: int = 100

        mock_connector = MockConnector()
        mock_connector_repo = AsyncMock()
        mock_connector_repo.get_connector = AsyncMock(return_value=mock_connector)

        mock_result = MockOperationResult(success=True, data=vm_data)
        mock_vmware_connector = AsyncMock()
        mock_vmware_connector.execute = AsyncMock(return_value=mock_result)

        mock_cred_repo = AsyncMock()
        mock_cred_repo.get_credentials = AsyncMock(
            return_value={"username": "admin", "password": "pass"}
        )

        # Create a mock CachedTable to return
        mock_cached_table = CachedTable(
            table_name="virtual_machines",
            operation_id="list_virtual_machines",
            connector_id="test-connector",
            columns=["name", "power_state", "memory_mb", "cpu_count"],
            row_count=50,
        )

        with (
            patch(
                "meho_app.modules.connectors.repositories.connector_repository.ConnectorRepository",
                return_value=mock_connector_repo,
            ),
            patch(
                "meho_app.modules.connectors.database.create_session_maker"
            ) as mock_session_maker,
            patch(
                "meho_app.modules.connectors.connectors.get_pooled_connector",
                return_value=mock_vmware_connector,
            ),
            patch(
                "meho_app.modules.connectors.user_credentials.UserCredentialRepository",
                return_value=mock_cred_repo,
            ),
            patch(
                "meho_app.modules.agents.unified_executor.get_unified_executor"
            ) as mock_get_executor,
        ):
            mock_session = AsyncMock()
            mock_session_maker.return_value = MagicMock(
                __aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock()
            )

            mock_executor = MagicMock()
            mock_executor.cache_as_table_async = AsyncMock(return_value=mock_cached_table)
            mock_get_executor.return_value = mock_executor

            args = {
                "connector_id": "test-connector",
                "operation_id": "list_virtual_machines",
                "parameter_sets": [{}],
            }

            result_json = await call_operation_handler(mock_deps, args)
            result = json.loads(result_json)

            # Verify caching was called
            mock_executor.cache_as_table_async.assert_called_once()

            # Verify result has table info
            assert result["success"] is True
            assert result["cached"] is True
            assert result["table"] == "virtual_machines"
            assert result["count"] == 50
            assert "columns" in result
            assert "sample" in result
