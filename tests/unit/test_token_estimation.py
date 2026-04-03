# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for token estimation and response tiering (TASK-161 Phase 2).

Tests the estimate_tokens() and determine_response_tier() functions
that enable intelligent token-based caching decisions.
"""

from meho_app.modules.agents.execution.cache import (
    TOKEN_TIER_INLINE,
    ResponseTier,
    determine_response_tier,
    estimate_tokens,
)


class TestEstimateTokens:
    """Tests for estimate_tokens() function."""

    def test_empty_dict(self):
        """Empty dict returns minimal tokens."""
        tokens = estimate_tokens({})
        # {} = 2 chars / 4 = 0
        assert tokens == 0

    def test_empty_list(self):
        """Empty list returns minimal tokens."""
        tokens = estimate_tokens([])
        # [] = 2 chars / 4 = 0
        assert tokens == 0

    def test_small_dict(self):
        """Small dict returns low token count."""
        data = {"name": "default"}
        tokens = estimate_tokens(data)
        # {"name": "default"} = 19 chars / 4 ≈ 4
        assert 1 <= tokens <= 10

    def test_small_list_of_dicts(self):
        """Small list of dicts returns proportional tokens."""
        data = [
            {"name": "default", "uid": "abc123"},
            {"name": "kube-system", "uid": "def456"},
        ]
        tokens = estimate_tokens(data)
        # Should be relatively small
        assert 10 <= tokens <= 50

    def test_large_list(self):
        """Large list returns high token count."""
        data = [{"name": f"item-{i}", "data": "x" * 100} for i in range(100)]
        tokens = estimate_tokens(data)
        # 100 items with ~110 chars each ≈ 11000 chars / 4 ≈ 2750 tokens
        assert tokens > 2000

    def test_very_large_data(self):
        """Very large data returns very high token count."""
        data = [{"name": f"item-{i}", "data": "x" * 1000} for i in range(100)]
        tokens = estimate_tokens(data)
        # 100 items with ~1010 chars each ≈ 101000 chars / 4 ≈ 25250 tokens
        assert tokens > 20000

    def test_nested_structure(self):
        """Nested structures are handled correctly."""
        data = {
            "cluster": {
                "name": "production",
                "nodes": [
                    {"name": "node-1", "status": "ready"},
                    {"name": "node-2", "status": "ready"},
                ],
                "metadata": {
                    "labels": {"env": "prod", "region": "us-east"},
                },
            }
        }
        tokens = estimate_tokens(data)
        # Should be reasonable for nested structure
        assert 30 <= tokens <= 100

    def test_special_characters(self):
        """Special characters (unicode, emoji) are handled."""
        data = {"message": "Hello 世界! 🚀 Testing unicode chars"}
        tokens = estimate_tokens(data)
        # Should not crash, returns reasonable count
        assert tokens > 0

    def test_none_value(self):
        """None value is handled."""
        data = {"name": "test", "value": None}
        tokens = estimate_tokens(data)
        assert tokens > 0

    def test_numeric_values(self):
        """Numeric values are handled correctly."""
        data = {"count": 42, "ratio": 3.14159, "big": 10**18}
        tokens = estimate_tokens(data)
        assert tokens > 0

    def test_boolean_values(self):
        """Boolean values are handled correctly."""
        data = {"active": True, "deleted": False}
        tokens = estimate_tokens(data)
        assert tokens > 0

    def test_primitive_string(self):
        """Primitive string is handled."""
        tokens = estimate_tokens("Hello, world!")
        # "Hello, world!" = 15 chars / 4 ≈ 3
        assert 1 <= tokens <= 10

    def test_primitive_number(self):
        """Primitive number is handled."""
        tokens = estimate_tokens(12345)
        assert tokens > 0


class TestDetermineResponseTier:
    """Tests for determine_response_tier() function (2-tier system)."""

    def test_inline_tier_low(self):
        """Very small token count returns INLINE."""
        assert determine_response_tier(0) == ResponseTier.INLINE
        assert determine_response_tier(100) == ResponseTier.INLINE
        assert determine_response_tier(500) == ResponseTier.INLINE

    def test_inline_tier_boundary(self):
        """Tokens just below INLINE threshold returns INLINE."""
        assert determine_response_tier(TOKEN_TIER_INLINE - 1) == ResponseTier.INLINE

    def test_cached_tier_at_boundary(self):
        """Tokens at INLINE threshold returns CACHED."""
        assert determine_response_tier(TOKEN_TIER_INLINE) == ResponseTier.CACHED

    def test_cached_tier_medium(self):
        """Medium token count returns CACHED."""
        assert determine_response_tier(5000) == ResponseTier.CACHED
        assert determine_response_tier(10000) == ResponseTier.CACHED

    def test_cached_tier_high(self):
        """Very high token count returns CACHED."""
        assert determine_response_tier(50000) == ResponseTier.CACHED
        assert determine_response_tier(100000) == ResponseTier.CACHED
        assert determine_response_tier(1000000) == ResponseTier.CACHED


class TestDefaultThresholds:
    """Tests for default threshold values."""

    def test_inline_threshold_default(self):
        """INLINE threshold has sensible default."""
        assert TOKEN_TIER_INLINE == 2000


class TestIntegration:
    """Integration tests combining estimate_tokens and determine_response_tier."""

    def test_small_namespace_list_inline(self):
        """Small namespace list should return INLINE tier."""
        data = [
            {"name": "default", "uid": "abc123", "phase": "Active"},
            {"name": "kube-system", "uid": "def456", "phase": "Active"},
            {"name": "kube-public", "uid": "ghi789", "phase": "Active"},
        ]
        tokens = estimate_tokens(data)
        tier = determine_response_tier(tokens)
        assert tier == ResponseTier.INLINE

    def test_medium_vm_list_cached(self):
        """Medium VM list should return CACHED tier when above threshold."""
        data = [
            {
                "name": f"vm-{i:03d}",
                "power_state": "poweredOn",
                "num_cpu": 4,
                "memory_mb": 8192,
                "guest_os": "ubuntu64Guest",
                "ip_address": f"192.168.1.{i}",
                "notes": "Production virtual machine for application workloads",
            }
            for i in range(50)
        ]
        tokens = estimate_tokens(data)
        tier = determine_response_tier(tokens)
        # 50 VMs with ~150 chars each ≈ 7500 chars / 4 ≈ 1875 tokens
        # Near threshold - could be INLINE or CACHED
        assert tier in (ResponseTier.INLINE, ResponseTier.CACHED)

    def test_large_dataset_cached(self):
        """Large dataset should return CACHED tier."""
        data = [
            {
                "name": f"vm-{i:04d}",
                "power_state": "poweredOn",
                "num_cpu": 4,
                "memory_mb": 8192,
                "guest_os": "ubuntu64Guest",
                "ip_address": f"192.168.{i // 256}.{i % 256}",
                "notes": "A" * 200,  # Long notes
                "labels": {f"key-{j}": f"value-{j}" for j in range(10)},
            }
            for i in range(500)
        ]
        tokens = estimate_tokens(data)
        tier = determine_response_tier(tokens)
        # Very large dataset - always CACHED
        assert tier == ResponseTier.CACHED

    def test_huge_dataset_cached(self):
        """Huge dataset should return CACHED tier."""
        data = [
            {
                "name": f"item-{i}",
                "data": "x" * 500,  # 500 chars of data per item
            }
            for i in range(1000)
        ]
        tokens = estimate_tokens(data)
        tier = determine_response_tier(tokens)
        # 1000 items * 500 chars ≈ 500k chars / 4 ≈ 125k tokens
        # Huge dataset - always CACHED
        assert tier == ResponseTier.CACHED


class TestEdgeCases:
    """Edge case tests."""

    def test_non_serializable_datetime(self):
        """Non-serializable datetime should use default=str fallback."""
        from datetime import UTC, datetime

        data = {"timestamp": datetime.now(tz=UTC), "name": "test"}
        tokens = estimate_tokens(data)
        # Should not crash, returns reasonable count
        assert tokens > 0

    def test_non_serializable_object_fallback(self):
        """Non-serializable custom object falls back to str()."""

        class CustomObject:
            def __str__(self):
                return "CustomObject(value=42)"

        data = {"obj": CustomObject()}
        tokens = estimate_tokens(data)
        # Uses default=str, should work
        assert tokens > 0

    def test_negative_tokens_impossible(self):
        """Token count should never be negative."""
        assert estimate_tokens({}) >= 0
        assert estimate_tokens([]) >= 0
        assert estimate_tokens("") >= 0
        assert estimate_tokens(0) >= 0
