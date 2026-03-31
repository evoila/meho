# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Performance tests for Topology Auto-Discovery.

TASK-143 Phase 4: Validates that auto-discovery can handle high-volume
scenarios without significant performance degradation.

Metrics tracked:
- Extraction time for large VM lists
- Queue throughput (push/pop operations)
- Batch processor performance under load
- Concurrent operation handling
- Memory usage during large batches

Run with:
    pytest tests/performance/test_auto_discovery_performance.py -v --tb=short

Note: These tests are designed to establish baselines, not fail on specific thresholds.
"""

import asyncio
import time
import tracemalloc
import uuid
from typing import Any
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
from meho_app.modules.topology.auto_discovery.base import ExtractedEntity, ExtractedRelationship
from meho_app.modules.topology.auto_discovery.queue import DiscoveryMessage
from meho_app.modules.topology.extraction import get_schema_extractor, reset_schema_extractor

pytestmark = [pytest.mark.asyncio]


# =============================================================================
# Test Data Generators
# =============================================================================


def generate_large_vm_list(count: int) -> list[dict[str, Any]]:
    """Generate a large list of VMware VMs for performance testing."""
    vms = []
    for i in range(count):
        vms.append(
            {
                "name": f"vm-{i:05d}",
                "config": {
                    "num_cpu": 4 + (i % 8),
                    "memory_mb": 4096 * (1 + (i % 4)),
                    "guest_os": f"CentOS 8 variant-{i % 10}",
                },
                "runtime": {
                    "host": f"esxi-host-{i % 20:02d}",
                    "power_state": "poweredOn" if i % 10 != 0 else "poweredOff",
                },
                "guest": {
                    "ip_address": f"192.168.{(i // 256) % 256}.{i % 256}",
                    "hostname": f"vm-{i:05d}.corp.local",
                },
                "datastores": [f"datastore-{i % 5}", f"datastore-backup-{i % 3}"],
            }
        )
    return vms


def generate_large_k8s_pod_list(count: int) -> dict[str, Any]:
    """Generate a large Kubernetes PodList for performance testing."""
    items = []
    for i in range(count):
        items.append(
            {
                "metadata": {
                    "name": f"pod-{i:05d}",
                    "namespace": f"ns-{i % 10}",
                    "ownerReferences": [{"kind": "ReplicaSet", "name": f"rs-{i % 50:03d}"}],
                },
                "spec": {
                    "nodeName": f"k8s-node-{i % 10:02d}",
                },
                "status": {
                    "phase": "Running" if i % 20 != 0 else "Pending",
                    "podIP": f"10.244.{(i // 256) % 256}.{i % 256}",
                },
            }
        )
    return {
        "apiVersion": "v1",
        "kind": "PodList",
        "items": items,
    }


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset singleton instances before each test."""
    reset_auto_discovery_service()
    reset_discovery_queue()
    reset_batch_processor()
    reset_schema_extractor()
    yield
    reset_auto_discovery_service()
    reset_discovery_queue()
    reset_batch_processor()
    reset_schema_extractor()


@pytest.fixture
def tenant_id() -> str:
    return f"perf-tenant-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def connector_id() -> str:
    return str(uuid.uuid4())


# =============================================================================
# VMware Extractor Performance
# =============================================================================


class TestSchemaExtractorPerformance:
    """Performance tests for schema-based entity extraction."""

    async def test_large_vm_list_extraction(self, connector_id: str):
        """
        Test: Extract entities from 1000 VMs using schema-based extractor.

        Baseline: Should complete in < 2 seconds.
        """
        reset_schema_extractor()
        vm_count = 1000
        vm_data = generate_large_vm_list(vm_count)

        extractor = get_schema_extractor()

        # Start timing
        start_time = time.perf_counter()

        entities, _relationships = extractor.extract(
            connector_type="vmware",
            operation_id="list_virtual_machines",
            result_data=vm_data,
            connector_id=connector_id,
            connector_name="Performance Test vCenter",
        )

        elapsed = time.perf_counter() - start_time

        # Assertions - VMs plus stub entities for hosts and datastores
        vm_entities = [e for e in entities if e.entity_type == "VM"]
        assert len(vm_entities) == vm_count

        # Performance baseline (log for reference)
        print(
            f"\n📊 Schema extraction: {vm_count} VMs in {elapsed:.3f}s "
            f"({vm_count / elapsed:.0f} VMs/sec)"
        )

        # Should be reasonably fast (adjust threshold if needed)
        assert elapsed < 5.0, f"Extraction took too long: {elapsed:.3f}s"

    async def test_extraction_memory_usage(self, connector_id: str):
        """
        Test: Memory usage during large extraction.

        Validates memory stays bounded during entity extraction.
        """
        reset_schema_extractor()
        vm_count = 5000
        vm_data = generate_large_vm_list(vm_count)

        extractor = get_schema_extractor()

        # Start memory tracking
        tracemalloc.start()

        entities, _relationships = extractor.extract(
            connector_type="vmware",
            operation_id="list_virtual_machines",
            result_data=vm_data,
            connector_id=connector_id,
            connector_name="Memory Test vCenter",
        )

        # Get memory usage
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Convert to MB
        current_mb = current / (1024 * 1024)
        peak_mb = peak / (1024 * 1024)

        print(
            f"\n📊 Memory usage: current={current_mb:.2f}MB, peak={peak_mb:.2f}MB "
            f"for {vm_count} VMs"
        )

        # Assertions
        vm_entities = [e for e in entities if e.entity_type == "VM"]
        assert len(vm_entities) == vm_count
        # Peak memory should be reasonable (< 200MB for 5000 VMs with JMESPath overhead)
        assert peak_mb < 200, f"Memory usage too high: {peak_mb:.2f}MB"


# =============================================================================
# Queue Performance
# =============================================================================


class TestQueuePerformance:
    """Performance tests for DiscoveryQueue operations."""

    async def test_queue_throughput(self, tenant_id: str, connector_id: str):
        """
        Test: Queue push/pop throughput.

        Baseline: Should handle 10,000 push/pop operations quickly.
        """
        queue = DiscoveryQueue()
        message_count = 10000

        # Generate messages
        messages = []
        for i in range(message_count):
            messages.append(
                DiscoveryMessage(
                    entities=[
                        ExtractedEntity(
                            name=f"entity-{i}",
                            description=f"Test entity {i}",
                            connector_id=connector_id,
                            connector_name="Queue Test",
                            raw_attributes={},
                        )
                    ],
                    relationships=[],
                    tenant_id=tenant_id,
                )
            )

        # Test push throughput
        start_push = time.perf_counter()
        for msg in messages:
            await queue.push(msg)
        push_elapsed = time.perf_counter() - start_push

        push_rate = message_count / push_elapsed
        print(
            f"\n📊 Queue push: {message_count} messages in {push_elapsed:.3f}s "
            f"({push_rate:.0f} msg/sec)"
        )

        # Test pop throughput
        start_pop = time.perf_counter()
        popped = await queue.pop_batch(message_count)
        pop_elapsed = time.perf_counter() - start_pop

        pop_rate = len(popped) / pop_elapsed if pop_elapsed > 0 else float("inf")
        print(
            f"📊 Queue pop: {len(popped)} messages in {pop_elapsed:.3f}s ({pop_rate:.0f} msg/sec)"
        )

        # Assertions
        assert len(popped) == message_count
        assert await queue.size() == 0

        # Performance baselines (in-memory queue should be very fast)
        assert push_elapsed < 5.0, f"Push took too long: {push_elapsed:.3f}s"

    async def test_queue_batch_pop_performance(self, tenant_id: str, connector_id: str):
        """
        Test: Batch pop performance with different batch sizes.
        """
        queue = DiscoveryQueue()
        total_messages = 1000

        # Push messages
        for i in range(total_messages):
            await queue.push(
                DiscoveryMessage(
                    entities=[
                        ExtractedEntity(
                            name=f"batch-entity-{i}",
                            description=f"Batch test entity {i}",
                            connector_id=connector_id,
                            connector_name="Batch Test",
                            raw_attributes={},
                        )
                    ],
                    relationships=[],
                    tenant_id=tenant_id,
                )
            )

        # Test different batch sizes
        batch_sizes = [10, 50, 100, 500]
        results = []

        for batch_size in batch_sizes:
            # Refill queue
            remaining = total_messages - await queue.size()
            for i in range(remaining):
                await queue.push(
                    DiscoveryMessage(
                        entities=[
                            ExtractedEntity(
                                name=f"refill-{i}",
                                description="Refill",
                                connector_id=connector_id,
                                connector_name="Test",
                                raw_attributes={},
                            )
                        ],
                        relationships=[],
                        tenant_id=tenant_id,
                    )
                )

            # Clear and refill
            await queue.clear()
            for i in range(total_messages):
                await queue.push(
                    DiscoveryMessage(
                        entities=[
                            ExtractedEntity(
                                name=f"batch-{batch_size}-{i}",
                                description="Test",
                                connector_id=connector_id,
                                connector_name="Test",
                                raw_attributes={},
                            )
                        ],
                        relationships=[],
                        tenant_id=tenant_id,
                    )
                )

            start = time.perf_counter()
            count = 0
            while await queue.size() > 0:
                batch = await queue.pop_batch(batch_size)
                count += len(batch)
            elapsed = time.perf_counter() - start

            results.append((batch_size, count, elapsed))

        print("\n📊 Batch pop performance:")
        for batch_size, count, elapsed in results:
            rate = count / elapsed if elapsed > 0 else float("inf")
            print(
                f"   Batch size {batch_size}: {count} messages in {elapsed:.3f}s "
                f"({rate:.0f} msg/sec)"
            )


# =============================================================================
# BatchProcessor Performance
# =============================================================================


class TestBatchProcessorPerformance:
    """Performance tests for BatchProcessor under load."""

    async def test_batch_processor_under_load(self, tenant_id: str, connector_id: str):
        """
        Test: Process 1000 messages through BatchProcessor.

        Uses mocked TopologyService to isolate processor performance.
        """
        queue = DiscoveryQueue()
        message_count = 1000

        # Queue messages
        for i in range(message_count):
            await queue.push(
                DiscoveryMessage(
                    entities=[
                        ExtractedEntity(
                            name=f"load-entity-{i}",
                            description=f"Load test entity {i} with description",
                            connector_id=connector_id,
                            connector_name="Load Test vCenter",
                            raw_attributes={"index": i},
                        )
                    ],
                    relationships=[
                        ExtractedRelationship(
                            from_entity_name=f"load-entity-{i}",
                            to_entity_name=f"host-{i % 10}",
                            relationship_type="runs_on",
                        )
                    ],
                    tenant_id=tenant_id,
                )
            )

        # Create mock session
        mock_session = AsyncMock()
        mock_session_maker = MagicMock(return_value=mock_session)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        # Mock TopologyService
        mock_result = MagicMock()
        mock_result.stored = True
        mock_result.entities_created = 1
        mock_result.relationships_created = 1
        mock_result.same_as_created = 0
        mock_result.message = "OK"

        with patch("meho_app.modules.topology.service.TopologyService") as MockTopologyService:
            mock_service_instance = AsyncMock()
            mock_service_instance.store_discovery = AsyncMock(return_value=mock_result)
            MockTopologyService.return_value = mock_service_instance

            processor = BatchProcessor(
                queue=queue,
                session_maker=mock_session_maker,
                batch_size=100,  # Process 100 at a time
                interval_seconds=0,  # No delay between batches
            )

            start_time = time.perf_counter()

            # Process all messages
            processed = 0
            while await queue.size() > 0:
                count = await processor._process_batch()
                processed += count

            elapsed = time.perf_counter() - start_time

        rate = processed / elapsed if elapsed > 0 else float("inf")
        print(f"\n📊 BatchProcessor: {processed} messages in {elapsed:.3f}s ({rate:.0f} msg/sec)")

        # Assertions
        assert processed == message_count
        assert await queue.size() == 0

        # Performance baseline
        assert elapsed < 10.0, f"Processing took too long: {elapsed:.3f}s"


# =============================================================================
# Concurrent Operations
# =============================================================================


class TestConcurrentOperations:
    """Tests for concurrent auto-discovery operations."""

    async def test_concurrent_connector_operations(self, tenant_id: str):
        """
        Test: 10 parallel connector operations.

        Validates no race conditions in extraction and queueing.
        """
        queue = DiscoveryQueue()
        service = TopologyAutoDiscoveryService(queue=queue)

        concurrent_ops = 10
        vms_per_op = 100

        async def process_connector(idx: int):
            """Simulate a connector operation."""
            connector_id = str(uuid.uuid4())
            vm_data = generate_large_vm_list(vms_per_op)

            # Modify VM names to be unique per connector
            for vm in vm_data:
                vm["name"] = f"conn-{idx}-{vm['name']}"

            return await service.process_operation_result(
                connector_type="vmware",
                connector_id=connector_id,
                connector_name=f"Concurrent Test vCenter {idx}",
                operation_id="list_virtual_machines",
                result_data=vm_data,
                tenant_id=tenant_id,
            )

        start_time = time.perf_counter()

        # Run all connector operations concurrently
        results = await asyncio.gather(*[process_connector(i) for i in range(concurrent_ops)])

        elapsed = time.perf_counter() - start_time

        total_entities = sum(results)
        print(
            f"\n📊 Concurrent ops: {concurrent_ops} operations, "
            f"{total_entities} entities in {elapsed:.3f}s"
        )

        # Assertions
        assert total_entities == concurrent_ops * vms_per_op
        assert await queue.size() == concurrent_ops  # 1 message per operation

        # Verify all messages are intact
        messages = await queue.pop_batch(concurrent_ops)
        total_queued_entities = sum(len(m.entities) for m in messages)
        assert total_queued_entities == concurrent_ops * vms_per_op


# =============================================================================
# End-to-End Performance
# =============================================================================


class TestEndToEndPerformance:
    """End-to-end performance tests for the complete auto-discovery flow."""

    async def test_full_flow_performance(self, tenant_id: str, connector_id: str):
        """
        Test: Complete flow from extraction to queue storage.

        Measures the entire auto-discovery pipeline performance.
        """
        queue = DiscoveryQueue()
        service = TopologyAutoDiscoveryService(queue=queue)

        vm_count = 500
        vm_data = generate_large_vm_list(vm_count)

        start_time = time.perf_counter()

        # Process through service
        count = await service.process_operation_result(
            connector_type="vmware",
            connector_id=connector_id,
            connector_name="Full Flow Test",
            operation_id="list_virtual_machines",
            result_data=vm_data,
            tenant_id=tenant_id,
        )

        # Pop from queue to verify
        messages = await queue.pop_batch(1)

        elapsed = time.perf_counter() - start_time

        rate = count / elapsed if elapsed > 0 else float("inf")
        print(
            f"\n📊 Full flow: {count} entities processed in {elapsed:.3f}s "
            f"({rate:.0f} entities/sec)"
        )

        # Assertions
        assert count == vm_count
        assert len(messages) == 1
        assert len(messages[0].entities) == vm_count

        # Service stats
        stats = service.stats
        assert stats["entities_queued"] == vm_count
        assert stats["operations_processed"] == 1

    async def test_kubernetes_large_pod_list(self, tenant_id: str, connector_id: str):
        """
        Test: Kubernetes extractor with large pod list.
        """
        queue = DiscoveryQueue()
        service = TopologyAutoDiscoveryService(queue=queue)

        pod_count = 500
        pod_data = generate_large_k8s_pod_list(pod_count)

        start_time = time.perf_counter()

        count = await service.process_operation_result(
            connector_type="rest",  # K8s via REST connector
            connector_id=connector_id,
            connector_name="K8s Performance Test",
            operation_id="list_pods",
            result_data=pod_data,
            tenant_id=tenant_id,
        )

        elapsed = time.perf_counter() - start_time

        rate = count / elapsed if elapsed > 0 else float("inf")
        print(f"\n📊 K8s flow: {count} pods processed in {elapsed:.3f}s ({rate:.0f} pods/sec)")

        # Assertions
        assert count == pod_count

        messages = await queue.pop_batch(1)
        msg = messages[0]

        # Each pod has runs_on and member_of relationships
        assert len(msg.relationships) == pod_count * 2
