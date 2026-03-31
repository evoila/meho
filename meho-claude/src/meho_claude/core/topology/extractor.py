"""Entity extraction framework for topology auto-discovery.

Provides the BaseEntityExtractor ABC, extractor registry (per connector type),
and the run_extraction() side-effect function that silently populates topology.db
after every successful connector call.

Extraction failure never breaks connector execution -- all exceptions are caught
and logged via structlog.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import structlog

from meho_claude.core.topology.models import ExtractionResult

logger = structlog.get_logger()

# Module-level registry: connector_type -> extractor class
_EXTRACTOR_REGISTRY: dict[str, type[BaseEntityExtractor]] = {}


class BaseEntityExtractor(ABC):
    """Abstract base class for topology entity extractors.

    Each connector type (kubernetes, vmware, etc.) can register an extractor
    that knows how to parse connector query results into topology entities
    and relationships.
    """

    @abstractmethod
    def extract(
        self,
        connector_name: str,
        connector_type: str,
        operation_id: str,
        result_data: dict[str, Any],
    ) -> ExtractionResult:
        """Extract topology entities and relationships from connector result data.

        Args:
            connector_name: Name of the connector (e.g., "prod-cluster").
            connector_type: Type of connector (e.g., "kubernetes").
            operation_id: The operation that produced this data (e.g., "list-pods").
            result_data: Raw result dict from connector execution.

        Returns:
            ExtractionResult with discovered entities and relationships.
        """
        ...


def register_extractor(connector_type: str):
    """Decorator to register an extractor class for a given connector type.

    Usage::

        @register_extractor("kubernetes")
        class K8sExtractor(BaseEntityExtractor):
            def extract(self, connector_name, connector_type, operation_id, result_data):
                ...

    Args:
        connector_type: The connector type this extractor handles.
    """

    def decorator(cls: type[BaseEntityExtractor]) -> type[BaseEntityExtractor]:
        _EXTRACTOR_REGISTRY[connector_type] = cls
        return cls

    return decorator


def get_extractor_class(connector_type: str) -> type[BaseEntityExtractor] | None:
    """Look up a registered extractor class by connector type.

    Returns None if no extractor is registered for this type. This is
    intentional: missing extractor is normal (e.g., REST has none).

    Args:
        connector_type: The connector type to look up.

    Returns:
        The extractor class, or None if not registered.
    """
    return _EXTRACTOR_REGISTRY.get(connector_type)


def run_extraction(
    state_dir: Path,
    connector_name: str,
    connector_type: str,
    operation_id: str,
    result_data: dict[str, Any],
) -> None:
    """Side-effect function: extract topology entities from connector results.

    Called from CLI after every successful connector call. Orchestrates:
    1. Look up extractor for this connector type
    2. Extract entities and relationships
    3. Store via TopologyStore.ingest()
    4. Embed changed entities via embed_topology_entities()

    This function NEVER raises -- all exceptions are caught and logged.
    If no extractor is registered for the connector type, returns silently.

    Args:
        state_dir: Path to meho state directory (~/.meho).
        connector_name: Name of the connector.
        connector_type: Type of connector.
        operation_id: The operation that produced the data.
        result_data: Raw result dict from connector execution.
    """
    try:
        # Lazy-import extractors package to trigger @register_extractor decorators
        try:
            import meho_claude.core.topology.extractors  # noqa: F401
        except ImportError:
            pass

        # Look up extractor class for this connector type
        extractor_cls = get_extractor_class(connector_type)
        if extractor_cls is None:
            return  # No extractor for this type (e.g., REST) -- silent return

        # Extract entities and relationships
        extractor = extractor_cls()
        extraction = extractor.extract(connector_name, connector_type, operation_id, result_data)

        # Skip if extraction yielded nothing
        if not extraction.entities and not extraction.relationships:
            return

        # Store via TopologyStore
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(state_dir)
        try:
            entities_needing_embedding = store.ingest(extraction)

            # Embed changed entities
            if entities_needing_embedding:
                embed_topology_entities(state_dir, entities_needing_embedding)
        finally:
            store.close()

    except Exception as exc:
        logger.warning(
            "topology_extraction_failed",
            connector_name=connector_name,
            connector_type=connector_type,
            operation_id=operation_id,
            error=str(exc),
        )


def embed_topology_entities(state_dir: Path, entities: list) -> None:
    """Embed topology entities into ChromaDB.

    This is a forward reference -- the actual implementation is in
    meho_claude.core.topology.search. This function is defined here
    as a convenience import target that search.py will replace at runtime.

    Args:
        state_dir: Path to meho state directory.
        entities: List of TopologyEntity objects to embed.
    """
    from meho_claude.core.topology.search import embed_topology_entities as _embed

    _embed(state_dir, entities)
