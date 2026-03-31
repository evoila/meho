# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit test fixtures.

Mock-based, no real services required.
Inherits root conftest env setup + ALLOW_MODEL_REQUESTS=False guard.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# ============================================================================
# Phase 80: License key test fixtures
# ============================================================================

# Generate test keypair (fresh per test run, not persisted)
_TEST_PRIVATE_KEY = Ed25519PrivateKey.generate()
_TEST_PUBLIC_KEY = _TEST_PRIVATE_KEY.public_key()


def _create_test_license(
    org: str = "Test Org",
    tier: str = "enterprise",
    features: list[str] | None = None,
    expires_at: str | None = None,
    max_tenants: int | None = None,
    license_id: str = "test-license-001",
) -> str:
    """Create a signed test license key using the test keypair."""
    if features is None:
        features = ["multi_tenancy", "sso", "audit", "group_sessions"]
    payload = {
        "org": org,
        "tier": tier,
        "features": features,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
        "max_tenants": max_tenants,
        "license_id": license_id,
    }
    header = base64.urlsafe_b64encode(
        json.dumps({"typ": "meho-license", "ver": 1}).encode()
    ).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    signing_input = header + b"." + body
    sig = base64.urlsafe_b64encode(_TEST_PRIVATE_KEY.sign(signing_input)).rstrip(b"=")
    return f"{header.decode()}.{body.decode()}.{sig.decode()}"


@pytest.fixture
def test_keypair():
    """Return the Ed25519 test keypair."""
    return _TEST_PRIVATE_KEY, _TEST_PUBLIC_KEY


@pytest.fixture
def test_license_key():
    """Valid enterprise license key (no expiry)."""
    return _create_test_license()


@pytest.fixture
def expired_license_in_grace():
    """License expired 3 days ago (within 30-day grace)."""
    expired = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    return _create_test_license(expires_at=expired)


@pytest.fixture
def expired_license_past_grace():
    """License expired 35 days ago (past 30-day grace)."""
    expired = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
    return _create_test_license(expires_at=expired)


@pytest.fixture
def patch_license_public_key(monkeypatch):
    """Patch the public key in licensing.py to use our test keypair."""
    from meho_app.core import licensing

    raw = _TEST_PUBLIC_KEY.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    test_key_b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    monkeypatch.setattr(licensing, "_PUBLIC_KEY_B64", test_key_b64)
    monkeypatch.setattr(licensing, "_TEST_PUBLIC_KEY_B64", test_key_b64)


@pytest.fixture
def mock_meho_dependencies():
    """
    Create fully-mocked MEHODependencies for unit tests.

    Provides: user_context, connector_repo, knowledge_service, topology_service.
    No real database, Redis, or external service connections.
    """
    deps = MagicMock()
    deps.user_context = MagicMock()
    deps.user_context.tenant_id = "test-tenant"
    deps.user_context.user_id = "test-user"
    deps.connector_repo = MagicMock()
    deps.connector_repo.list_connectors = AsyncMock(return_value=[])
    deps.connector_repo.get_connector = AsyncMock(return_value=None)
    deps.knowledge_service = AsyncMock()
    deps.topology_service = AsyncMock()
    return deps


@pytest.fixture
def mock_connector_list():
    """Standard test connectors spanning different types."""
    return [
        {"id": "k8s-prod", "name": "Production K8s", "connector_type": "kubernetes"},
        {"id": "vmware-dc", "name": "vSphere DC", "connector_type": "vmware"},
        {"id": "prom-main", "name": "Prometheus", "connector_type": "prometheus"},
        {"id": "rest-api", "name": "Custom API", "connector_type": "rest"},
    ]
