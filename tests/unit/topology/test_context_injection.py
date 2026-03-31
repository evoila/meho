# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for enhanced topology context injection (TOPO-03, TOPO-04).

Tests format_topology_context_for_prompt() with:
- Full neighbor chain (relationships + SAME_AS) per D-06
- Freshness timestamps as relative + absolute labels per D-07
- SAME_AS confidence markers (CONFIRMED/HIGH/MEDIUM/SUGGESTED) per D-08
- Token budget truncation with priority-based truncation
- Relationship/SAME_AS/possibly-related caps
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from uuid import uuid4

from meho_app.modules.topology.context_node import (
    TopologyContext,
    format_topology_context_for_prompt,
    _format_freshness,
    _format_confidence,
)
from meho_app.modules.topology.schemas import TopologyEntity


# =============================================================================
# Helpers
# =============================================================================

UTC = timezone.utc

# Fixed "now" for deterministic freshness tests
FROZEN_NOW = datetime(2026, 3, 22, 15, 0, 0, tzinfo=UTC)


def make_entity(
    name: str = "worker-01",
    entity_type: str = "Node",
    connector_type: str = "kubernetes",
    connector_name: str = "Production K8s",
    last_verified_at: datetime | None = None,
    discovered_at: datetime | None = None,
) -> TopologyEntity:
    """Create a TopologyEntity for testing."""
    return TopologyEntity(
        id=uuid4(),
        name=name,
        entity_type=entity_type,
        connector_type=connector_type,
        connector_id=uuid4(),
        connector_name=connector_name,
        scope={},
        canonical_id=f"{connector_type}/{name}",
        description=f"Test entity {name}",
        raw_attributes={},
        discovered_at=discovered_at or FROZEN_NOW - timedelta(days=1),
        last_verified_at=last_verified_at,
        stale_at=None,
        tenant_id="test-tenant",
    )


def make_relationship(
    from_entity: str,
    to_entity: str,
    relationship_type: str = "runs_on",
    last_verified_at: datetime | None = None,
) -> dict:
    """Create a relationship dict for TopologyContext."""
    return {
        "from_entity": from_entity,
        "to_entity": to_entity,
        "relationship_type": relationship_type,
        "last_verified_at": last_verified_at,
    }


def make_same_as(
    name: str = "vm-web-01",
    entity_type: str = "VM",
    connector_name: str = "Production vCenter",
    connector_type: str = "vmware",
    verified_via: list[str] | None = None,
    similarity_score: float = 0.99,
    last_verified_at: datetime | None = None,
) -> dict:
    """Create a SAME_AS dict for TopologyContext."""
    entity = make_entity(
        name=name,
        entity_type=entity_type,
        connector_type=connector_type,
        connector_name=connector_name,
        last_verified_at=last_verified_at,
    )
    return {
        "entity": entity,
        "connector_name": connector_name,
        "verified_via": verified_via or [],
        "similarity_score": similarity_score,
    }


def make_context(
    query: str = "worker-01",
    found: bool = True,
    entity: TopologyEntity | None = None,
    relationships: list[dict] | None = None,
    same_as_entities: list[dict] | None = None,
    possibly_related: list[dict] | None = None,
) -> TopologyContext:
    """Create a TopologyContext with enhanced fields."""
    return TopologyContext(
        query=query,
        found=found,
        entity=entity or make_entity(name=query),
        relationships=relationships or [],
        same_as_entities=same_as_entities or [],
        connectors=[],
        possibly_related=possibly_related or [],
    )


# =============================================================================
# Empty / No-Found Tests
# =============================================================================


class TestEmptyContexts:
    """Tests for empty or no-found context handling."""

    def test_empty_contexts_returns_empty_string(self):
        """format_topology_context_for_prompt([]) returns empty string."""
        result = format_topology_context_for_prompt([])
        assert result == ""

    def test_no_found_contexts_returns_empty(self):
        """All contexts with found=False returns empty string."""
        contexts = [
            TopologyContext(
                query="unknown-entity",
                found=False,
                entity=None,
                relationships=[],
                same_as_entities=[],
                connectors=[],
                possibly_related=[],
            ),
            TopologyContext(
                query="another-unknown",
                found=False,
                entity=None,
                relationships=[],
                same_as_entities=[],
                connectors=[],
                possibly_related=[],
            ),
        ]
        result = format_topology_context_for_prompt(contexts)
        assert result == ""


# =============================================================================
# Full Neighbor Chain Test (D-06)
# =============================================================================


class TestFullNeighborChain:
    """Tests for full neighbor chain format per D-06."""

    def test_full_neighbor_chain(self):
        """Found context with entity + relationships + SAME_AS produces full format."""
        entity = make_entity(
            name="worker-01",
            entity_type="Node",
            connector_name="Production K8s",
            last_verified_at=FROZEN_NOW - timedelta(hours=2),
        )
        relationships = [
            make_relationship(
                "pod-frontend-7f8d9",
                "worker-01",
                "runs_on",
                last_verified_at=FROZEN_NOW - timedelta(hours=1),
            ),
            make_relationship(
                "worker-01",
                "prod-cluster",
                "member_of",
                last_verified_at=FROZEN_NOW - timedelta(days=3),
            ),
        ]
        same_as = [
            make_same_as(
                name="vm-web-01",
                entity_type="VM",
                connector_name="Production vCenter",
                verified_via=[
                    "deterministic_resolution",
                    "match_type:provider_id",
                    'matched_values:{"providerID": "gce://project/zone/instance"}',
                    "confidence:0.99",
                ],
                last_verified_at=FROZEN_NOW - timedelta(hours=4),
            ),
        ]

        ctx = make_context(
            query="worker-01",
            entity=entity,
            relationships=relationships,
            same_as_entities=same_as,
        )

        with patch(
            "meho_app.modules.topology.context_node.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = FROZEN_NOW
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            result = format_topology_context_for_prompt([ctx])

        # Entity header
        assert "**worker-01** (Node) [Production K8s]" in result
        # Relationships section
        assert "Relationships:" in result
        assert "pod-frontend-7f8d9" in result
        assert "runs_on" in result
        assert "member_of" in result
        # SAME_AS section
        assert "SAME_AS:" in result
        assert "vm-web-01" in result
        assert "VM" in result
        assert "Production vCenter" in result
        # Confidence label
        assert "CONFIRMED" in result


# =============================================================================
# Freshness Label Tests (D-07)
# =============================================================================


class TestFreshnessLabels:
    """Tests for freshness timestamps as relative + absolute labels per D-07."""

    def test_freshness_labels_recent(self):
        """Entity verified 30 minutes ago shows '30 minutes ago (ISO)'."""
        ts = FROZEN_NOW - timedelta(minutes=30)
        with patch(
            "meho_app.modules.topology.context_node.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = FROZEN_NOW
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            result = _format_freshness(ts)
        assert "30 minutes ago" in result
        assert ts.isoformat() in result

    def test_freshness_labels_hours(self):
        """Entity verified 5 hours ago shows '5 hours ago (ISO)'."""
        ts = FROZEN_NOW - timedelta(hours=5)
        with patch(
            "meho_app.modules.topology.context_node.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = FROZEN_NOW
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            result = _format_freshness(ts)
        assert "5 hours ago" in result
        assert ts.isoformat() in result

    def test_freshness_labels_days(self):
        """Entity verified 3 days ago shows '3 days ago (ISO)'."""
        ts = FROZEN_NOW - timedelta(days=3)
        with patch(
            "meho_app.modules.topology.context_node.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = FROZEN_NOW
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            result = _format_freshness(ts)
        assert "3 days ago" in result
        assert ts.isoformat() in result

    def test_freshness_labels_unknown(self):
        """Entity with no timestamp produces 'Unknown'."""
        result = _format_freshness(None)
        assert result == "Unknown"

    def test_freshness_just_now(self):
        """Entity verified <60 seconds ago shows 'just now'."""
        ts = FROZEN_NOW - timedelta(seconds=30)
        with patch(
            "meho_app.modules.topology.context_node.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = FROZEN_NOW
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            result = _format_freshness(ts)
        assert "just now" in result


# =============================================================================
# Confidence Label Tests (D-08)
# =============================================================================


class TestConfidenceLabels:
    """Tests for SAME_AS confidence labels per D-08."""

    def test_confidence_label_confirmed(self):
        """verified_via containing 'providerID' produces 'CONFIRMED (providerID match: ...)'."""
        verified_via = [
            "deterministic_resolution",
            "match_type:provider_id",
            'matched_values:{"providerID": "gce://project/zone/instance"}',
            "confidence:0.99",
        ]
        result = _format_confidence(verified_via)
        assert result.startswith("CONFIRMED")
        assert "providerID match" in result

    def test_confidence_label_high(self):
        """verified_via containing 'IP' produces 'HIGH (IP match: ...)'."""
        verified_via = [
            "deterministic_resolution",
            "match_type:ip_address",
            'matched_values:{"ip": "10.128.0.42"}',
            "confidence:0.95",
        ]
        result = _format_confidence(verified_via)
        assert result.startswith("HIGH")
        assert "IP match" in result

    def test_confidence_label_medium(self):
        """verified_via containing 'hostname' produces 'MEDIUM (hostname partial: ...)'."""
        verified_via = [
            "deterministic_resolution",
            "match_type:hostname",
            'matched_values:{"hostname": "web-server-01"}',
            "confidence:0.80",
        ]
        result = _format_confidence(verified_via)
        assert result.startswith("MEDIUM")
        assert "hostname partial" in result

    def test_confidence_label_suggested(self):
        """verified_via with embedding similarity produces 'SUGGESTED (...)'."""
        verified_via = [
            "embedding_similarity",
            "confidence:0.65",
        ]
        result = _format_confidence(verified_via)
        assert result.startswith("SUGGESTED")

    def test_confidence_empty_verified_via(self):
        """Empty verified_via returns SUGGESTED with UNKNOWN."""
        result = _format_confidence([])
        assert "SUGGESTED" in result


# =============================================================================
# Cap Tests
# =============================================================================


class TestCaps:
    """Tests for relationship, SAME_AS, and possibly-related caps."""

    def test_relationship_cap_at_10(self):
        """Entity with 15 relationships only shows first 10 plus '... and 5 more'."""
        entity = make_entity(last_verified_at=FROZEN_NOW - timedelta(hours=1))
        relationships = [
            make_relationship(
                f"pod-{i}",
                "worker-01",
                "runs_on",
                last_verified_at=FROZEN_NOW - timedelta(hours=1),
            )
            for i in range(15)
        ]
        ctx = make_context(
            entity=entity,
            relationships=relationships,
        )

        with patch(
            "meho_app.modules.topology.context_node.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = FROZEN_NOW
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            result = format_topology_context_for_prompt([ctx])

        assert "... and 5 more" in result
        # Should only show 10 relationship lines (not 15)
        rel_lines = [line for line in result.split("\n") if "runs_on" in line]
        assert len(rel_lines) == 10

    def test_same_as_cap_at_5(self):
        """Entity with 8 SAME_AS links only shows first 5."""
        entity = make_entity(last_verified_at=FROZEN_NOW - timedelta(hours=1))
        same_as = [
            make_same_as(
                name=f"vm-{i}",
                verified_via=[
                    "deterministic_resolution",
                    "match_type:provider_id",
                    f'matched_values:{{"providerID": "gce://project/zone/instance-{i}"}}',
                    "confidence:0.99",
                ],
                last_verified_at=FROZEN_NOW - timedelta(hours=i + 1),
            )
            for i in range(8)
        ]
        ctx = make_context(
            entity=entity,
            same_as_entities=same_as,
        )

        with patch(
            "meho_app.modules.topology.context_node.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = FROZEN_NOW
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            result = format_topology_context_for_prompt([ctx])

        # Count SAME_AS entity lines (lines starting with "    == ")
        same_as_lines = [line for line in result.split("\n") if line.strip().startswith("== ")]
        assert len(same_as_lines) == 5

    def test_possibly_related_cap_at_3(self):
        """6 possibly related entities only shows first 3."""
        entity = make_entity(last_verified_at=FROZEN_NOW - timedelta(hours=1))
        possibly_related = [
            {"entity": f"maybe-{i}", "similarity": 0.7 + i * 0.01}
            for i in range(6)
        ]
        ctx = make_context(
            entity=entity,
            possibly_related=possibly_related,
        )

        with patch(
            "meho_app.modules.topology.context_node.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = FROZEN_NOW
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            result = format_topology_context_for_prompt([ctx])

        # Count possibly-related lines
        related_lines = [line for line in result.split("\n") if "maybe-" in line]
        assert len(related_lines) == 3


# =============================================================================
# Token Budget Tests
# =============================================================================


class TestTokenBudget:
    """Tests for token budget truncation."""

    def test_token_budget_truncation(self):
        """Very large context gets truncated with truncation message when exceeding ~8000 chars."""
        entity = make_entity(last_verified_at=FROZEN_NOW - timedelta(hours=1))
        # Create many relationships to blow up the context
        relationships = [
            make_relationship(
                f"pod-very-long-name-for-testing-{i:04d}",
                "worker-01",
                "runs_on",
                last_verified_at=FROZEN_NOW - timedelta(hours=1),
            )
            for i in range(10)
        ]
        # Many SAME_AS entities with long names
        same_as = [
            make_same_as(
                name=f"vm-very-long-name-for-testing-{i:04d}",
                verified_via=[
                    "deterministic_resolution",
                    "match_type:provider_id",
                    f'matched_values:{{"providerID": "gce://long-project-name/us-central1-long-zone/instance-with-very-long-name-{i:04d}"}}',
                    "confidence:0.99",
                ],
                last_verified_at=FROZEN_NOW - timedelta(hours=i + 1),
            )
            for i in range(5)
        ]

        # Create multiple contexts to exceed the budget
        contexts = []
        for i in range(10):
            ctx = make_context(
                query=f"entity-{i}",
                entity=make_entity(
                    name=f"entity-with-very-long-name-for-testing-purposes-{i:04d}",
                    last_verified_at=FROZEN_NOW - timedelta(hours=1),
                ),
                relationships=relationships,
                same_as_entities=same_as,
                possibly_related=[
                    {"entity": f"related-entity-with-long-name-{j}", "similarity": 0.78}
                    for j in range(3)
                ],
            )
            contexts.append(ctx)

        with patch(
            "meho_app.modules.topology.context_node.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = FROZEN_NOW
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            # Use a small token budget to force truncation
            result = format_topology_context_for_prompt(contexts, token_budget=500)

        assert "truncated for token budget" in result
        # Result should be approximately at the budget limit (500 * 4 = 2000 chars)
        assert len(result) < 2500  # some slack for the truncation message


# =============================================================================
# Output Format Tests
# =============================================================================


class TestOutputFormat:
    """Tests for output format compliance."""

    def test_output_starts_with_known_topology_header(self):
        """Output starts with '## Known Topology'."""
        ctx = make_context(
            entity=make_entity(last_verified_at=FROZEN_NOW - timedelta(hours=1)),
        )

        with patch(
            "meho_app.modules.topology.context_node.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = FROZEN_NOW
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            result = format_topology_context_for_prompt([ctx])

        assert result.startswith("## Known Topology")

    def test_output_contains_last_seen_for_entity(self):
        """Output contains 'Last seen:' line for the entity."""
        entity = make_entity(
            last_verified_at=FROZEN_NOW - timedelta(hours=2),
        )
        ctx = make_context(entity=entity)

        with patch(
            "meho_app.modules.topology.context_node.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = FROZEN_NOW
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            result = format_topology_context_for_prompt([ctx])

        assert "Last seen:" in result
        assert "2 hours ago" in result
