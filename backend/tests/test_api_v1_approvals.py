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
from types import SimpleNamespace
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

import meho_backplane.audit as _audit_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
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


# ---------------------------------------------------------------------------
# G11.7-T1 (#1401) — resume-target hardening must fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_refuses_when_pinned_target_no_longer_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A soft-deleted / unresolvable resume target is refused, not executed.

    The approval row pins a concrete ``target_id``. If that target is
    soft-deleted (or revoked) between request and approval,
    ``resolve_target_by_id`` returns ``None``. The resume path must
    **fail closed** — return a structured ``denied`` result and never
    call ``dispatch`` — rather than dispatching with ``target=None``,
    which would let a typed handler that derives its connection from
    ``connector_id`` / ``params`` execute the approved privileged write
    outside the original target scope (G11.7-T1 #1401, B1).
    """
    from meho_backplane.api.v1 import approvals as approvals_module

    target_id = uuid.uuid4()
    operator = Operator(
        sub="op-resume-test",
        name="Resume Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    # Minimal stand-in for the ApprovalRequest ORM row: the resume helper
    # reads id / target_id / op_id / connector_id / params / work_ref off it.
    request = SimpleNamespace(
        id=uuid.uuid4(),
        target_id=target_id,
        op_id="vault.kv.put",
        connector_id="vault-1.x",
        params={"path": "secret/x", "value": "s3cr3t"},
        work_ref=None,
    )

    # The pinned target no longer resolves (soft-deleted between request
    # and approval). The resume helper (now in the service layer) imports
    # ``resolve_target_by_id`` lazily from ``targets.resolver``, so patch
    # it at its source module.
    import meho_backplane.targets.resolver as resolver_module

    async def _resolve_none(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(resolver_module, "resolve_target_by_id", _resolve_none)

    # Spy on dispatch — it must NOT be reached on the fail-closed path.
    dispatch_calls: list[dict[str, Any]] = []

    async def _dispatch_spy(**kwargs: Any) -> Any:
        dispatch_calls.append(kwargs)
        raise AssertionError("dispatch must not run when the pinned target is unresolvable")

    import meho_backplane.operations.dispatcher as dispatcher_module

    monkeypatch.setattr(dispatcher_module, "dispatch", _dispatch_spy)

    result = await approvals_module._resume_dispatch_after_approval(
        operator=operator,
        request=request,  # type: ignore[arg-type]
        params={"path": "secret/x", "value": "s3cr3t"},
    )

    assert dispatch_calls == [], "dispatch was called despite an unresolvable target"
    assert result.status == "denied", result
    assert result.op_id == "vault.kv.put"
    assert str(target_id) in (result.error or "")


@pytest.mark.asyncio
async def test_resume_dispatches_when_no_target_was_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant-wide op (``target_id IS NULL``) still re-dispatches normally.

    The fail-closed branch must fire only when a concrete ``target_id``
    was pinned at request time. An approval whose request had no target
    (tenant-wide op) must reach ``dispatch`` with ``target=None`` as
    before — the hardening must not regress that path.
    """
    from meho_backplane.api.v1 import approvals as approvals_module

    operator = Operator(
        sub="op-resume-test",
        name="Resume Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    request = SimpleNamespace(
        id=uuid.uuid4(),
        target_id=None,
        op_id="some.tenant_wide.op",
        connector_id="some-1.x",
        params={"k": "v"},
        work_ref=None,
    )

    seen: dict[str, Any] = {}

    async def _dispatch_spy(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return SimpleNamespace(status="ok", op_id=kwargs["op_id"], result={}, error=None)

    import meho_backplane.operations.dispatcher as dispatcher_module

    monkeypatch.setattr(dispatcher_module, "dispatch", _dispatch_spy)

    result = await approvals_module._resume_dispatch_after_approval(
        operator=operator,
        request=request,  # type: ignore[arg-type]
        params={"k": "v"},
    )

    assert result.status == "ok"
    assert seen["target"] is None
    assert seen["_approved"] is True


# ---------------------------------------------------------------------------
# T6 (#1483) — self_approval_forbidden REST detail must carry the
# APPROVAL_ALLOW_SELF_APPROVAL break-glass hint the exception constructs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path_suffix, body",
    [
        ("approve", {"params": {}}),
        ("decide", {"decision": "approved"}),
    ],
)
def test_self_approval_forbidden_detail_carries_break_glass_hint(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path_suffix: str,
    body: dict[str, Any],
) -> None:
    """403 ``self_approval_forbidden`` detail names ``APPROVAL_ALLOW_SELF_APPROVAL``.

    The service raises :class:`SelfApprovalForbiddenError` whose message
    already names the break-glass flag; both the ``/approve`` and the
    operator-decision ``/decide`` route map it to 403. The wire ``detail``
    must keep the machine-readable ``self_approval_forbidden`` token prefix
    **and** carry the env-var hint so a solo operator sees the escape hatch
    in the response body rather than a bare token (#1483).
    """
    from meho_backplane.api.v1 import approvals as approvals_module
    from meho_backplane.operations.approval_queue import SelfApprovalForbiddenError

    request_id = uuid.UUID("55555555-5555-5555-5555-555555555555")

    async def _raise_self_approval(*_a: object, **_kw: object) -> None:
        raise SelfApprovalForbiddenError(request_id, principal_sub="op-test")

    # Both routes call the module-level ``approve_request``; patch it to
    # raise before any DB row is touched.
    monkeypatch.setattr(approvals_module, "approve_request", _raise_self_approval)

    key = make_rsa_keypair("kid-op")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.OPERATOR)}"}
        response = client.post(
            f"/api/v1/approvals/{request_id}/{path_suffix}",
            headers=headers,
            json=body,
        )

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail.startswith("self_approval_forbidden"), detail
    assert "APPROVAL_ALLOW_SELF_APPROVAL" in detail, detail
