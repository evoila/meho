# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for Ed25519 license key generation and verification.

All tests use ephemeral key pairs generated at runtime.
No private key material is ever stored on disk.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


@pytest.fixture
def ephemeral_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Generate a fresh Ed25519 keypair for each test run."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return private_key, public_key


def _b64url_encode(data: bytes) -> str:
    """Base64url-encode with padding stripped (matching licensing.py format)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    """Base64url-decode with padding restored."""
    return base64.urlsafe_b64decode(s + "==")


class TestKeyGeneration:
    """Tests that Ed25519 key generation produces valid keys."""

    def test_generates_valid_ed25519_private_key(
        self, ephemeral_keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
    ) -> None:
        private_key, _ = ephemeral_keypair
        assert isinstance(private_key, Ed25519PrivateKey)

    def test_generates_valid_ed25519_public_key(
        self, ephemeral_keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
    ) -> None:
        _, public_key = ephemeral_keypair
        assert isinstance(public_key, Ed25519PublicKey)

    def test_private_key_raw_bytes_are_32_bytes(
        self, ephemeral_keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
    ) -> None:
        private_key, _ = ephemeral_keypair
        raw = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        assert len(raw) == 32

    def test_public_key_raw_bytes_are_32_bytes(
        self, ephemeral_keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
    ) -> None:
        _, public_key = ephemeral_keypair
        raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        assert len(raw) == 32

    def test_base64url_roundtrip(
        self, ephemeral_keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
    ) -> None:
        _, public_key = ephemeral_keypair
        raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        encoded = _b64url_encode(raw)
        decoded = _b64url_decode(encoded)
        assert decoded == raw


class TestSignVerifyRoundtrip:
    """Tests that sign/verify works with ephemeral keys."""

    def test_sign_and_verify_succeeds(
        self, ephemeral_keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
    ) -> None:
        private_key, public_key = ephemeral_keypair
        message = b"test-license-payload"
        signature = private_key.sign(message)
        # Should not raise
        public_key.verify(signature, message)

    def test_verify_with_wrong_key_fails(self) -> None:
        key1 = Ed25519PrivateKey.generate()
        key2 = Ed25519PrivateKey.generate()
        message = b"test-license-payload"
        signature = key1.sign(message)
        with pytest.raises(Exception):
            key2.public_key().verify(signature, message)

    def test_verify_with_tampered_message_fails(
        self, ephemeral_keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
    ) -> None:
        private_key, public_key = ephemeral_keypair
        message = b"original-payload"
        signature = private_key.sign(message)
        with pytest.raises(Exception):
            public_key.verify(signature, b"tampered-payload")

    def test_license_format_roundtrip(
        self, ephemeral_keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
    ) -> None:
        """Verify the full license key format: header.payload.signature."""
        private_key, public_key = ephemeral_keypair

        header = _b64url_encode(json.dumps({"alg": "Ed25519", "typ": "license"}).encode())
        payload = _b64url_encode(
            json.dumps(
                {
                    "org": "test-org",
                    "tier": "enterprise",
                    "features": ["multi-tenancy"],
                    "issued_at": "2026-01-01T00:00:00Z",
                    "expires_at": "2027-01-01T00:00:00Z",
                    "max_tenants": 10,
                    "license_id": "test-001",
                }
            ).encode()
        )

        signing_input = f"{header}.{payload}".encode()
        signature = private_key.sign(signing_input)
        sig_b64 = _b64url_encode(signature)

        license_key = f"{header}.{payload}.{sig_b64}"

        # Verify by decoding
        parts = license_key.split(".")
        assert len(parts) == 3
        h, p, s = parts
        sig_bytes = _b64url_decode(s)
        public_key.verify(sig_bytes, f"{h}.{p}".encode())

        # Decode payload
        payload_data = json.loads(_b64url_decode(p))
        assert payload_data["org"] == "test-org"
        assert payload_data["tier"] == "enterprise"


class TestProductionKeyLoads:
    """Tests that the production public key in licensing.py is valid."""

    def test_production_public_key_loads(self) -> None:
        """Verify _PUBLIC_KEY_B64 in licensing.py is a valid Ed25519 public key."""
        from meho_app.core.licensing import _PUBLIC_KEY_B64

        assert _PUBLIC_KEY_B64 != "REPLACE_WITH_PRODUCTION_PUBLIC_KEY"
        raw = _b64url_decode(_PUBLIC_KEY_B64)
        key = Ed25519PublicKey.from_public_bytes(raw)
        assert isinstance(key, Ed25519PublicKey)

    def test_test_public_key_loads(self) -> None:
        """Verify _TEST_PUBLIC_KEY_B64 in licensing.py is a valid Ed25519 public key."""
        from meho_app.core.licensing import _TEST_PUBLIC_KEY_B64

        assert _TEST_PUBLIC_KEY_B64 != "REPLACE_WITH_TEST_PUBLIC_KEY"
        raw = _b64url_decode(_TEST_PUBLIC_KEY_B64)
        key = Ed25519PublicKey.from_public_bytes(raw)
        assert isinstance(key, Ed25519PublicKey)

    def test_get_public_key_does_not_raise(self) -> None:
        """Verify _get_public_key() no longer raises ValueError."""
        from meho_app.core.licensing import _get_public_key

        key = _get_public_key()
        assert isinstance(key, Ed25519PublicKey)
