# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""REST tests for ``/api/v1/service-principals/grants*`` (#3151).

Two contracts:

* **RBAC** — every route is gated by ``require_role(TenantRole.OPERATOR)``;
  a ``read_only`` JWT gets HTTP 403 ``insufficient_role`` everywhere.
* **Happy path (operator)** — create → list → show → revoke round-trips,
  and the create-time review rejects a delete-shaped op / a wildcard with
  HTTP 422.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

import meho_backplane.audit as _audit_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_BASE = "/api/v1/service-principals/grants"


@pytest.fixture(autouse=True)
def _noop_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence the broadcast publisher so responses don't stall on Valkey."""

    async def _noop(*_a: object, **_kw: object) -> None:
        pass

    monkeypatch.setattr(_audit_module, "publish_event", _noop)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app)


def _token(key: Any, *, role: TenantRole, sub: str = "op-test") -> str:
    return mint_token(key, sub=sub, tenant_role=role.value, tenant_id=str(_TENANT_A))


_GRANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")

_ENDPOINTS: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("GET", _BASE, None),
    ("GET", f"{_BASE}/{_GRANT_ID}", None),
    (
        "POST",
        _BASE,
        {
            "principal_sub": "svc:deploy-bot",
            "op_id": "vmware.composite.vm.create",
            "connector_id": "vmware-rest-9.0",
            "reason": "unattended build",
        },
    ),
    ("DELETE", f"{_BASE}/{_GRANT_ID}", None),
)


@pytest.mark.parametrize("method, path, body", _ENDPOINTS)
def test_read_only_role_is_rejected(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """``read_only`` JWT → HTTP 403 ``insufficient_role`` on every route."""
    key = make_rsa_keypair("kid-ro")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.READ_ONLY)}"}
        response = client.request(method, path, headers=headers, json=body)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}


def test_operator_crud_round_trip(client: TestClient) -> None:
    """create (201) → list → show → revoke (204) → show-revoked → re-revoke (404)."""
    key = make_rsa_keypair("kid-op")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.OPERATOR)}"}
        body = {
            "principal_sub": "svc:deploy-bot",
            "op_id": "vmware.composite.vm.power",
            "connector_id": "vmware-rest-9.0",
            "reason": "unattended power-on for the build",
        }
        created = client.post(_BASE, headers=headers, json=body)
        assert created.status_code == 201, created.text
        grant_id = created.json()["id"]
        assert created.json()["reason"] == body["reason"]
        assert created.json()["revoked_at"] is None

        listed = client.get(_BASE, headers=headers, params={"principal_sub": "svc:deploy-bot"})
        assert listed.status_code == 200
        assert grant_id in {g["id"] for g in listed.json()["grants"]}

        shown = client.get(f"{_BASE}/{grant_id}", headers=headers)
        assert shown.status_code == 200
        assert shown.json()["op_id"] == "vmware.composite.vm.power"

        revoked = client.delete(f"{_BASE}/{grant_id}", headers=headers)
        assert revoked.status_code == 204

        after = client.get(f"{_BASE}/{grant_id}", headers=headers)
        assert after.status_code == 200
        assert after.json()["revoked_at"] is not None

        # Re-revoking a soft-deleted grant is a 404 (no live row matched).
        re_revoke = client.delete(f"{_BASE}/{grant_id}", headers=headers)
        assert re_revoke.status_code == 404


@pytest.mark.parametrize(
    "op_id, expect_fragment",
    [
        ("DELETE:/vcenter/vm/{vm}", "delete-shaped"),
        ("vmware.composite.vm.*", "wildcard"),
    ],
)
def test_create_review_rejects_bad_op(client: TestClient, op_id: str, expect_fragment: str) -> None:
    """A delete-shaped op / a wildcard op_id is refused with HTTP 422."""
    key = make_rsa_keypair("kid-op2")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.OPERATOR)}"}
        body = {
            "principal_sub": "svc:deploy-bot",
            "op_id": op_id,
            "connector_id": "vmware-rest-9.0",
            "reason": "should be rejected",
        }
        response = client.post(_BASE, headers=headers, json=body)
    assert response.status_code == 422, response.text
    assert expect_fragment in response.json()["detail"]
