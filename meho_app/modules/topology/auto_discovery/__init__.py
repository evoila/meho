# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Topology Auto-Discovery Module

Provides automatic extraction of topology entities from connector
operation results. Part of the "Muscle" layer in the hybrid
topology discovery system.

The auto-discovery system:
1. Intercepts connector operation results
2. Extracts entities using schema-based extraction (Pods, VMs, Hosts, etc.)
3. Detects basic relationships (runs_on, member_of, uses)
4. Queues discoveries for background storage with embeddings

Supported Connectors (via extraction schemas):
- Kubernetes: Pods, Nodes, Namespaces, Deployments, ReplicaSets,
  StatefulSets, DaemonSets, Services, Ingresses
- VMware: VMs, Hosts, Clusters, Datastores

Note: Connector types without extraction schemas will not have
entities extracted. Add schemas to meho_app.modules.topology.extraction
to enable support for additional connector types.

Components:
- ExtractedEntity / ExtractedRelationship: Data structures for discoveries
- DiscoveryQueue: Redis-backed queue with in-memory fallback
- BatchProcessor: Background processor for queued discoveries
- TopologyAutoDiscoveryService: Main orchestrator (uses SchemaBasedExtractor)

Usage:
    from meho_app.modules.topology.auto_discovery import (
        TopologyAutoDiscoveryService,
        get_auto_discovery_service,
    )

    # Get the service
    service = get_auto_discovery_service()

    # Process connector operation result
    count = await service.process_operation_result(
        connector_type="kubernetes",
        connector_id="abc123",
        connector_name="Production K8s",
        operation_id="list_pods",
        result_data={"kind": "PodList", "items": [...]},
        tenant_id="tenant-1",
    )

See TASK-143 for architecture documentation.
See TASK-157 for extraction schema documentation.
"""

# Base types
from .base import (
    BaseExtractor,
    ExtractedEntity,
    ExtractedRelationship,
)

# Processor
from .processor import (
    BatchProcessor,
    get_batch_processor,
    get_processor_instance,
    reset_batch_processor,
)

# Queue
from .queue import (
    DiscoveryMessage,
    DiscoveryQueue,
    get_discovery_queue,
    reset_discovery_queue,
)

# Service
from .service import (
    TopologyAutoDiscoveryService,
    get_auto_discovery_service,
    reset_auto_discovery_service,
)

__all__ = [
    "BaseExtractor",
    # Processor
    "BatchProcessor",
    # Queue
    "DiscoveryMessage",
    "DiscoveryQueue",
    # Base types
    "ExtractedEntity",
    "ExtractedRelationship",
    # Service
    "TopologyAutoDiscoveryService",
    "get_auto_discovery_service",
    "get_batch_processor",
    "get_discovery_queue",
    "get_processor_instance",
    "reset_auto_discovery_service",
    "reset_batch_processor",
    "reset_discovery_queue",
]
