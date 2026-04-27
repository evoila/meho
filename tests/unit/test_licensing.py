# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for LicenseService -- Ed25519 license validation and edition gating.

Phase 80: Tests community default, valid enterprise key, invalid key fallback,
grace period logic, API response shape, and feature checks.
"""

from __future__ import annotations

import pytest

from meho_app.core.licensing import Edition, LicenseService


class TestCommunityDefault:
    """LicenseService with no key defaults to community mode."""

    def test_community_default(self):
        """No license key -> community edition."""
        svc = LicenseService()
        assert svc.edition == Edition.COMMUNITY
        assert not svc.is_enterprise
        assert svc.org is None
        assert svc.features == frozenset()
        assert svc.expires_at is None
        assert not svc.in_grace_period


class TestValidEnterpriseKey:
    """LicenseService with a valid signed key returns enterprise mode."""

    def test_valid_enterprise_key(self, patch_license_public_key, test_license_key):
        """Valid Ed25519-signed key -> enterprise edition."""
        svc = LicenseService(license_key=test_license_key)
        assert svc.edition == Edition.ENTERPRISE
        assert svc.is_enterprise
        assert svc.org == "Test Org"
        assert "multi_tenancy" in svc.features
        assert "sso" in svc.features
        assert "audit" in svc.features
        assert "group_sessions" in svc.features
        assert not svc.in_grace_period


class TestInvalidKeyFallback:
    """Invalid license key falls back to community mode."""

    def test_invalid_key_fallback(self):
        """Garbage key -> community edition (no crash)."""
        svc = LicenseService(license_key="garbage.invalid.key")
        assert svc.edition == Edition.COMMUNITY
        assert not svc.is_enterprise

    def test_empty_string_key(self):
        """Empty string key -> community edition."""
        svc = LicenseService(license_key="")
        assert svc.edition == Edition.COMMUNITY

    def test_malformed_segments(self):
        """Key with wrong number of segments -> community edition."""
        svc = LicenseService(license_key="only-one-segment")
        assert svc.edition == Edition.COMMUNITY

        svc2 = LicenseService(license_key="two.segments")
        assert svc2.edition == Edition.COMMUNITY


class TestGracePeriod:
    """License expiry with 7-day grace period."""

    def test_expired_in_grace(self, patch_license_public_key, expired_license_in_grace):
        """License expired 3 days ago -> still enterprise, in grace period."""
        svc = LicenseService(license_key=expired_license_in_grace)
        assert svc.edition == Edition.ENTERPRISE
        assert svc.is_enterprise
        assert svc.in_grace_period

    def test_expired_past_grace(self, patch_license_public_key, expired_license_past_grace):
        """License expired 10 days ago -> community mode, grace period ended."""
        svc = LicenseService(license_key=expired_license_past_grace)
        assert svc.edition == Edition.COMMUNITY
        assert not svc.is_enterprise
        assert not svc.in_grace_period


class TestApiResponse:
    """to_api_response() returns correct shape and values."""

    def test_to_api_response_shape(self):
        """API response has exactly the expected keys."""
        svc = LicenseService()
        resp = svc.to_api_response()
        assert set(resp.keys()) == {"edition", "features", "org", "expires_at", "in_grace_period"}

    def test_community_api_response(self):
        """Community response values."""
        svc = LicenseService()
        resp = svc.to_api_response()
        assert resp["edition"] == "community"
        assert resp["features"] == []
        assert resp["org"] is None
        assert resp["expires_at"] is None
        assert resp["in_grace_period"] is False

    def test_enterprise_api_response(self, patch_license_public_key, test_license_key):
        """Enterprise response values."""
        svc = LicenseService(license_key=test_license_key)
        resp = svc.to_api_response()
        assert resp["edition"] == "enterprise"
        assert "multi_tenancy" in resp["features"]
        assert resp["org"] == "Test Org"
        assert resp["in_grace_period"] is False


class TestHasFeature:
    """has_feature() checks enabled features."""

    def test_has_feature(self, patch_license_public_key, test_license_key):
        """Enterprise service reports correct features."""
        svc = LicenseService(license_key=test_license_key)
        assert svc.has_feature("multi_tenancy") is True
        assert svc.has_feature("sso") is True
        assert svc.has_feature("nonexistent") is False

    def test_community_has_no_features(self):
        """Community service has no features."""
        svc = LicenseService()
        assert svc.has_feature("multi_tenancy") is False
        assert svc.has_feature("anything") is False
