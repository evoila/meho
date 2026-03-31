"""Topology engine: entity storage, correlation, extraction, and graph queries.

Public API for the topology subsystem.
"""

from meho_claude.core.topology.correlator import CorrelationEngine
from meho_claude.core.topology.extractor import (
    BaseEntityExtractor,
    get_extractor_class,
    register_extractor,
    run_extraction,
)
from meho_claude.core.topology.models import (
    ExtractionResult,
    TopologyCorrelation,
    TopologyEntity,
    TopologyRelationship,
)
from meho_claude.core.topology.store import TopologyStore

__all__ = [
    "BaseEntityExtractor",
    "CorrelationEngine",
    "ExtractionResult",
    "TopologyCorrelation",
    "TopologyEntity",
    "TopologyRelationship",
    "TopologyStore",
    "get_extractor_class",
    "register_extractor",
    "run_extraction",
]
