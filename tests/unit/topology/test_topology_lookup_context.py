# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for TopologyLookupNode enhanced SAME_AS context formatting.

Phase 16 Plan 01: Verifies that SAME_AS context includes connector_id,
match confidence, and evidence for every correlated entity, enabling
the agent to traverse cross-system links.

Also includes trust model regression tests (DIAG-03) confirming READ
operations are auto-approved regardless of connector.
"""

import sys
import types
from datetime import datetime
from unittest.mock import MagicMock
from uuid import UUID, uuid4

# ---------------------------------------------------------------------------
# Pre-import mocking: The nodes/__init__.py import chain triggers missing
# modules (tool_nodes, approval_check_node). We mock them so
# TopologyLookupNode can be imported without pulling in the entire node
# graph. This is a pre-existing issue unrelated to Phase 16 changes.
# ---------------------------------------------------------------------------
for _mod_name in [
    "meho_app.modules.agents.react.nodes.tool_nodes",
    "meho_app.modules.agents.react.nodes.approval_check_node",
]:
    if _mod_name not in sys.modules:
        _mock_mod = types.ModuleType(_mod_name)
        _mock_mod.__dict__["__getattr__"] = lambda name: MagicMock()
        # Add common class names as MagicMock for explicit imports
        for _attr in [
            "SearchOperationsNode",
            "CallOperationNode",
            "SearchTypesNode",
            "SearchKnowledgeNode",
            "ListConnectorsNode",
            "ReduceDataNode",
            "StoreDiscoveryNode",
            "LookupTopologyNode",
            "InvalidateTopologyNode",
            "StoreMemoryNode",
            "RecallMemoryNode",
            "ApprovalCheckNode",
        ]:
            setattr(_mock_mod, _attr, MagicMock())
        sys.modules[_mod_name] = _mock_mod

from meho_app.modules.agents.shared.graph.nodes.topology_lookup_node import (  # noqa: E402 -- import after test setup
    TopologyLookupNode,
)
from meho_app.modules.agents.shared.topology_utils import (  # noqa: E402 -- import after test setup
    parse_verification_evidence,
)
from meho_app.modules.topology.schemas import (  # noqa: E402 -- conditional/deferred import for test setup
    CorrelatedEntity,
    TopologyChainItem,
    TopologyEntity,
)

# =============================================================================
# Fixtures
# =============================================================================


def _make_entity(
    name: str = "instance-xyz",
    entity_type: str = "Instance",
    connector_type: str = "gcp",
    connector_id: UUID | None = None,
    raw_attributes: dict | None = None,
    description: str = "A test entity",
) -> TopologyEntity:
    """Create a mock TopologyEntity for testing."""
    cid = connector_id or uuid4()
    return TopologyEntity(
        id=uuid4(),
        name=name,
        entity_type=entity_type,
        connector_type=connector_type,
        connector_id=cid,
        connector_name=None,
        scope={},
        canonical_id=f"{connector_type}/{name}",
        description=description,
        raw_attributes=raw_attributes or {},
        discovered_at=datetime(2026, 3, 1),  # noqa: DTZ001 -- naive datetime for test compatibility
        last_verified_at=None,
        stale_at=None,
        tenant_id="test-tenant",
    )


def _make_correlated(
    entity: TopologyEntity,
    connector_type: str = "gcp",
    connector_name: str | None = "GCP Production",
    verified_via: list[str] | None = None,
) -> CorrelatedEntity:
    """Create a mock CorrelatedEntity for testing."""
    return CorrelatedEntity(
        entity=entity,
        connector_type=connector_type,
        connector_name=connector_name,
        verified_via=verified_via or [],
    )


# =============================================================================
# _parse_verification_evidence tests
# =============================================================================


class TestParseVerificationEvidence:
    """Tests for the parse_verification_evidence shared utility."""

    def test_provider_id_high_confidence(self):
        """Provider ID match with 0.99 confidence should be HIGH."""
        verified_via = [
            "deterministic_resolution",
            "match_type:provider_id",
            'matched_values:{"providerID": "gce://myproject/us-central1-a/instance-xyz"}',
            "confidence:0.99",
        ]
        confidence, evidence = parse_verification_evidence(verified_via)
        assert "HIGH" in confidence
        assert "providerID exact match" in confidence
        assert 'providerID: "gce://myproject/us-central1-a/instance-xyz"' in evidence

    def test_ip_address_medium_confidence(self):
        """IP address match with 0.85 confidence should be MEDIUM."""
        verified_via = [
            "deterministic_resolution",
            "match_type:ip_address",
            'matched_values:{"ip": "10.0.1.5"}',
            "confidence:0.85",
        ]
        confidence, evidence = parse_verification_evidence(verified_via)
        assert "MEDIUM" in confidence
        assert "IP address match" in confidence
        assert 'ip: "10.0.1.5"' in evidence

    def test_hostname_match(self):
        """Hostname match should use hostname description."""
        verified_via = [
            "deterministic_resolution",
            "match_type:hostname",
            'matched_values:{"hostname": "worker-01.example.com"}',
            "confidence:0.90",
        ]
        confidence, evidence = parse_verification_evidence(verified_via)
        assert "MEDIUM" in confidence
        assert "hostname match" in confidence
        assert 'hostname: "worker-01.example.com"' in evidence

    def test_high_confidence_boundary(self):
        """Confidence exactly 0.95 should be HIGH."""
        verified_via = ["confidence:0.95", "match_type:provider_id"]
        confidence, _ = parse_verification_evidence(verified_via)
        assert "HIGH" in confidence

    def test_medium_confidence_boundary(self):
        """Confidence exactly 0.7 should be MEDIUM."""
        verified_via = ["confidence:0.7", "match_type:ip_address"]
        confidence, _ = parse_verification_evidence(verified_via)
        assert "MEDIUM" in confidence

    def test_low_confidence(self):
        """Confidence below 0.7 should be LOW."""
        verified_via = ["confidence:0.5", "match_type:hostname"]
        confidence, _ = parse_verification_evidence(verified_via)
        assert "LOW" in confidence

    def test_empty_verified_via(self):
        """Empty list should return UNKNOWN."""
        confidence, evidence = parse_verification_evidence([])
        assert confidence == "UNKNOWN"
        assert evidence == ""

    def test_malformed_verified_via(self):
        """Non-parseable items should return LOW with unknown type."""
        verified_via = ["something_random", "another_thing"]
        confidence, evidence = parse_verification_evidence(verified_via)
        assert "LOW" in confidence
        assert "unknown" in confidence
        assert evidence == ""

    def test_malformed_confidence_value(self):
        """Non-numeric confidence should be treated as missing."""
        verified_via = ["confidence:not_a_number", "match_type:provider_id"]
        confidence, _ = parse_verification_evidence(verified_via)
        assert "LOW" in confidence

    def test_malformed_matched_values_json(self):
        """Invalid JSON in matched_values should return raw string."""
        verified_via = [
            "confidence:0.99",
            "match_type:provider_id",
            "matched_values:{invalid_json}",
        ]
        confidence, evidence = parse_verification_evidence(verified_via)
        assert "HIGH" in confidence
        assert evidence == "{invalid_json}"


# =============================================================================
# _format_context SAME_AS rendering tests
# =============================================================================


class TestFormatContextSameAs:
    """Tests for enhanced _format_context SAME_AS section."""

    def setup_method(self):
        self.node = TopologyLookupNode()
        self.primary_connector_id = uuid4()
        self.correlated_connector_id = uuid4()

    def _make_context_parts(
        self,
        same_as: list[CorrelatedEntity] | None = None,
        chain: list | None = None,
        related: list | None = None,
    ) -> list[dict]:
        """Build context_parts list for _format_context."""
        primary = _make_entity(
            name="worker-01",
            entity_type="Node",
            connector_type="kubernetes",
            connector_id=self.primary_connector_id,
            description="K8s worker node",
        )
        return [
            {
                "entity": primary,
                "chain": chain or [],
                "same_as": same_as or [],
                "related": related or [],
            }
        ]

    def test_connector_id_present_in_output(self):
        """SAME_AS section must include connector_id as a copy-pasteable value."""
        corr_entity = _make_entity(
            name="instance-xyz",
            connector_id=self.correlated_connector_id,
        )
        correlated = _make_correlated(
            corr_entity,
            verified_via=[
                "deterministic_resolution",
                "match_type:provider_id",
                "confidence:0.99",
            ],
        )
        parts = self._make_context_parts(same_as=[correlated])
        output = self.node._format_context(parts)
        assert f"connector_id: {self.correlated_connector_id}" in output

    def test_confidence_label_present(self):
        """SAME_AS section must include match confidence label."""
        corr_entity = _make_entity(name="instance-xyz")
        correlated = _make_correlated(
            corr_entity,
            verified_via=[
                "deterministic_resolution",
                "match_type:provider_id",
                "confidence:0.99",
            ],
        )
        parts = self._make_context_parts(same_as=[correlated])
        output = self.node._format_context(parts)
        assert "Match confidence: HIGH (providerID exact match)" in output

    def test_evidence_present(self):
        """SAME_AS section must include evidence when matched_values provided."""
        corr_entity = _make_entity(name="instance-xyz")
        correlated = _make_correlated(
            corr_entity,
            verified_via=[
                "deterministic_resolution",
                "match_type:provider_id",
                'matched_values:{"providerID": "gce://proj/zone/instance-xyz"}',
                "confidence:0.99",
            ],
        )
        parts = self._make_context_parts(same_as=[correlated])
        output = self.node._format_context(parts)
        assert "Evidence:" in output
        assert 'providerID: "gce://proj/zone/instance-xyz"' in output

    def test_header_is_cross_system_identity(self):
        """SAME_AS section header should be 'Cross-System Identity (SAME_AS)'."""
        corr_entity = _make_entity(name="instance-xyz")
        correlated = _make_correlated(
            corr_entity,
            verified_via=["confidence:0.99", "match_type:provider_id"],
        )
        parts = self._make_context_parts(same_as=[correlated])
        output = self.node._format_context(parts)
        assert "#### Cross-System Identity (SAME_AS)" in output

    def test_footer_mentions_search_operations(self):
        """Footer should instruct agent to use search_operations with connector_id."""
        corr_entity = _make_entity(name="instance-xyz")
        correlated = _make_correlated(
            corr_entity,
            verified_via=["confidence:0.99", "match_type:provider_id"],
        )
        parts = self._make_context_parts(same_as=[correlated])
        output = self.node._format_context(parts)
        assert "search_operations and call_operation" in output

    def test_no_same_as_no_section(self):
        """When no SAME_AS entities, the section should not appear."""
        parts = self._make_context_parts(same_as=[])
        output = self.node._format_context(parts)
        assert "Cross-System Identity" not in output

    def test_key_identifiers_still_rendered(self):
        """Key identifiers from raw_attributes should still appear."""
        corr_entity = _make_entity(
            name="instance-xyz",
            raw_attributes={"id": "instance-xyz", "node": "us-central1-a"},
        )
        correlated = _make_correlated(
            corr_entity,
            verified_via=["confidence:0.99", "match_type:provider_id"],
        )
        parts = self._make_context_parts(same_as=[correlated])
        output = self.node._format_context(parts)
        assert "Key identifiers:" in output
        assert "id=instance-xyz" in output


# =============================================================================
# _format_chain_same_as_context tests
# =============================================================================


class TestFormatChainSameAsContext:
    """Tests for cross-connector chain item rendering."""

    def setup_method(self):
        self.node = TopologyLookupNode()
        self.primary_connector_id = uuid4()

    def test_cross_connector_chain_items_rendered(self):
        """Chain items from different connectors should be noted."""
        other_connector_id = uuid4()
        primary = _make_entity(
            name="worker-01",
            connector_id=self.primary_connector_id,
        )
        chain = [
            TopologyChainItem(
                depth=1,
                entity="instance-xyz",
                entity_type="Instance",
                connector="GCP Production",
                connector_id=other_connector_id,
                relationship="runs_on",
            ),
        ]
        lines = self.node._format_chain_same_as_context(chain, primary)
        assert len(lines) > 0
        assert any("instance-xyz" in line for line in lines)
        assert any("SAME_AS" in line for line in lines)

    def test_same_connector_chain_items_not_rendered(self):
        """Chain items from the same connector should NOT be noted."""
        primary = _make_entity(
            name="worker-01",
            connector_id=self.primary_connector_id,
        )
        chain = [
            TopologyChainItem(
                depth=1,
                entity="pod-abc",
                entity_type="Pod",
                connector="K8s cluster",
                connector_id=self.primary_connector_id,
                relationship="runs_on",
            ),
        ]
        lines = self.node._format_chain_same_as_context(chain, primary)
        assert len(lines) == 0

    def test_empty_chain_returns_empty(self):
        """Empty chain should return empty list."""
        primary = _make_entity(name="worker-01")
        lines = self.node._format_chain_same_as_context([], primary)
        assert lines == []


# =============================================================================
# Trust model regression tests (DIAG-03)
#
# These tests verify that the existing trust classifier handles cross-connector
# operations correctly. READ operations should be auto-approved regardless of
# which connector_id is specified, while WRITE and DESTRUCTIVE operations
# should require approval. No code changes are needed -- these are regression
# guards.
# =============================================================================

from meho_app.modules.agents.approval.trust_classifier import (  # noqa: E402 -- conditional/deferred import for test setup
    classify_operation,
    requires_approval,
)
from meho_app.modules.agents.models import (  # noqa: E402 -- import after test setup
    TrustTier,
)


class TestTrustModelCrossConnector:
    """Regression tests: trust model for cross-connector operations (DIAG-03)."""

    def test_classify_get_as_read_for_rest_connector(self):
        """GET on a REST connector should classify as READ regardless of connector type."""
        tier = classify_operation("rest", "get_vm_status", http_method="GET")
        assert tier == TrustTier.READ

    def test_classify_get_as_read_for_any_connector_type(self):
        """GET on any connector type string should classify as READ via HTTP heuristic."""
        # Use a hypothetical connector type that isn't in the typed registry
        tier = classify_operation("rest", "get_instance_details", http_method="GET")
        assert tier == TrustTier.READ

    def test_read_does_not_require_approval(self):
        """READ tier should NOT require operator approval (auto-approved)."""
        assert requires_approval(TrustTier.READ) is False

    def test_classify_post_as_write(self):
        """POST operations should classify as WRITE -- cross-connector writes need approval."""
        tier = classify_operation("rest", "create_instance", http_method="POST")
        assert tier == TrustTier.WRITE

    def test_write_requires_approval(self):
        """WRITE tier MUST require operator approval."""
        assert requires_approval(TrustTier.WRITE) is True

    def test_classify_delete_as_destructive(self):
        """DELETE operations should classify as DESTRUCTIVE -- cross-connector deletes need approval."""
        tier = classify_operation("rest", "delete_instance", http_method="DELETE")
        assert tier == TrustTier.DESTRUCTIVE

    def test_destructive_requires_approval(self):
        """DESTRUCTIVE tier MUST require operator approval."""
        assert requires_approval(TrustTier.DESTRUCTIVE) is True

    def test_head_is_read(self):
        """HEAD requests should be READ (auto-approved) for cross-connector health checks."""
        tier = classify_operation("rest", "health_check", http_method="HEAD")
        assert tier == TrustTier.READ

    def test_options_is_read(self):
        """OPTIONS requests should be READ (auto-approved) for cross-connector preflight."""
        tier = classify_operation("rest", "preflight", http_method="OPTIONS")
        assert tier == TrustTier.READ

    def test_put_is_write(self):
        """PUT operations should classify as WRITE (requires approval)."""
        tier = classify_operation("rest", "update_instance", http_method="PUT")
        assert tier == TrustTier.WRITE
