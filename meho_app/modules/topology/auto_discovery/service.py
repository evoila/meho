# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Topology auto-discovery service.

Main orchestrator for extracting topology entities from connector
operation results and queueing them for background storage.

This is the "Muscle" layer of the hybrid topology discovery system.

Supported connector types (via extraction schemas):
- kubernetes: Kubernetes (Pods, Nodes, Deployments, Services, etc.)
- vmware: VMware vSphere (VMs, Hosts, Clusters, Datastores)
- gcp: Google Cloud (Instances, Disks, Networks, Subnets, Firewalls, GKE Clusters)
- proxmox: Proxmox VE (VMs, Containers, Nodes, Storage)
- prometheus: Prometheus (ScrapeTargets)
- argocd: ArgoCD (Applications, AppProjects)
- github: GitHub (Repositories, Workflows)

Note: Connector types without extraction schemas (rest, soap, loki, tempo,
alertmanager, jira, confluence, email) will not have entities extracted.
Extraction schemas define HOW to extract entities from API responses.
The actual wiring of process_operation_result calls into each connector's
operation handlers is a separate concern in the connector modules.
"""

from typing import Any

from meho_app.core.otel import get_logger, span

from ..extraction import get_schema_extractor
from .queue import DiscoveryMessage, DiscoveryQueue, get_discovery_queue

logger = get_logger(__name__)


class TopologyAutoDiscoveryService:
    """
    Service for automatic topology discovery from connector operations.

    This service is called after connector operations complete to extract
    entities and relationships using schema-based extraction, then queue
    them for background storage.

    The "Muscle" layer handles:
    - Entity extraction from connector results (via extraction schemas)
    - Basic hierarchical relationships (runs_on, member_of, uses)
    - Queueing for async storage with embeddings

    The "Brain" layer (LLM tools) handles:
    - Complex relationship inference
    - SAME_AS cross-connector correlation
    - Context-aware reasoning

    Usage:
        service = TopologyAutoDiscoveryService(queue)

        # After connector operation completes
        count = await service.process_operation_result(
            connector_type="vmware",
            connector_id="abc123",
            connector_name="Production vCenter",
            operation_id="list_virtual_machines",
            result_data=[{"name": "web-01", ...}, ...],
            tenant_id="tenant-1",
        )
        print(f"Queued {count} entities for topology storage")
    """

    def __init__(
        self,
        queue: DiscoveryQueue,
        enabled: bool = True,
    ):
        """
        Initialize the auto-discovery service.

        Args:
            queue: Discovery queue for storing messages
            enabled: Whether auto-discovery is enabled
        """
        self.queue = queue
        self.enabled = enabled
        self._extractor = get_schema_extractor()

        # Statistics
        self._entities_queued = 0
        self._relationships_queued = 0
        self._operations_processed = 0

    @property
    def stats(self) -> dict:
        """Get service statistics."""
        return {
            "entities_queued": self._entities_queued,
            "relationships_queued": self._relationships_queued,
            "operations_processed": self._operations_processed,
            "enabled": self.enabled,
        }

    async def process_operation_result(
        self,
        connector_type: str,
        connector_id: str,
        connector_name: str | None,
        operation_id: str,
        result_data: Any,
        tenant_id: str,
    ) -> int:
        """
        Process a connector operation result for topology discovery.

        Extracts entities and relationships from the result data using
        the schema-based extractor, then queues them for background storage.

        Args:
            connector_type: Type of connector (vmware, kubernetes, etc.)
            connector_id: Unique connector ID
            connector_name: Human-readable connector name
            operation_id: Operation that was executed
            result_data: Result data from the operation
            tenant_id: Tenant ID for multi-tenancy

        Returns:
            Number of entities queued for storage
        """
        if not self.enabled:
            logger.debug("Auto-discovery disabled, skipping")
            return 0

        with span(
            "topology.auto_discovery",
            connector_type=connector_type,
            operation_id=operation_id,
            connector_id=connector_id,
        ):
            try:
                # Extract entities and relationships using schema-based extraction
                entities, relationships = self._extractor.extract(
                    connector_type=connector_type,
                    operation_id=operation_id,
                    result_data=result_data,
                    connector_id=connector_id,
                    connector_name=connector_name,
                )

                if not entities and not relationships:
                    logger.warning(
                        f"No entities extracted from {connector_type}/{operation_id}",
                        connector_type=connector_type,
                        operation_id=operation_id,
                    )
                    return 0

                # Log successful extraction
                entity_types = list({e.entity_type for e in entities})
                logger.info(
                    f"Extracted {len(entities)} entities, {len(relationships)} relationships",
                    entity_count=len(entities),
                    relationship_count=len(relationships),
                    entity_types=entity_types[:5],
                )

                # Create discovery message
                message = DiscoveryMessage(
                    entities=entities,
                    relationships=relationships,
                    tenant_id=tenant_id,
                    connector_type=connector_type,  # Required for StoreDiscoveryInput
                )

                # Queue for background processing
                success = await self.queue.push(message)

                if success:
                    self._entities_queued += len(entities)
                    self._relationships_queued += len(relationships)
                    self._operations_processed += 1

                    logger.info(
                        f"Auto-discovery queued: {len(entities)} entities, {len(relationships)} relationships from {connector_type}/{operation_id}",
                        entity_count=len(entities),
                        relationship_count=len(relationships),
                        connector_type=connector_type,
                        operation_id=operation_id,
                    )

                    # Trigger immediate processing (if processor is running)
                    from .processor import get_processor_instance

                    processor = get_processor_instance()
                    if processor:
                        processor.trigger()

                    return len(entities)
                else:
                    logger.error(
                        f"Failed to queue discovery message for {connector_type}/{operation_id}",
                        connector_type=connector_type,
                        operation_id=operation_id,
                    )
                    return 0

            except Exception as e:
                logger.error(
                    f"Error in auto-discovery for {connector_type}/{operation_id}: {e}",
                    connector_type=connector_type,
                    operation_id=operation_id,
                    error=str(e),
                )
                return 0

    def reset_stats(self) -> None:
        """Reset service statistics."""
        self._entities_queued = 0
        self._relationships_queued = 0
        self._operations_processed = 0


# =============================================================================
# Singleton / Factory
# =============================================================================

_service_instance: TopologyAutoDiscoveryService | None = None


async def get_auto_discovery_service(
    queue: DiscoveryQueue | None = None,
    enabled: bool = True,
) -> TopologyAutoDiscoveryService:
    """
    Get or create the auto-discovery service singleton.

    Args:
        queue: Optional discovery queue (uses default if not provided)
        enabled: Whether auto-discovery is enabled

    Returns:
        TopologyAutoDiscoveryService instance
    """
    global _service_instance

    if _service_instance is None:
        if queue is None:
            queue = await get_discovery_queue()

        _service_instance = TopologyAutoDiscoveryService(
            queue=queue,
            enabled=enabled,
        )
        logger.info("TopologyAutoDiscoveryService initialized")

    return _service_instance


def reset_auto_discovery_service() -> None:
    """Reset the service singleton (for testing)."""
    global _service_instance
    _service_instance = None
