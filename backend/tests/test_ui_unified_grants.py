# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Request-level coverage for the unified grants BFF (#3533)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import tests.test_ui_agent_grants as agent_grants_test
from meho_backplane.agents.grants import AgentGrantService
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, ServicePrincipalGrant
from tests.test_ui_agent_grants import (
    _OP_A,
    _TENANT_A,
    _TENANT_B,
    _admin_token,
    _authenticated_client,
    _csrf_headers,
    _future_iso,
    _make_keypair_and_jwks,
    _operator_token,
    _register_named_principal,
    _register_principal,
    _seed_grant,
    _seed_session_sync,
    _seed_tenant,
)


@pytest.fixture(autouse=True)
def _unified_grants_bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reuse the BFF chassis reset only for this module's tests.

    Loading ``test_ui_agent_grants`` as ``pytest_plugins`` would register its
    autouse fixture for every collected test module.  Calling the wrapped
    fixture here retains the established setup while containing that scope.
    """
    yield from agent_grants_test._bff_env.__wrapped__(monkeypatch)


def _seed_service(
    tenant_id: uuid.UUID,
    *,
    principal_sub: str = "service:inventory",
    op_id: str = "POST:/vcenter/vm?action=start",
    created_at: datetime | None = None,
) -> uuid.UUID:
    grant_id = uuid.uuid4()

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            row = ServicePrincipalGrant(
                id=grant_id,
                tenant_id=tenant_id,
                principal_sub=principal_sub,
                op_id=op_id,
                connector_id="vmware-rest-9.0",
                target_id=None,
                target_product="vmware",
                target_name_pattern="esx-*",
                reason="inventory automation",
                created_by_sub=_OP_A,
            )
            if created_at is not None:
                row.created_at = created_at
            session.add(row)

    asyncio.run(_do())
    return grant_id


def _admin_client(*, csrf: bool = False):
    keypair, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=_admin_token(keypair), operator_sub=_OP_A
    )
    return _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=csrf)


def test_admin_lists_both_grant_kinds_with_agent_name_and_scope() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_named_principal(_TENANT_A, "agent:recon", "Recon Scout")
    _seed_grant(
        tenant_id=_TENANT_A, principal_sub="agent:recon", op_pattern="vault.kv.*", target_scope="*"
    )
    _seed_service(_TENANT_A)
    client, mock, _ = _admin_client()
    try:
        response = client.get("/ui/grants")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "Recon Scout" in response.text
    assert "vault.kv.*" in response.text
    assert ">*<" in response.text
    assert "service:inventory" in response.text
    assert "POST:/vcenter/vm?action=start" in response.text
    assert "vmware, esx-*" in response.text
    assert "Issue grant" in response.text
    # The legacy elevation endpoint returns a modal fragment; navigation to it
    # would replace the full console shell, so it must remain an HTMX modal.
    assert 'hx-get="/ui/agents/grants/elevate"' in response.text
    assert 'hx-target="#grants-modal-container"' in response.text
    # HTMX reports a 422 at the target; the stable target owns the scoped
    # before-swap override so validation replaces the modal rather than the
    # whole page.
    assert 'id="grants-modal-container"' in response.text
    assert "hx-on::before-swap" in response.text


def test_operator_sees_service_only_and_never_calls_agent_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_service(_TENANT_A)
    _seed_grant(tenant_id=_TENANT_A, op_pattern="must-not-leak.*")

    async def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("operator listing must not invoke AgentGrantService")

    monkeypatch.setattr(AgentGrantService, "list_", forbidden)
    keypair, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=_operator_token(keypair), operator_sub=_OP_A
    )
    client, mock, _ = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/grants")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "service:inventory" in response.text
    assert "must-not-leak.*" not in response.text
    assert "Read-only access" in response.text
    assert "Issue grant" not in response.text


def test_write_routes_require_admin_before_create_or_revoke() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    grant_id = _seed_service(_TENANT_A)
    keypair, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=_operator_token(keypair), operator_sub=_OP_A
    )
    client, mock, token = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        create = client.post(
            "/ui/grants/create", data={"kind": "service"}, headers=_csrf_headers(token)
        )
        revoke = client.post(f"/ui/grants/service/{grant_id}/revoke", headers=_csrf_headers(token))
    finally:
        mock.stop()
    assert create.status_code == revoke.status_code == 403


def test_create_service_and_agent_use_real_scope_validation() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_principal(_TENANT_A, "agent:new")
    client, mock, token = _admin_client(csrf=True)
    try:
        service = client.post(
            "/ui/grants/create",
            data={
                "kind": "service",
                "principal_sub": "service:new",
                "op": "POST:/vcenter/vm?action=start",
                "connector_id": "vmware-rest-9.0",
                "target_id": "",
                "target_product": "vmware",
                "target_name_pattern": "esx-*",
                "reason": "needed for inventory",
                "expires_at": "",
            },
            headers=_csrf_headers(token),
        )
        agent = client.post(
            "/ui/grants/create",
            data={
                "kind": "agent",
                "principal_sub": "agent:new",
                "op": "vault.kv.*",
                "target_scope": "*",
                "verdict": "needs-approval",
                "expires_at": _future_iso(),
            },
            headers=_csrf_headers(token),
        )
    finally:
        mock.stop()
    assert service.status_code == agent.status_code == 204

    async def _check() -> tuple[ServicePrincipalGrant, AgentPermission | None]:
        async with get_sessionmaker()() as session:
            service_row = (
                await session.execute(
                    select(ServicePrincipalGrant).where(
                        ServicePrincipalGrant.principal_sub == "service:new"
                    )
                )
            ).scalar_one()
            agent_row = (
                await session.execute(
                    select(AgentPermission).where(AgentPermission.principal_sub == "agent:new")
                )
            ).scalar_one()
            return service_row, agent_row

    service_row, agent_row = asyncio.run(_check())
    assert (service_row.target_id, service_row.target_product, service_row.target_name_pattern) == (
        None,
        "vmware",
        "esx-*",
    )
    assert agent_row.target_scope == "*"


def test_csrf_escape_and_cross_tenant_revoke_boundary() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    other = _seed_service(_TENANT_B)
    client, mock, token = _admin_client(csrf=True)
    try:
        missing_csrf = client.post("/ui/grants/create", data={"kind": "service"})
        escaped = client.post(
            "/ui/grants/create",
            data={
                "kind": "service",
                "principal_sub": "service:retained",
                "op": "POST:/x",
                "connector_id": "connector-1",
                "target_id": "<script>alert(1)</script>",
                "reason": "ok",
            },
            headers=_csrf_headers(token),
        )
        cross_tenant = client.post(
            f"/ui/grants/service/{other}/revoke", headers=_csrf_headers(token)
        )
    finally:
        mock.stop()
    assert missing_csrf.status_code == 403
    assert escaped.status_code == 422, escaped.text
    assert "<script>" not in escaped.text
    assert "&lt;script&gt;" in escaped.text
    # Inline validation must keep the operator in the service form with their
    # non-sensitive inputs intact, rather than replacing it with a bare alert.
    assert 'name="principal_sub"' in escaped.text
    assert 'value="service:retained"' in escaped.text
    assert 'name="kind"' in escaped.text
    assert 'value="service" selected' in escaped.text
    assert cross_tenant.status_code == 404


def test_admin_revoke_roundtrip_changes_both_grant_kinds() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    service_id = _seed_service(_TENANT_A)
    agent_id = _seed_grant(tenant_id=_TENANT_A, principal_sub="agent:revoke")
    client, mock, token = _admin_client(csrf=True)
    try:
        service = client.post(
            f"/ui/grants/service/{service_id}/revoke", headers=_csrf_headers(token)
        )
        agent = client.post(f"/ui/grants/agent/{agent_id}/revoke", headers=_csrf_headers(token))
    finally:
        mock.stop()
    assert service.status_code == agent.status_code == 204

    async def _check() -> tuple[ServicePrincipalGrant, AgentPermission]:
        async with get_sessionmaker()() as session:
            service_row = await session.get(ServicePrincipalGrant, service_id)
            agent_row = await session.get(AgentPermission, agent_id)
            assert service_row is not None
            return service_row, agent_row

    service_row, agent_row = asyncio.run(_check())
    assert service_row.revoked_at is not None and service_row.revoked_by_sub == _OP_A
    # AgentGrantService has established hard-delete semantics; the BFF must
    # preserve it while service grant revocation is intentionally historical.
    assert agent_row is None


def test_invalid_kind_is_normalized_before_re_rendering_modal() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, token = _admin_client(csrf=True)
    try:
        response = client.post(
            "/ui/grants/create",
            data={"kind": "</script><script>alert(1)</script>", "principal_sub": "service:x"},
            headers=_csrf_headers(token),
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert "<script>" not in response.text
    # A value later embedded by Alpine must be one of the two server-owned
    # kinds, never attacker-controlled text in an x-data JavaScript literal.
    assert "x-data=\"{ kind: 'agent' }\"" in response.text
    assert '<option value="agent" selected>' in response.text


def test_independent_offsets_filter_and_limit_are_enforced() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_named_principal(_TENANT_A, "shared", "Shared")
    now = datetime.now(UTC)
    _seed_service(_TENANT_A, principal_sub="shared", op_id="new-service", created_at=now)
    _seed_service(
        _TENANT_A,
        principal_sub="shared",
        op_id="old-service",
        created_at=now - timedelta(minutes=1),
    )
    _seed_grant(
        tenant_id=_TENANT_A, principal_sub="shared", op_pattern="new-agent", expires_at=None
    )
    _seed_grant(
        tenant_id=_TENANT_A, principal_sub="shared", op_pattern="old-agent", expires_at=None
    )
    client, mock, _ = _admin_client()
    try:
        page = client.get("/ui/grants?principal=shared&service_offset=1&agent_offset=0&limit=1")
        invalid_limit = client.get("/ui/grants?limit=501")
    finally:
        mock.stop()
    assert page.status_code == 200, page.text
    assert "old-service" in page.text and "new-service" not in page.text
    assert ("new-agent" in page.text) != ("old-agent" in page.text)
    # Each pager retains the shared filter and the other table's cursor.
    assert "service_offset=0" in page.text
    assert "agent_offset=1" in page.text
    assert "principal=shared" in page.text
    assert "limit=1" in page.text
    assert invalid_limit.status_code == 422
