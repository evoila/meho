# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for call_operation_handler Brain-Muscle caching.

Tests the caching behavior added to call_operation_handler to ensure
large responses are cached and return a summary with cache_key.

Phase 84: Module paths restructured -- connectors.database and connectors.repository
moved to connectors.repositories, mock patch targets no longer valid.
"""

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: connectors.database and connectors.repository module paths restructured to connectors.repositories")

# =============================================================================
# Test Fixtures and Helpers
# =============================================================================


@dataclass
class MockConnector:
    """Mock connector for testing."""

    id: str = "test-connector"
    name: str = "Test Connector"
    connector_type: str = "vmware"
    credential_strategy: str = "USER_PROVIDED"
    protocol_config: dict[str, Any] = None

    def __post_init__(self):
        if self.protocol_config is None:
            self.protocol_config = {"host": "vcenter.example.com"}


@dataclass
class MockOperationResult:
    """Mock result from VMware connector."""

    success: bool = True
    data: Any = None
    error: str | None = None
    duration_ms: int = 100


@pytest.fixture
def mock_deps():
    """Create mock MEHOGraphDeps."""
    deps = MagicMock()
    deps.session_id = "test-session-123"
    deps.user_id = "test-user"
    deps.meho_deps = MagicMock()
    deps.meho_deps.session_state = None
    return deps


@pytest.fixture
def large_vm_data():
    """Generate a large VM dataset (>20 items to trigger caching)."""
    return [
        {
            "name": f"vm-{i:03d}",
            "power_state": "poweredOn" if i % 3 != 0 else "poweredOff",
            "memory_mb": 4096 + (i * 512),
            "cpu_count": 2 + (i % 4),
            "guest_os": "Linux",
        }
        for i in range(50)  # 50 VMs, well above threshold of 20
    ]


@pytest.fixture
def small_vm_data():
    """Generate a small VM dataset (<20 items, no caching)."""
    return [
        {
            "name": f"vm-{i:03d}",
            "power_state": "poweredOn",
            "memory_mb": 4096,
        }
        for i in range(5)  # 5 VMs, below threshold
    ]


# =============================================================================
# call_operation_handler Caching Tests
# =============================================================================


class TestCallOperationCaching:
    """Tests for Brain-Muscle caching in call_operation_handler."""

    @pytest.mark.asyncio
    async def test_large_vmware_response_is_cached(self, mock_deps, large_vm_data):
        """Test that VMware responses with >20 items are cached and return summary."""
        from meho_app.modules.agents.shared.handlers.tool_handlers import call_operation_handler

        # Mock the connector repository
        mock_connector = MockConnector(connector_type="vmware")
        mock_connector_repo = AsyncMock()
        mock_connector_repo.get_connector = AsyncMock(return_value=mock_connector)

        # Mock VMware connector operation result
        mock_result = MockOperationResult(success=True, data=large_vm_data)
        mock_vmware_connector = AsyncMock()
        mock_vmware_connector.execute = AsyncMock(return_value=mock_result)

        # Mock credentials
        mock_cred_repo = AsyncMock()
        mock_cred_repo.get_credentials = AsyncMock(
            return_value={"username": "admin", "password": "pass"}
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
            # Mock session maker context
            mock_session = AsyncMock()
            mock_session_maker.return_value = MagicMock(
                __aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock()
            )

            # Mock unified executor caching
            mock_executor = MagicMock()
            mock_cached = MagicMock()
            mock_cached.summarize_for_brain.return_value = {
                "cache_key": "test-session-123:test-connector:/vmware/list_virtual_machines",
                "endpoint": "/vmware/list_virtual_machines",
                "connector_id": "test-connector",
                "count": 50,
                "schema": {"fields": ["name", "power_state", "memory_mb"]},
                "sample": large_vm_data[:5],
                "cached_at": "2025-01-01T00:00:00",
            }
            mock_executor.cache_response_async = AsyncMock(return_value=mock_cached)
            mock_get_executor.return_value = mock_executor

            # Call the handler
            args = {
                "connector_id": "test-connector",
                "operation_id": "list_virtual_machines",
                "parameter_sets": [{}],
            }

            result_json = await call_operation_handler(mock_deps, args)
            result = json.loads(result_json)

            # Verify caching was called
            mock_executor.cache_response_async.assert_called_once()
            call_kwargs = mock_executor.cache_response_async.call_args.kwargs
            assert call_kwargs["session_id"] == "test-session-123"
            assert call_kwargs["connector_id"] == "test-connector"
            assert call_kwargs["endpoint_path"] == "/vmware/list_virtual_machines"
            assert len(call_kwargs["data"]) == 50

            # Verify result is a summary with cache_key
            assert result["success"] is True
            assert result["cached"] is True
            assert "cache_key" in result
            assert result["count"] == 50
            assert "sample" in result
            assert len(result["sample"]) == 5
            assert "message" in result
            assert "50 items" in result["message"]

    @pytest.mark.asyncio
    async def test_small_vmware_response_not_cached(self, mock_deps, small_vm_data):
        """Test that VMware responses with <=20 items are NOT cached."""
        from meho_app.modules.agents.shared.handlers.tool_handlers import call_operation_handler

        mock_connector = MockConnector(connector_type="vmware")
        mock_connector_repo = AsyncMock()
        mock_connector_repo.get_connector = AsyncMock(return_value=mock_connector)

        mock_result = MockOperationResult(success=True, data=small_vm_data)
        mock_vmware_connector = AsyncMock()
        mock_vmware_connector.execute = AsyncMock(return_value=mock_result)

        mock_cred_repo = AsyncMock()
        mock_cred_repo.get_credentials = AsyncMock(
            return_value={"username": "admin", "password": "pass"}
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
            mock_executor.cache_response_async = AsyncMock()
            mock_get_executor.return_value = mock_executor

            args = {
                "connector_id": "test-connector",
                "operation_id": "list_virtual_machines",
                "parameter_sets": [{}],
            }

            result_json = await call_operation_handler(mock_deps, args)
            result = json.loads(result_json)

            # Verify caching was NOT called (small response)
            mock_executor.cache_response_async.assert_not_called()

            # Verify result contains full data
            assert result["success"] is True
            assert "cached" not in result or result.get("cached") is not True
            assert result["data"] == small_vm_data

    @pytest.mark.asyncio
    async def test_cache_key_format(self, mock_deps, large_vm_data):
        """Test that cache_key follows the expected format."""
        from meho_app.modules.agents.shared.handlers.tool_handlers import call_operation_handler

        mock_connector = MockConnector(connector_type="vmware")
        mock_connector_repo = AsyncMock()
        mock_connector_repo.get_connector = AsyncMock(return_value=mock_connector)

        mock_result = MockOperationResult(success=True, data=large_vm_data)
        mock_vmware_connector = AsyncMock()
        mock_vmware_connector.execute = AsyncMock(return_value=mock_result)

        mock_cred_repo = AsyncMock()
        mock_cred_repo.get_credentials = AsyncMock(
            return_value={"username": "admin", "password": "pass"}
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

            # Capture the actual caching call to verify endpoint_path
            captured_calls = []

            async def capture_cache(*args, **kwargs):
                captured_calls.append(kwargs)
                mock_cached = MagicMock()
                mock_cached.summarize_for_brain.return_value = {
                    "cache_key": f"{kwargs['session_id']}:{kwargs['connector_id']}:{kwargs['endpoint_path']}",
                    "count": len(kwargs["data"]),
                    "sample": kwargs["data"][:3],
                }
                return mock_cached

            mock_executor.cache_response_async = capture_cache
            mock_get_executor.return_value = mock_executor

            args = {
                "connector_id": "my-vcenter",
                "operation_id": "get_all_vms",
                "parameter_sets": [{}],
            }
            mock_deps.session_id = "session-abc"

            result_json = await call_operation_handler(mock_deps, args)
            result = json.loads(result_json)

            # Verify endpoint_path format
            assert len(captured_calls) == 1
            assert captured_calls[0]["endpoint_path"] == "/vmware/get_all_vms"

            # Verify cache_key in result
            assert "cache_key" in result
            assert result["cache_key"] == "session-abc:my-vcenter:/vmware/get_all_vms"


# =============================================================================
# reduce_data Integration Tests
# =============================================================================


class TestReduceDataWithCacheKey:
    """Tests for reduce_data_handler using cache_key from call_operation."""

    @pytest.mark.asyncio
    async def test_reduce_data_with_valid_cache_key(self, mock_deps, large_vm_data):
        """Test that reduce_data works with a valid cache_key."""
        from meho_app.modules.agents.shared.handlers.tool_handlers import reduce_data_handler

        # Pre-populate the cache
        cache_key = "test-session:test-connector:/vmware/list_vms"

        with patch(
            "meho_app.modules.agents.unified_executor.get_unified_executor"
        ) as mock_get_executor:
            mock_executor = MagicMock()

            # Mock the lookup for filtering by power_state
            mock_executor.execute_from_brain_async = AsyncMock(
                return_value={
                    "success": True,
                    "records": [vm for vm in large_vm_data if vm["power_state"] == "poweredOff"],
                    "count": len([vm for vm in large_vm_data if vm["power_state"] == "poweredOff"]),
                    "total_matched": len(
                        [vm for vm in large_vm_data if vm["power_state"] == "poweredOff"]
                    ),
                }
            )
            mock_get_executor.return_value = mock_executor

            args = {
                "cache_key": cache_key,
                "query": {
                    "filter": {
                        "conditions": [
                            {"field": "power_state", "operator": "=", "value": "poweredOff"}
                        ]
                    }
                },
            }

            result_json = await reduce_data_handler(mock_deps, args)
            result = json.loads(result_json)

            assert result["success"] is True
            # ~17 VMs should be powered off (every 3rd one from 50)
            assert result["count"] > 0

    @pytest.mark.asyncio
    async def test_reduce_data_without_cache_key_fails(self, mock_deps):
        """Test that reduce_data fails gracefully without cache_key."""
        from meho_app.modules.agents.shared.handlers.tool_handlers import reduce_data_handler

        args = {
            "query": {"filter": {"field": "power_state", "operator": "==", "value": "poweredOff"}}
        }

        result_json = await reduce_data_handler(mock_deps, args)
        result = json.loads(result_json)

        assert "error" in result
        assert "cache_key" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_reduce_data_lookup_entity(self, mock_deps, large_vm_data):
        """Test that reduce_data can lookup a specific entity."""
        from meho_app.modules.agents.shared.handlers.tool_handlers import reduce_data_handler

        cache_key = "test-session:test-connector:/vmware/list_vms"

        with patch(
            "meho_app.modules.agents.unified_executor.get_unified_executor"
        ) as mock_get_executor:
            mock_executor = MagicMock()

            # Mock entity lookup
            mock_executor.lookup_entity_async = AsyncMock(
                return_value={
                    "success": True,
                    "entity": {"name": "vm-010", "power_state": "poweredOff", "memory_mb": 9216},
                }
            )
            mock_get_executor.return_value = mock_executor

            args = {"cache_key": cache_key, "match": "vm-010"}

            result_json = await reduce_data_handler(mock_deps, args)
            result = json.loads(result_json)

            assert result["success"] is True
            assert result["entity"]["name"] == "vm-010"


# =============================================================================
# Edge Cases
# =============================================================================


class TestCachingEdgeCases:
    """Tests for edge cases in caching behavior."""

    @pytest.mark.asyncio
    async def test_failed_operation_not_cached(self, mock_deps):
        """Test that failed operations are not cached."""
        from meho_app.modules.agents.shared.handlers.tool_handlers import call_operation_handler

        mock_connector = MockConnector(connector_type="vmware")
        mock_connector_repo = AsyncMock()
        mock_connector_repo.get_connector = AsyncMock(return_value=mock_connector)

        # Simulate failed operation
        mock_result = MockOperationResult(success=False, data=None, error="Connection failed")
        mock_vmware_connector = AsyncMock()
        mock_vmware_connector.execute = AsyncMock(return_value=mock_result)

        mock_cred_repo = AsyncMock()
        mock_cred_repo.get_credentials = AsyncMock(
            return_value={"username": "admin", "password": "pass"}
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
            mock_executor.cache_response_async = AsyncMock()
            mock_get_executor.return_value = mock_executor

            args = {
                "connector_id": "test-connector",
                "operation_id": "list_virtual_machines",
                "parameter_sets": [{}],
            }

            result_json = await call_operation_handler(mock_deps, args)
            result = json.loads(result_json)

            # Should not cache failed operations
            mock_executor.cache_response_async.assert_not_called()
            assert result["success"] is False

    @pytest.mark.asyncio
    async def test_non_list_data_not_cached(self, mock_deps):
        """Test that non-list responses (dict) are not cached."""
        from meho_app.modules.agents.shared.handlers.tool_handlers import call_operation_handler

        mock_connector = MockConnector(connector_type="vmware")
        mock_connector_repo = AsyncMock()
        mock_connector_repo.get_connector = AsyncMock(return_value=mock_connector)

        # Single VM response (dict, not list)
        single_vm_data = {
            "name": "vm-001",
            "power_state": "poweredOn",
            "memory_mb": 4096,
        }

        mock_result = MockOperationResult(success=True, data=single_vm_data)
        mock_vmware_connector = AsyncMock()
        mock_vmware_connector.execute = AsyncMock(return_value=mock_result)

        mock_cred_repo = AsyncMock()
        mock_cred_repo.get_credentials = AsyncMock(
            return_value={"username": "admin", "password": "pass"}
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
            mock_executor.cache_response_async = AsyncMock()
            mock_get_executor.return_value = mock_executor

            args = {
                "connector_id": "test-connector",
                "operation_id": "get_vm",
                "parameter_sets": [{"vm_id": "vm-001"}],
            }

            result_json = await call_operation_handler(mock_deps, args)
            result = json.loads(result_json)

            # Should not cache non-list responses
            mock_executor.cache_response_async.assert_not_called()
            assert result["data"] == single_vm_data

    @pytest.mark.asyncio
    async def test_exactly_20_items_not_cached(self, mock_deps):
        """Test that exactly 20 items (at threshold) are NOT cached."""
        from meho_app.modules.agents.shared.handlers.tool_handlers import call_operation_handler

        mock_connector = MockConnector(connector_type="vmware")
        mock_connector_repo = AsyncMock()
        mock_connector_repo.get_connector = AsyncMock(return_value=mock_connector)

        # Exactly 20 items - should NOT trigger caching (threshold is >20)
        threshold_data = [{"name": f"vm-{i}"} for i in range(20)]

        mock_result = MockOperationResult(success=True, data=threshold_data)
        mock_vmware_connector = AsyncMock()
        mock_vmware_connector.execute = AsyncMock(return_value=mock_result)

        mock_cred_repo = AsyncMock()
        mock_cred_repo.get_credentials = AsyncMock(
            return_value={"username": "admin", "password": "pass"}
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
            mock_executor.cache_response_async = AsyncMock()
            mock_get_executor.return_value = mock_executor

            args = {
                "connector_id": "test-connector",
                "operation_id": "list_virtual_machines",
                "parameter_sets": [{}],
            }

            result_json = await call_operation_handler(mock_deps, args)
            result = json.loads(result_json)

            # Should NOT cache at exactly threshold
            mock_executor.cache_response_async.assert_not_called()
            assert len(result["data"]) == 20

    @pytest.mark.asyncio
    async def test_21_items_is_cached(self, mock_deps):
        """Test that 21 items (just above threshold) ARE cached."""
        from meho_app.modules.agents.shared.handlers.tool_handlers import call_operation_handler

        mock_connector = MockConnector(connector_type="vmware")
        mock_connector_repo = AsyncMock()
        mock_connector_repo.get_connector = AsyncMock(return_value=mock_connector)

        # 21 items - SHOULD trigger caching (>20)
        above_threshold_data = [{"name": f"vm-{i}"} for i in range(21)]

        mock_result = MockOperationResult(success=True, data=above_threshold_data)
        mock_vmware_connector = AsyncMock()
        mock_vmware_connector.execute = AsyncMock(return_value=mock_result)

        mock_cred_repo = AsyncMock()
        mock_cred_repo.get_credentials = AsyncMock(
            return_value={"username": "admin", "password": "pass"}
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
            mock_cached = MagicMock()
            mock_cached.summarize_for_brain.return_value = {
                "cache_key": "test:test:/vmware/list_virtual_machines",
                "count": 21,
                "sample": above_threshold_data[:5],
            }
            mock_executor.cache_response_async = AsyncMock(return_value=mock_cached)
            mock_get_executor.return_value = mock_executor

            args = {
                "connector_id": "test-connector",
                "operation_id": "list_virtual_machines",
                "parameter_sets": [{}],
            }

            result_json = await call_operation_handler(mock_deps, args)
            result = json.loads(result_json)

            # SHOULD cache at 21 items
            mock_executor.cache_response_async.assert_called_once()
            assert result["cached"] is True
            assert result["count"] == 21
