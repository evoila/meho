# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for LicenseService -- Ed25519 license validation and edition gating.

Phase 80: Tests community default, valid enterprise key, invalid key fallback,
grace period logic, API response shape, and feature checks.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meho_app.core.licensing import Edition, LicenseService, _validate_license_key


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


def _sign_payload(payload: dict | list | str | int, private_key: Ed25519PrivateKey) -> str:
    """Sign an arbitrary JSON-serializable payload with the given key (test helper)."""
    header = base64.urlsafe_b64encode(
        json.dumps({"typ": "meho-license", "ver": 1}).encode()
    ).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    signing_input = header + b"." + body
    sig = base64.urlsafe_b64encode(private_key.sign(signing_input)).rstrip(b"=")
    return f"{header.decode()}.{body.decode()}.{sig.decode()}"


def _install_test_keypair(monkeypatch) -> Ed25519PrivateKey:
    """Generate a fresh keypair and patch both license public-key constants.

    Patches `_PUBLIC_KEY_B64` *and* `_TEST_PUBLIC_KEY_B64` so the test result
    does not depend on whether `MEHO_LICENSE_ENV=test` is set in the
    environment (mirrors the `patch_license_public_key` fixture in conftest).
    """
    from meho_app.core import licensing

    priv = Ed25519PrivateKey.generate()
    pub_b64 = (
        base64.urlsafe_b64encode(
            priv.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        )
        .rstrip(b"=")
        .decode()
    )
    monkeypatch.setattr(licensing, "_PUBLIC_KEY_B64", pub_b64)
    monkeypatch.setattr(licensing, "_TEST_PUBLIC_KEY_B64", pub_b64)
    return priv


class TestExceptionHandling:
    """_validate_license_key narrows caught exceptions to the input-malformation family."""

    def test_validation_error_returns_none(self, monkeypatch):
        """Schema mismatch (pydantic ValidationError) returns None, not a propagated error."""
        priv = _install_test_keypair(monkeypatch)
        # Missing org/tier/features/issued_at/license_id -> ValidationError
        token = _sign_payload({"only": "junk"}, priv)
        assert _validate_license_key(token) is None

    def test_non_mapping_payload_returns_none(self, monkeypatch):
        """A valid-JSON-but-non-mapping payload (caught by the isinstance guard) returns None."""
        priv = _install_test_keypair(monkeypatch)
        # JSON list -> isinstance(data, dict) is False -> early return None
        token = _sign_payload([1, 2, 3], priv)
        assert _validate_license_key(token) is None

    def test_unexpected_exception_propagates(self, monkeypatch):
        """A non-validation exception (e.g. RuntimeError) propagates instead of being swallowed."""
        from meho_app.core import licensing

        def boom() -> None:
            msg = "synthetic verifier failure"
            raise RuntimeError(msg)

        monkeypatch.setattr(licensing, "_get_public_key", boom)
        # The token shape is fine; the failure comes from inside _get_public_key.
        with pytest.raises(RuntimeError, match="synthetic verifier failure"):
            _validate_license_key("aGVhZGVy.cGF5bG9hZA.c2ln")
