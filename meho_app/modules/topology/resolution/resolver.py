# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
DeterministicResolver orchestrator for entity resolution.

Applies attribute matchers in priority order (ProviderID > IP > Hostname)
to determine if two entities represent the same physical resource.

Checks SameAsEligibility before running matchers to prevent nonsensical
comparisons (e.g., Pod vs VM). Entities from the same connector are
automatically skipped.

For batch resolution, uses pairwise comparison with early filtering.
"""

from meho_app.modules.topology.models import TopologyEntityModel
from meho_app.modules.topology.resolution.evidence import MatchEvidence
from meho_app.modules.topology.resolution.matchers.base import BaseMatcher
from meho_app.modules.topology.schema import get_topology_schema


class DeterministicResolver:
    """
    Orchestrates matchers in priority order for entity resolution.

    Usage:
        resolver = DeterministicResolver(matchers=[
            ProviderIDMatcher(),
            IPAddressMatcher(),
            HostnameMatcher(),
        ])

        evidence = resolver.resolve_pair(k8s_node, gcp_instance)
        if evidence and evidence.auto_confirm:
            # Create SAME_AS relationship
            ...
    """

    def __init__(self, matchers: list[BaseMatcher]):
        """Initialize with matchers sorted by priority (lowest number first)."""
        self.matchers = sorted(matchers, key=lambda m: m.priority)

    def resolve_pair(
        self,
        entity_a: TopologyEntityModel,
        entity_b: TopologyEntityModel,
    ) -> MatchEvidence | None:
        """
        Try matchers in priority order, return first match.

        Pre-checks:
        1. Same connector -> skip (entities from same connector can't be SAME_AS)
        2. SameAsEligibility -> skip incompatible types

        Returns MatchEvidence on first match, None if no matcher succeeds.
        """
        # Same connector check
        if entity_a.connector_id and entity_b.connector_id:  # noqa: SIM102 -- readability preferred over collapse
            if entity_a.connector_id == entity_b.connector_id:
                return None

        # SameAsEligibility check
        if not self._are_eligible(entity_a, entity_b):
            return None

        # Try matchers in priority order
        for matcher in self.matchers:
            evidence = matcher.match(entity_a, entity_b)
            if evidence:
                return evidence

        return None

    def resolve_batch(
        self,
        entities_a: list[TopologyEntityModel],
        entities_b: list[TopologyEntityModel],
    ) -> list[tuple[TopologyEntityModel, TopologyEntityModel, MatchEvidence]]:
        """
        Compare two lists of entities and return all matches.

        Returns list of (entity_a, entity_b, MatchEvidence) tuples.
        Uses pairwise comparison with eligibility pre-filtering.
        """
        results: list[tuple[TopologyEntityModel, TopologyEntityModel, MatchEvidence]] = []

        if not entities_a or not entities_b:
            return results

        for entity_a in entities_a:
            for entity_b in entities_b:
                evidence = self.resolve_pair(entity_a, entity_b)
                if evidence:
                    results.append((entity_a, entity_b, evidence))

        return results

    def _are_eligible(
        self,
        entity_a: TopologyEntityModel,
        entity_b: TopologyEntityModel,
    ) -> bool:
        """
        Check if two entities are eligible for SAME_AS matching.

        Uses SameAsEligibility from the topology schema registry.
        If no schema is found for the connector type, allows matching
        (to support REST/SOAP and other non-schema connectors).
        """
        # Get schemas for both entities
        schema_a = get_topology_schema(entity_a.connector_type)
        schema_b = get_topology_schema(entity_b.connector_type)

        # Check entity_a's eligibility to match entity_b's type
        if schema_a:
            defn_a = schema_a.get_entity_definition(entity_a.entity_type)
            if defn_a:
                if defn_a.same_as is None:
                    return False
                if not defn_a.same_as.can_correlate_with(entity_b.entity_type):
                    return False

        # Check entity_b's eligibility to match entity_a's type
        if schema_b:
            defn_b = schema_b.get_entity_definition(entity_b.entity_type)
            if defn_b:
                if defn_b.same_as is None:
                    return False
                if not defn_b.same_as.can_correlate_with(entity_a.entity_type):
                    return False

        return True
