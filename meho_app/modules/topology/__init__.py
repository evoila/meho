# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Topology module public exports.

This module enables MEHO to learn and remember system topology
as it investigates systems. Entities, relationships, and cross-connector
correlations are discovered and stored for future reference.

Usage:
    from meho_app.modules.topology import TopologyService, get_topology_service

    # Auto-discovery (TASK-143, TASK-157)
    from meho_app.modules.topology import (
        TopologyAutoDiscoveryService,
        get_auto_discovery_service,
    )
"""

# Auto-discovery (TASK-143, TASK-157)
from .auto_discovery import (
    BatchProcessor,
    DiscoveryMessage,
    DiscoveryQueue,
    ExtractedEntity,
    ExtractedRelationship,
    TopologyAutoDiscoveryService,
    get_auto_discovery_service,
    get_batch_processor,
    get_discovery_queue,
    reset_auto_discovery_service,
)
from .context_node import (
    TopologyContext,
    TopologyContextService,
    format_topology_context_for_prompt,
    get_topology_context_service,
)
from .correlation import (
    CorrelationService,
    get_correlation_service,
)
from .embedding import (
    TopologyEmbeddingService,
    get_topology_embedding_service,
)
from .entity_extractor import (
    EntityExtractor,
    extract_entity_references,
    get_entity_extractor,
)

# Note: tool_nodes are NOT imported here to avoid circular imports
# They are imported lazily in reason_node.py where they're used
# Import them directly: from meho_app.modules.topology.tool_nodes import ...
from .schemas import (
    InvalidateTopologyInput,
    InvalidateTopologyResult,
    LookupTopologyInput,
    LookupTopologyResult,
    PossiblyRelatedEntity,
    RelationshipType,
    StoreDiscoveryInput,
    StoreDiscoveryResult,
    TopologyChainItem,
    TopologyEntity,
    TopologyEntityCreate,
    TopologyRelationship,
    TopologyRelationshipCreate,
    TopologySameAs,
    TopologySameAsCreate,
)
from .service import TopologyService, get_topology_service

__all__ = [
    "BatchProcessor",
    # Correlation
    "CorrelationService",
    "DiscoveryMessage",
    "DiscoveryQueue",
    # Entity Extractor
    "EntityExtractor",
    "ExtractedEntity",
    "ExtractedRelationship",
    "InvalidateTopologyInput",
    "InvalidateTopologyResult",
    "LookupTopologyInput",
    "LookupTopologyResult",
    "PossiblyRelatedEntity",
    "RelationshipType",
    # Tool input/output schemas
    "StoreDiscoveryInput",
    "StoreDiscoveryResult",
    # Auto-discovery (TASK-143, TASK-157)
    "TopologyAutoDiscoveryService",
    # Supporting schemas
    "TopologyChainItem",
    # Context Node
    "TopologyContext",
    "TopologyContextService",
    # Embedding
    "TopologyEmbeddingService",
    # Note: Tool Nodes (LookupTopologyNode, etc.) NOT exported here
    # to avoid circular imports - import from .tool_nodes directly
    # Entity schemas
    "TopologyEntity",
    "TopologyEntityCreate",
    # Relationship schemas
    "TopologyRelationship",
    "TopologyRelationshipCreate",
    # SAME_AS schemas
    "TopologySameAs",
    "TopologySameAsCreate",
    # Service
    "TopologyService",
    "extract_entity_references",
    "format_topology_context_for_prompt",
    "get_auto_discovery_service",
    "get_batch_processor",
    "get_correlation_service",
    "get_discovery_queue",
    "get_entity_extractor",
    "get_topology_context_service",
    "get_topology_embedding_service",
    "get_topology_service",
    "reset_auto_discovery_service",
]
