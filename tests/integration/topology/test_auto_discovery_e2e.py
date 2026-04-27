# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
End-to-end tests for topology auto-discovery integration.

Tests the complete flow:
1. Connector operation executes
2. Auto-discovery extracts entities
3. Entities are queued
4. BatchProcessor stores entities in topology database

TASK-143: Phase 2 Integration Tests
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.topology.auto_discovery import (
    BatchProcessor,
    DiscoveryQueue,
    TopologyAutoDiscoveryService,
    reset_auto_discovery_service,
    reset_batch_processor,
    reset_discovery_queue,
)
from meho_app.modules.topology.auto_discovery.base import (
    ExtractedEntity,
    ExtractedRelationship,
)
from meho_app.modules.topology.auto_discovery.queue import DiscoveryMessage


class TestOperationHandlerIntegration:
    """Test auto-discovery integration with call_operation_handler."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Reset singletons before each test."""
        reset_auto_discovery_service()
        reset_discovery_queue()
        reset_batch_processor()

    @pytest.mark.asyncio
    async def test_trigger_auto_discovery_vmware_vms(self):
        """Test that _trigger_auto_discovery extracts VMware VMs correctly."""
        from meho_app.modules.agents.shared.handlers.operation_handlers import (
            _trigger_auto_discovery,
        )

        # Create a queue and service
        queue = DiscoveryQueue()
        service = TopologyAutoDiscoveryService(queue=queue)

        # Mock get_auto_discovery_service to return our service
        # Patch at source since it's imported inside the function
        with (
            patch(
                "meho_app.modules.topology.auto_discovery.service.get_auto_discovery_service",
                return_value=service,
            ),
            patch(
                "meho_app.modules.topology.auto_discovery.get_auto_discovery_service",
                return_value=service,
            ),
            patch("meho_app.core.config.get_config") as mock_config,
        ):
            # Configure mock
            mock_config.return_value.topology_auto_discovery_enabled = True

            # Simulate VMware operation result
            result_data = [
                {
                    "name": "web-01",
                    "config": {"num_cpu": 4, "memory_mb": 8192, "guest_os": "CentOS 7"},
                    "runtime": {"host": "esxi-01", "power_state": "poweredOn"},
                    "guest": {"ip_address": "192.168.1.10"},
                    "datastores": ["datastore1"],
                },
                {
                    "name": "db-01",
                    "config": {"num_cpu": 8, "memory_mb": 32768, "guest_os": "RHEL 8"},
                    "runtime": {"host": "esxi-02"},
                    "datastores": ["datastore2", "datastore3"],
                },
            ]

            # Call the auto-discovery trigger
            await _trigger_auto_discovery(
                connector_type="vmware",
                connector_id="vcenter-001",
                connector_name="Production vCenter",
                operation_id="list_virtual_machines",
                result_data=result_data,
                tenant_id="acme-corp",
            )

            # Verify entities were queued
            assert await queue.size() == 1

            # Pop and verify message content
            messages = await queue.pop_batch(1)
            assert len(messages) == 1

            msg = messages[0]
            assert msg.tenant_id == "acme-corp"
            assert len(msg.entities) == 2

            # Check entity details
            entity_names = [e.name for e in msg.entities]
            assert "web-01" in entity_names
            assert "db-01" in entity_names

            # Check relationships
            runs_on = [r for r in msg.relationships if r.relationship_type == "runs_on"]
            assert len(runs_on) == 2

            uses = [r for r in msg.relationships if r.relationship_type == "uses"]
            assert len(uses) == 3  # 1 + 2 datastores

    @pytest.mark.asyncio
    async def test_trigger_auto_discovery_disabled(self):
        """Test that auto-discovery respects disabled config."""
        from meho_app.modules.agents.shared.handlers.operation_handlers import (
            _trigger_auto_discovery,
        )

        queue = DiscoveryQueue()

        with patch("meho_app.core.config.get_config") as mock_config:
            # Disable auto-discovery
            mock_config.return_value.topology_auto_discovery_enabled = False

            await _trigger_auto_discovery(
                connector_type="vmware",
                connector_id="vcenter-001",
                connector_name="vCenter",
                operation_id="list_virtual_machines",
                result_data=[{"name": "vm1"}],
                tenant_id="tenant-1",
            )

            # Queue should be empty since disabled
            assert await queue.size() == 0

    @pytest.mark.asyncio
    async def test_trigger_auto_discovery_error_non_blocking(self):
        """Test that auto-discovery errors don't block operations."""
        from meho_app.modules.agents.shared.handlers.operation_handlers import (
            _trigger_auto_discovery,
        )

        with patch("meho_app.core.config.get_config") as mock_config:
            mock_config.return_value.topology_auto_discovery_enabled = True

            with patch(
                "meho_app.modules.topology.auto_discovery.get_auto_discovery_service",
                side_effect=Exception("Service unavailable"),
            ):
                # This should NOT raise - errors are caught and logged
                await _trigger_auto_discovery(
                    connector_type="vmware",
                    connector_id="vcenter-001",
                    connector_name="vCenter",
                    operation_id="list_virtual_machines",
                    result_data=[{"name": "vm1"}],
                    tenant_id="tenant-1",
                )
                # If we reach here without exception, test passes

    @pytest.mark.asyncio
    async def test_trigger_auto_discovery_unsupported_connector(self):
        """Test auto-discovery with unsupported connector type."""
        from meho_app.modules.agents.shared.handlers.operation_handlers import (
            _trigger_auto_discovery,
        )

        queue = DiscoveryQueue()
        service = TopologyAutoDiscoveryService(queue=queue)

        with (
            patch(
                "meho_app.modules.topology.auto_discovery.get_auto_discovery_service",
                return_value=service,
            ),
            patch("meho_app.core.config.get_config") as mock_config,
        ):
            mock_config.return_value.topology_auto_discovery_enabled = True

            await _trigger_auto_discovery(
                connector_type="unknown_type",
                connector_id="conn-001",
                connector_name="Unknown Connector",
                operation_id="list_items",
                result_data=[{"name": "item1"}],
                tenant_id="tenant-1",
            )

            # Should not queue anything for unsupported type
            assert await queue.size() == 0


class TestBatchProcessorE2E:
    """End-to-end tests for BatchProcessor storing entities in topology."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Reset singletons before each test."""
        reset_auto_discovery_service()
        reset_discovery_queue()
        reset_batch_processor()

    @pytest.mark.asyncio
    async def test_processor_stores_entities_in_topology(self):
        """Test that BatchProcessor stores entities via TopologyService."""
        # Create queue with test message
        queue = DiscoveryQueue()

        # Use proper UUID for connector_id (required by TopologyEntityCreate)
        connector_id = str(uuid.uuid4())

        message = DiscoveryMessage(
            entities=[
                ExtractedEntity(
                    name="web-01",
                    description="VMware VM web-01 (4 vCPU, 8192MB RAM)",
                    connector_id=connector_id,
                    connector_name="Production vCenter",
                    raw_attributes={"power_state": "poweredOn"},
                ),
            ],
            relationships=[
                ExtractedRelationship(
                    from_entity_name="web-01",
                    to_entity_name="esxi-01",
                    relationship_type="runs_on",
                ),
            ],
            tenant_id="test-tenant",
        )

        await queue.push(message)
        assert await queue.size() == 1

        # Create mock session and TopologyService
        mock_session = AsyncMock()
        mock_session_maker = MagicMock(return_value=mock_session)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        # Mock TopologyService
        mock_result = MagicMock()
        mock_result.stored = True
        mock_result.entities_created = 1
        mock_result.relationships_created = 1
        mock_result.message = "Stored successfully"

        # Patch TopologyService at its source location
        with patch("meho_app.modules.topology.service.TopologyService") as MockTopologyService:
            mock_service_instance = AsyncMock()
            mock_service_instance.store_discovery = AsyncMock(return_value=mock_result)
            MockTopologyService.return_value = mock_service_instance

            # Create and process
            processor = BatchProcessor(
                queue=queue,
                session_maker=mock_session_maker,
                batch_size=10,
                interval_seconds=1,
            )

            # Process one message
            processed = await processor.process_one()

            assert processed is True
            assert await queue.size() == 0

            # Verify TopologyService was called
            mock_service_instance.store_discovery.assert_called_once()
            call_args = mock_service_instance.store_discovery.call_args

            # Check the input data
            input_data = call_args[0][0]
            assert len(input_data.entities) == 1
            assert input_data.entities[0].name == "web-01"

            assert len(input_data.relationships) == 1
            assert input_data.relationships[0].relationship_type == "runs_on"

            # Check tenant_id
            tenant_id = call_args[0][1]
            assert tenant_id == "test-tenant"

            # Check stats
            assert processor.stats["messages_processed"] == 1
            assert processor.stats["entities_processed"] == 1
            assert processor.stats["relationships_processed"] == 1

    @pytest.mark.asyncio
    async def test_processor_handles_multiple_messages(self):
        """Test BatchProcessor processes multiple messages in batch."""
        queue = DiscoveryQueue()

        # Use proper UUID for connector_id (required by TopologyEntityCreate)
        connector_id = str(uuid.uuid4())

        # Add multiple messages
        for i in range(5):
            message = DiscoveryMessage(
                entities=[
                    ExtractedEntity(
                        name=f"vm-{i}",
                        description=f"VM {i}",
                        connector_id=connector_id,
                        connector_name="vCenter",
                        raw_attributes={},
                    ),
                ],
                relationships=[],
                tenant_id="tenant-1",
            )
            await queue.push(message)

        assert await queue.size() == 5

        # Create mock session
        mock_session = AsyncMock()
        mock_session_maker = MagicMock(return_value=mock_session)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_result = MagicMock()
        mock_result.stored = True
        mock_result.entities_created = 1
        mock_result.relationships_created = 0

        with patch("meho_app.modules.topology.service.TopologyService") as MockTopologyService:
            mock_service_instance = AsyncMock()
            mock_service_instance.store_discovery = AsyncMock(return_value=mock_result)
            MockTopologyService.return_value = mock_service_instance

            processor = BatchProcessor(
                queue=queue,
                session_maker=mock_session_maker,
                batch_size=10,
                interval_seconds=1,
            )

            # Process batch
            count = await processor._process_batch()

            assert count == 5
            assert await queue.size() == 0
            assert processor.stats["messages_processed"] == 5
            assert processor.stats["entities_processed"] == 5


class TestFullE2EFlow:
    """
    Full end-to-end flow tests simulating real usage.

    Tests the complete cycle:
    1. Service extracts entities from operation result
    2. Entities are queued
    3. Processor stores entities in topology
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Reset singletons."""
        reset_auto_discovery_service()
        reset_discovery_queue()
        reset_batch_processor()

    @pytest.mark.asyncio
    async def test_vmware_to_topology_full_flow(self):
        """Test complete VMware -> Queue -> Topology flow."""
        # Step 1: Create components
        queue = DiscoveryQueue()
        service = TopologyAutoDiscoveryService(queue=queue)

        # Use proper UUID for connector_id (required by TopologyEntityCreate)
        connector_id = str(uuid.uuid4())

        # Step 2: Simulate VMware operation result (from call_operation_handler)
        vmware_result = [
            {
                "name": "production-web-01",
                "config": {
                    "num_cpu": 8,
                    "memory_mb": 16384,
                    "guest_os": "Ubuntu 22.04 (64-bit)",
                },
                "guest": {
                    "ip_address": "10.1.1.100",
                    "hostname": "production-web-01.corp.example.com",
                },
                "runtime": {
                    "host": "esxi-blade-01.dc1.example.com",
                    "power_state": "poweredOn",
                },
                "datastores": ["SAN-LUN-01", "NFS-BACKUP-01"],
            },
        ]

        # Step 3: Process through auto-discovery service
        count = await service.process_operation_result(
            connector_type="vmware",
            connector_id=connector_id,
            connector_name="DC1 vCenter",
            operation_id="list_virtual_machines",
            result_data=vmware_result,
            tenant_id="enterprise-corp",
        )

        assert count == 1
        assert await queue.size() == 1

        # Step 4: Create processor and store
        mock_session = AsyncMock()
        mock_session_maker = MagicMock(return_value=mock_session)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_result = MagicMock()
        mock_result.stored = True
        mock_result.entities_created = 1
        mock_result.relationships_created = 3
        mock_result.message = "Success"

        with patch("meho_app.modules.topology.service.TopologyService") as MockTopologyService:
            mock_service_instance = AsyncMock()
            mock_service_instance.store_discovery = AsyncMock(return_value=mock_result)
            MockTopologyService.return_value = mock_service_instance

            processor = BatchProcessor(
                queue=queue,
                session_maker=mock_session_maker,
                batch_size=100,
                interval_seconds=5,
            )

            # Process
            processed = await processor.process_one()

            assert processed is True
            assert await queue.size() == 0

            # Verify what was stored
            call_args = mock_service_instance.store_discovery.call_args
            input_data = call_args[0][0]
            tenant_id = call_args[0][1]

            assert tenant_id == "enterprise-corp"
            assert len(input_data.entities) == 1

            # Check entity details
            entity = input_data.entities[0]
            assert entity.name == "production-web-01"
            assert str(entity.connector_id) == connector_id
            assert entity.connector_name == "DC1 vCenter"
            assert "8 vCPU" in entity.description
            assert "16384MB RAM" in entity.description
            assert "Ubuntu 22.04" in entity.description

            # Check relationships
            assert len(input_data.relationships) == 3  # 1 runs_on + 2 uses

            rel_types = {r.relationship_type for r in input_data.relationships}
            assert rel_types == {"runs_on", "uses"}

            runs_on = [r for r in input_data.relationships if r.relationship_type == "runs_on"]
            assert runs_on[0].to_entity_name == "esxi-blade-01.dc1.example.com"

    @pytest.mark.asyncio
    async def test_multi_connector_discovery(self):
        """Test discovery from multiple connector operations."""
        queue = DiscoveryQueue()
        service = TopologyAutoDiscoveryService(queue=queue)

        # VMware VMs
        await service.process_operation_result(
            connector_type="vmware",
            connector_id="vcenter-1",
            connector_name="vCenter 1",
            operation_id="list_virtual_machines",
            result_data=[{"name": "vm-1", "runtime": {"host": "host-1"}}],
            tenant_id="tenant-1",
        )

        # VMware Hosts
        await service.process_operation_result(
            connector_type="vmware",
            connector_id="vcenter-1",
            connector_name="vCenter 1",
            operation_id="list_hosts",
            result_data=[{"name": "host-1", "cluster": "cluster-1"}],
            tenant_id="tenant-1",
        )

        # VMware Clusters
        await service.process_operation_result(
            connector_type="vmware",
            connector_id="vcenter-1",
            connector_name="vCenter 1",
            operation_id="list_clusters",
            result_data=[{"name": "cluster-1", "drs_enabled": True}],
            tenant_id="tenant-1",
        )

        # All should be queued
        assert await queue.size() == 3

        # Verify service stats
        assert service.stats["operations_processed"] == 3
        assert service.stats["entities_queued"] == 3
