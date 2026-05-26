# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Negative RBAC tests for ``/api/v1/agents/grants*``.

G11.2-T6 (#819) wires every grants route behind
``Depends(require_role(TenantRole.TENANT_ADMIN))``. The existing
service-layer suite (``test_agent_grants.py``) bypasses the gate by
construction — it calls
:class:`~meho_backplane.agents.grants.AgentGrantService` directly. A
refactor that drops ``_require_admin = Depends(require_role(...))``
from the router module would not surface there.

This file closes the gap by exercising every grants route through
the FastAPI :class:`~fastapi.testclient.TestClient` with two
under-privileged JWTs:

* ``read_only`` — the floor of the role lattice; rejected everywhere.
* ``operator`` — one rank below ``tenant_admin``; rejected everywhere
  on this surface because grants are governance data
  (``required_role=TenantRole.TENANT_ADMIN``).

Every route asserts HTTP 403 with detail ``insufficient_role``
(matching :func:`~meho_backplane.auth.rbac.require_role`'s
:class:`fastapi.HTTPException`). Refactors that downgrade the
required role would break these tests immediately.

Out of scope:

* Happy-path tenant-admin coverage — separate task.
* Cross-principal "agent can't grant itself" — independent property,
  separate task per the originating issue body.
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


@pytest.fixture(autouse=True)
def _noop_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence the broadcast publisher so 403 responses don't stall on Valkey.

    :class:`~meho_backplane.audit.AuditMiddleware` calls
    :func:`~meho_backplane.broadcast.publish_event` on every response,
    including 4xx. Without a running Valkey the redis-py client
    blocks on ``socket_connect_timeout``. The middleware's audit row
    is what matters for these tests, not the broadcast fan-out;
    silencing the publisher keeps the wall-clock honest. Mirror of
    ``test_api_v1_agent_principals._noop_broadcast``.
    """

    async def _noop(*_a: object, **_kw: object) -> None:
        pass

    monkeypatch.setattr(_audit_module, "publish_event", _noop)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`~meho_backplane.settings.Settings` requires."""
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


# Every route+method gated by ``_require_admin``. Stored as
# ``(method, path, body)`` so the negative-matrix tests can iterate
# without reproducing the verb table. ``body`` is None for GET /
# DELETE; for POST it's a minimal Pydantic-valid payload — the role
# gate fires before Pydantic parsing on a 403, so the body need only
# be a dict (FastAPI doesn't validate it until the dependency chain
# clears).
#
# Note on ``GET /api/v1/agents/grants`` (list)
# --------------------------------------------
# The agent-definitions router (``api/v1/agents.py``) is registered
# BEFORE the grants router in :mod:`meho_backplane.main`, and its
# ``GET /{name}`` route matches ``/api/v1/agents/grants`` first
# (``name="grants"``). FastAPI route precedence is registration
# order. The shadow is OPERATOR-gated rather than TENANT_ADMIN-gated:
#
# * A ``read_only`` request still surfaces as 403
#   ``insufficient_role`` (from the agents-show gate, not the
#   grants-list gate) — same observable behaviour from outside, so
#   the route is covered for the ``read_only`` case at the bottom
#   of this file via ``test_read_only_list_route_returns_403``.
# * An ``operator`` request passes the agents-show gate and reaches
#   the service, returning 404 ``agent_not_found`` rather than the
#   grants-list 403. The route is excluded from the parametrised
#   ``operator`` matrix below for that reason; the routing-shadow
#   itself is an adjacent finding for the orchestrator to file
#   separately.
_GRANT_ENDPOINTS: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("GET", f"/api/v1/agents/grants/{uuid.uuid4()}", None),
    (
        "POST",
        "/api/v1/agents/grants",
        {
            "principal_sub": "agent:deploy-bot",
            "op_pattern": "vm.list",
            "verdict": "auto-execute",
        },
    ),
    (
        "POST",
        "/api/v1/agents/grants/elevate",
        {
            "principal_sub": "agent:deploy-bot",
            "op_pattern": "vm.power_off",
            "verdict": "needs-approval",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    ),
    ("DELETE", f"/api/v1/agents/grants/{uuid.uuid4()}", None),
)


@pytest.mark.parametrize("method, path, body", _GRANT_ENDPOINTS)
def test_read_only_role_is_rejected_with_insufficient_role(
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """``read_only`` JWT → HTTP 403 ``insufficient_role`` on every grants route.

    The lowest-rank role can never act on a TENANT_ADMIN-gated surface;
    this is the floor of the lattice. Any future refactor that drops
    the role gate from a route would invert the response on this case
    (a 404 / 200 / 422 from a deeper layer rather than 403 here).
    """
    key = make_rsa_keypair("kid-ro")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.READ_ONLY)}"}
        response = client.request(method, path, headers=headers, json=body)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}


@pytest.mark.parametrize("method, path, body", _GRANT_ENDPOINTS)
def test_operator_role_is_rejected_with_insufficient_role(
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """``operator`` JWT → HTTP 403 ``insufficient_role`` on every grants route.

    One rank below ``tenant_admin``. This is the load-bearing assertion
    for the surface: grants show / create / elevate / revoke expose
    permission topology and must remain ``tenant_admin``-only. A
    refactor that lowers the gate to ``operator`` (e.g. to align with
    the approvals surface) trips this test. The list route is covered
    separately below; see the matrix-level docstring for the routing
    shadow that excludes it from this parametrised case.
    """
    key = make_rsa_keypair("kid-op")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.OPERATOR)}"}
        response = client.request(method, path, headers=headers, json=body)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}


def test_read_only_list_route_returns_403(client: TestClient) -> None:
    """``read_only`` ``GET /api/v1/agents/grants`` returns 403 ``insufficient_role``.

    The list route is shadowed by ``GET /api/v1/agents/{name}`` (see
    the matrix-level note), but the shadow's ``_require_operator``
    gate also rejects a ``read_only`` JWT with the same response
    shape. The route is therefore covered for ``read_only`` from
    outside — a refactor that drops the grants-list gate AND the
    agents-show gate would be required to surface a non-403 here.

    This test is the one in this file whose green status does NOT
    prove the grants-list-specific gate is wired; it proves the
    outer-observable behaviour. A separate fix that lands the
    grants router before the agents router in
    :mod:`meho_backplane.main` would let this case fold back into
    the parametrised matrix above.
    """
    key = make_rsa_keypair("kid-ro")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.READ_ONLY)}"}
        response = client.get("/api/v1/agents/grants", headers=headers)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}
