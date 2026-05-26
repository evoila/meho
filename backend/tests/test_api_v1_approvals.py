# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Negative RBAC tests for ``/api/v1/approvals*``.

G11.2-T5 (#818) wires every approval route behind
``Depends(require_role(TenantRole.OPERATOR))``. The existing
service-layer suite (``test_approval_queue.py``) bypasses the gate by
calling
:func:`~meho_backplane.operations.approval_queue.approve_request` /
:func:`~meho_backplane.operations.approval_queue.reject_request`
directly. A refactor that drops ``_require_operator = Depends(...)``
from the router module would not surface there.

This file closes the gap by exercising every approval route through
the FastAPI :class:`~fastapi.testclient.TestClient` with a
``read_only`` JWT — the only role strictly below ``operator`` in the
v0.2 lattice. Each route asserts HTTP 403 ``insufficient_role``
(matching :func:`~meho_backplane.auth.rbac.require_role`'s
:class:`fastapi.HTTPException`).

Note on coverage shape
----------------------

Why ``read_only`` and not also ``operator``: ``operator`` is the
required minimum on this surface, so the role gate passes for both
``operator`` and ``tenant_admin``. The negative-only role is
``read_only``. The approvals surface, unlike grants, is one rank
lower in the lattice (operators *should* be able to make approval
decisions; only tenant_admin owns grants). One under-privileged
role is enough to assert the gate is wired.

Out of scope:

* Happy-path approve / reject coverage — separate task; the
  re-dispatch + audit + broadcast plumbing is exercised by the
  service-layer suite.
* Pydantic body validation — covered by the existing surface tests
  via 422 paths; this file's contract is HTTP 403 from the role gate
  alone.
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
    """Silence the broadcast publisher (see ``test_api_v1_agent_grants``)."""

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


# Every approval route+method gated by ``_require_operator``.
# Each entry is ``(method, path, body)``. The role gate fires before
# Pydantic body parsing, so minimal valid bodies suffice for the POST
# routes — the goal is to ensure 403 from the gate, not a 422 from
# body validation.
#
# Fixed UUID literals for path parameters. The role gate fires before
# any DB lookup, so the value need only parse as a UUID; deterministic
# literals keep ``pytest-xdist`` test-collection IDs stable across
# workers (``uuid.uuid4()`` here would re-evaluate per worker and trip
# xdist's collection-determinism check).
_APPROVAL_ID_SHOW = uuid.UUID("11111111-1111-1111-1111-111111111111")
_APPROVAL_ID_APPROVE = uuid.UUID("22222222-2222-2222-2222-222222222222")
_APPROVAL_ID_REJECT = uuid.UUID("33333333-3333-3333-3333-333333333333")
_APPROVAL_ID_DECIDE = uuid.UUID("44444444-4444-4444-4444-444444444444")

_APPROVAL_ENDPOINTS: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("GET", "/api/v1/approvals", None),
    ("GET", f"/api/v1/approvals/{_APPROVAL_ID_SHOW}", None),
    (
        "POST",
        f"/api/v1/approvals/{_APPROVAL_ID_APPROVE}/approve",
        {"params": {}},
    ),
    (
        "POST",
        f"/api/v1/approvals/{_APPROVAL_ID_REJECT}/reject",
        {"reason": ""},
    ),
    (
        "POST",
        f"/api/v1/approvals/{_APPROVAL_ID_DECIDE}/decide",
        {"decision": "approved"},
    ),
)


@pytest.mark.parametrize("method, path, body", _APPROVAL_ENDPOINTS)
def test_read_only_role_is_rejected_with_insufficient_role(
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """``read_only`` JWT → HTTP 403 ``insufficient_role`` on every approval route.

    The role one rank below ``operator``; the only role rejected on
    this surface in the v0.2 lattice. A refactor that lowers the gate
    to ``read_only`` (or drops the gate entirely) would have this case
    return 200 / 404 from a deeper layer, breaking the test.
    """
    key = make_rsa_keypair("kid-ro")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.READ_ONLY)}"}
        response = client.request(method, path, headers=headers, json=body)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}
