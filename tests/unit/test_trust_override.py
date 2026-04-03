# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for per-endpoint trust level overrides.

Phase 5, Plan 04: Tests the full override priority chain from
safety_level vocabulary mapping through classify_operation.
"""

from meho_app.modules.agents.approval.trust_classifier import (
    classify_operation,
    safety_level_to_trust_tier,
)
from meho_app.modules.agents.models import TrustTier

# ============================================================================
# safety_level_to_trust_tier mapping tests
# ============================================================================


class TestSafetyLevelToTrustTier:
    """Tests for the DB safety_level -> TrustTier mapping function."""

    def test_safety_level_auto_means_no_override(self):
        """'auto' returns None -- use heuristic classification."""
        assert safety_level_to_trust_tier("auto") is None

    def test_safety_level_safe_means_no_override(self):
        """'safe' (old default) returns None -- use heuristic classification."""
        assert safety_level_to_trust_tier("safe") is None

    def test_none_means_no_override(self):
        """None returns None -- no override stored."""
        assert safety_level_to_trust_tier(None) is None

    def test_empty_string_means_no_override(self):
        """Empty string returns None -- no override stored."""
        assert safety_level_to_trust_tier("") is None

    def test_read_maps_to_read(self):
        """New vocabulary: 'read' maps to TrustTier.READ."""
        assert safety_level_to_trust_tier("read") == TrustTier.READ

    def test_write_maps_to_write(self):
        """New vocabulary: 'write' maps to TrustTier.WRITE."""
        assert safety_level_to_trust_tier("write") == TrustTier.WRITE

    def test_destructive_maps_to_destructive(self):
        """New vocabulary: 'destructive' maps to TrustTier.DESTRUCTIVE."""
        assert safety_level_to_trust_tier("destructive") == TrustTier.DESTRUCTIVE

    def test_legacy_caution_maps_to_write(self):
        """Legacy vocabulary: 'caution' maps to TrustTier.WRITE."""
        assert safety_level_to_trust_tier("caution") == TrustTier.WRITE

    def test_legacy_dangerous_maps_to_write(self):
        """Legacy vocabulary: 'dangerous' maps to TrustTier.WRITE."""
        assert safety_level_to_trust_tier("dangerous") == TrustTier.WRITE

    def test_legacy_critical_maps_to_destructive(self):
        """Legacy vocabulary: 'critical' maps to TrustTier.DESTRUCTIVE."""
        assert safety_level_to_trust_tier("critical") == TrustTier.DESTRUCTIVE

    def test_unknown_safety_level_returns_none(self):
        """Unknown value returns None -- no match, use heuristic."""
        assert safety_level_to_trust_tier("banana") is None

    def test_case_insensitive(self):
        """Mapping is case-insensitive."""
        assert safety_level_to_trust_tier("READ") == TrustTier.READ
        assert safety_level_to_trust_tier("Write") == TrustTier.WRITE
        assert safety_level_to_trust_tier("DESTRUCTIVE") == TrustTier.DESTRUCTIVE


# ============================================================================
# classify_operation override priority tests
# ============================================================================


class TestOverridePriority:
    """Tests that per-endpoint overrides take highest priority in classify_operation."""

    def test_no_override_uses_http_method_heuristic(self):
        """Without override, REST classification uses HTTP method."""
        # GET -> READ
        assert (
            classify_operation(
                connector_type="rest",
                operation_id="getUser",
                http_method="GET",
                override=None,
            )
            == TrustTier.READ
        )

        # POST -> WRITE
        assert (
            classify_operation(
                connector_type="rest",
                operation_id="createUser",
                http_method="POST",
                override=None,
            )
            == TrustTier.WRITE
        )

        # DELETE -> DESTRUCTIVE
        assert (
            classify_operation(
                connector_type="rest",
                operation_id="deleteUser",
                http_method="DELETE",
                override=None,
            )
            == TrustTier.DESTRUCTIVE
        )

    def test_read_override_on_post_allows_auto_approval(self):
        """Override=READ on a POST endpoint -> classified as READ (auto-approved)."""
        tier = classify_operation(
            connector_type="rest",
            operation_id="searchUsers",
            http_method="POST",
            override=TrustTier.READ,
        )
        assert tier == TrustTier.READ

    def test_destructive_override_on_get_requires_approval(self):
        """Override=DESTRUCTIVE on a GET endpoint -> classified as DESTRUCTIVE."""
        tier = classify_operation(
            connector_type="rest",
            operation_id="exportData",
            http_method="GET",
            override=TrustTier.DESTRUCTIVE,
        )
        assert tier == TrustTier.DESTRUCTIVE

    def test_write_override_on_delete_downgrades_to_write(self):
        """Override=WRITE on a DELETE endpoint -> classified as WRITE (not DESTRUCTIVE)."""
        tier = classify_operation(
            connector_type="rest",
            operation_id="softDeleteUser",
            http_method="DELETE",
            override=TrustTier.WRITE,
        )
        assert tier == TrustTier.WRITE

    def test_override_takes_priority_over_typed_connector_registry(self):
        """Override takes priority even for typed connectors with static registry."""
        # Kubernetes list_pods is normally READ in the registry.
        # With DESTRUCTIVE override, it should be DESTRUCTIVE.
        tier = classify_operation(
            connector_type="kubernetes",
            operation_id="list_pods",
            http_method=None,
            override=TrustTier.DESTRUCTIVE,
        )
        assert tier == TrustTier.DESTRUCTIVE

    def test_override_none_falls_through_to_heuristic(self):
        """override=None means the classifier uses normal priority chain."""
        tier = classify_operation(
            connector_type="rest",
            operation_id="getData",
            http_method="HEAD",
            override=None,
        )
        assert tier == TrustTier.READ
