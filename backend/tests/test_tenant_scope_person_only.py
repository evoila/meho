# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A tenant named in a body or a query follows the rules of the tenant header.

* ``authorize_tenant_scope`` (``auth/rbac.py``) lets only a platform admin
  who is a person cross into another tenant. A machine login is refused,
  also when its token carries the platform-admin claim.
* A tenant in the caller's own list is refused on the body and query door:
  the caller selects it with the ``Meho-Tenant`` header, so their own role
  there applies.
* MCP ``list_targets`` gives one fixed error for every ``tenant_id`` a
  caller without platform admin may not use, whether that tenant exists or
  not. A platform admin keeps the lookup and its ``no tenant matches``
  message.
* The settings export without ``tenant_id`` and the settings history cover
  every tenant only for a platform admin who is a person. A machine login
  gets its own tenant.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from meho_backplane.api.v1.checks_dashboards import router as dashboards_router
from meho_backplane.auth.operator import (
    Operator,
    PrincipalKind,
    RoleGroup,
    TenantMembership,
    TenantRole,
)
from meho_backplane.auth.rbac import authorize_tenant_scope
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import InstanceSettingsRevision, Target, Tenant
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.middleware import verify_jwt_and_bind
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_TENANT_A = UUID("0000000a-0000-0000-0000-00000000000a")
_TENANT_B = UUID("0000000b-0000-0000-0000-00000000000b")
_TENANT_C = UUID("0000000c-0000-0000-0000-00000000000c")
_UNKNOWN_TENANT = UUID("0000000d-0000-0000-0000-00000000000d")

_MACHINE_KINDS = [PrincipalKind.AGENT, PrincipalKind.SERVICE, PrincipalKind.RUNNER]


def _operator(
    *,
    platform_admin: bool = False,
    principal_kind: PrincipalKind = PrincipalKind.USER,
    tenant_id: UUID = _TENANT_B,
    tenants: tuple[TenantMembership, ...] = (),
    role_from_platform_admin: bool = False,
) -> Operator:
    return Operator(
        sub="op",
        raw_jwt="t",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=platform_admin,
        principal_kind=principal_kind,
        tenants=tenants,
        role_from_platform_admin=role_from_platform_admin,
    )


def _reader_in_a_admin_in_b(**kwargs: Any) -> Operator:
    """A person who is ``tenant_admin`` in B (current) and ``read_only`` in A."""
    return _operator(
        tenants=(
            TenantMembership(tenant_id=_TENANT_B, name="tenant-b", group=RoleGroup.ADMINS),
            TenantMembership(tenant_id=_TENANT_A, name="tenant-a", group=RoleGroup.READERS),
        ),
        **kwargs,
    )


def _refusal(operator: Operator, requested: UUID) -> str:
    with pytest.raises(HTTPException) as exc:
        authorize_tenant_scope(operator, requested)
    assert exc.value.status_code == 403
    return str(exc.value.detail)


# ---------------------------------------------------------------------------
# authorize_tenant_scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", _MACHINE_KINDS)
def test_machine_login_with_platform_admin_claim_cannot_cross_tenants(
    kind: PrincipalKind,
) -> None:
    operator = _operator(platform_admin=True, principal_kind=kind)
    assert _refusal(operator, _TENANT_C) == "cross_tenant_requires_platform_admin"


@pytest.mark.parametrize("kind", _MACHINE_KINDS)
def test_machine_login_keeps_its_own_tenant(kind: PrincipalKind) -> None:
    operator = _operator(platform_admin=True, principal_kind=kind)
    assert authorize_tenant_scope(operator, None) == _TENANT_B
    assert authorize_tenant_scope(operator, _TENANT_B) == _TENANT_B


def test_person_platform_admin_crosses_into_a_foreign_tenant() -> None:
    operator = _reader_in_a_admin_in_b(platform_admin=True)
    assert authorize_tenant_scope(operator, _TENANT_C) == _TENANT_C


def test_platform_admin_naming_own_lower_role_tenant_is_refused() -> None:
    """Read-only in A: the body door does not lend the role of tenant B."""
    operator = _reader_in_a_admin_in_b(platform_admin=True)
    assert _refusal(operator, _TENANT_A).startswith("tenant_select_with_header:")


def test_platform_admin_working_in_a_foreign_tenant_cannot_name_own_tenant() -> None:
    """In C through the platform admin role; A is still one of their own tenants."""
    operator = _reader_in_a_admin_in_b(
        platform_admin=True, tenant_id=_TENANT_C, role_from_platform_admin=True
    )
    assert _refusal(operator, _TENANT_A).startswith("tenant_select_with_header:")


def test_member_without_platform_admin_is_told_to_use_the_header() -> None:
    operator = _reader_in_a_admin_in_b()
    detail = _refusal(operator, _TENANT_A)
    assert detail.startswith("tenant_select_with_header:")
    assert "Meho-Tenant" in detail


def test_current_tenant_from_the_list_is_allowed() -> None:
    operator = _reader_in_a_admin_in_b(platform_admin=True)
    assert authorize_tenant_scope(operator, _TENANT_B) == _TENANT_B


# ---------------------------------------------------------------------------
# The body and query door on a route (check dashboards)
# ---------------------------------------------------------------------------


def _rest_client(operator: Operator) -> TestClient:
    rest_app = FastAPI()
    rest_app.include_router(dashboards_router)
    rest_app.dependency_overrides[verify_jwt_and_bind] = lambda: operator
    return TestClient(rest_app)


@pytest.mark.parametrize("kind", [PrincipalKind.SERVICE, PrincipalKind.AGENT])
def test_machine_login_body_tenant_id_is_403(kind: PrincipalKind) -> None:
    client = _rest_client(_operator(platform_admin=True, principal_kind=kind))
    response = client.post(
        "/api/v1/checks/dashboards",
        json={"name": "d", "tenant_id": str(_TENANT_C)},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "cross_tenant_requires_platform_admin"


@pytest.mark.parametrize("kind", [PrincipalKind.SERVICE, PrincipalKind.AGENT])
def test_machine_login_query_tenant_filter_is_403(kind: PrincipalKind) -> None:
    client = _rest_client(_operator(platform_admin=True, principal_kind=kind))
    response = client.get(
        "/api/v1/checks/dashboards",
        params={"tenant_filter": str(_TENANT_C)},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "cross_tenant_requires_platform_admin"


def test_platform_admin_read_only_in_tenant_cannot_write_there_by_body() -> None:
    client = _rest_client(_reader_in_a_admin_in_b(platform_admin=True))
    response = client.post(
        "/api/v1/checks/dashboards",
        json={"name": "d", "tenant_id": str(_TENANT_A)},
    )
    assert response.status_code == 403
    assert response.json()["detail"].startswith("tenant_select_with_header:")


# ---------------------------------------------------------------------------
# MCP list_targets
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_client(request: pytest.FixtureRequest) -> Iterator[TestClient]:
    operator: Operator = request.param

    async def _fake_verify() -> Operator:
        return operator

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


async def _seed_tenants_and_target() -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-tenant", name="op-tenant"))
        session.add(Tenant(id=_TENANT_C, slug="other-tenant", name="other-tenant"))
    async with get_sessionmaker()() as session, session.begin():
        for tenant_id, name in ((_TENANT_C, "other-vc"), (OPERATOR_TENANT_ID, "own-vc")):
            session.add(
                Target(
                    tenant_id=tenant_id,
                    name=name,
                    aliases=[],
                    product="vmware",
                    host=f"{name}.example",
                    port=443,
                    secret_ref="targets/tenant-scope",
                    auth_model="shared_service_account",
                )
            )


def _list_targets(client: TestClient, tenant_arg: str) -> Any:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_targets", "arguments": {"tenant_id": tenant_arg}},
        },
    )
    return response.json()


def _target_names(body: Any) -> list[str]:
    assert "error" not in body, body
    payload = json.loads(body["result"]["content"][0]["text"])
    return [t["name"] for t in payload["targets"]]


_OPERATOR = build_operator(TenantRole.OPERATOR)
_SERVICE_PLATFORM_ADMIN = build_operator(TenantRole.TENANT_ADMIN, platform_admin=True).model_copy(
    update={"principal_kind": PrincipalKind.SERVICE}
)
_PERSON_PLATFORM_ADMIN = build_operator(TenantRole.TENANT_ADMIN, platform_admin=True)


@pytest.mark.parametrize("mcp_client", [_OPERATOR, _SERVICE_PLATFORM_ADMIN], indirect=True)
async def test_list_targets_same_error_for_foreign_and_unknown_tenants(
    mcp_client: TestClient,
) -> None:
    await _seed_tenants_and_target()
    bodies = [
        _list_targets(mcp_client, value)
        for value in ("other-tenant", str(_TENANT_C), "no-such-tenant", str(_UNKNOWN_TENANT))
    ]
    errors = [body["error"] for body in bodies]
    assert {e["code"] for e in errors} == {INVALID_PARAMS}
    assert len({e["message"] for e in errors}) == 1
    assert "platform_admin" in errors[0]["message"]
    assert "other-tenant" not in errors[0]["message"]


@pytest.mark.parametrize("mcp_client", [_OPERATOR], indirect=True)
async def test_list_targets_own_tenant_by_slug_or_uuid(mcp_client: TestClient) -> None:
    await _seed_tenants_and_target()
    assert _target_names(_list_targets(mcp_client, "op-tenant")) == ["own-vc"]
    assert _target_names(_list_targets(mcp_client, str(OPERATOR_TENANT_ID))) == ["own-vc"]


@pytest.mark.parametrize("mcp_client", [_PERSON_PLATFORM_ADMIN], indirect=True)
async def test_list_targets_platform_admin_keeps_cross_tenant_list(
    mcp_client: TestClient,
) -> None:
    await _seed_tenants_and_target()
    assert _target_names(_list_targets(mcp_client, "other-tenant")) == ["other-vc"]
    assert _target_names(_list_targets(mcp_client, str(_TENANT_C))) == ["other-vc"]
    unknown = _list_targets(mcp_client, "no-such-tenant")
    assert unknown["error"]["code"] == INVALID_PARAMS
    assert "no tenant matches" in unknown["error"]["message"]


# ---------------------------------------------------------------------------
# REST settings export and history
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_client(request: pytest.FixtureRequest) -> Iterator[TestClient]:
    operator: Operator = request.param
    app.dependency_overrides[verify_jwt_and_bind] = lambda: operator
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_jwt_and_bind, None)


_MACHINE_PLATFORM_ADMINS = [
    build_operator(TenantRole.TENANT_ADMIN, platform_admin=True).model_copy(
        update={"principal_kind": kind}
    )
    for kind in _MACHINE_KINDS
]


def _exported_tenant_ids(client: TestClient) -> list[str]:
    response = client.get("/api/v1/settings/export")
    assert response.status_code == 200, response.text
    return [block["id"] for block in yaml.safe_load(response.text)["spec"]["tenants"]]


async def _seed_revision_of_other_tenant() -> None:
    block = {"id": str(_TENANT_C), "policies": {}, "conventions": [], "targets": []}
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            InstanceSettingsRevision(
                actor_sub="admin-person",
                audit_id=uuid.uuid4(),
                file_sha256="0" * 64,
                api_version="meho.evoila.com/v1",
                exported_from=None,
                spec_before={"tenants": [block]},
                spec_after={"tenants": [block]},
                summary={},
            )
        )


@pytest.mark.parametrize("settings_client", _MACHINE_PLATFORM_ADMINS, indirect=True)
async def test_machine_login_with_platform_admin_claim_exports_its_own_tenant(
    settings_client: TestClient,
) -> None:
    await _seed_tenants_and_target()
    assert _exported_tenant_ids(settings_client) == [str(OPERATOR_TENANT_ID)]


@pytest.mark.parametrize("settings_client", [_PERSON_PLATFORM_ADMIN], indirect=True)
async def test_person_platform_admin_exports_every_tenant(settings_client: TestClient) -> None:
    await _seed_tenants_and_target()
    assert {str(OPERATOR_TENANT_ID), str(_TENANT_C)} <= set(_exported_tenant_ids(settings_client))


@pytest.mark.parametrize("settings_client", _MACHINE_PLATFORM_ADMINS, indirect=True)
async def test_machine_login_with_platform_admin_claim_sees_own_tenant_history(
    settings_client: TestClient,
) -> None:
    await _seed_tenants_and_target()
    await _seed_revision_of_other_tenant()
    response = settings_client.get("/api/v1/settings/history")
    assert response.status_code == 200, response.text
    assert response.json() == []


@pytest.mark.parametrize("settings_client", [_PERSON_PLATFORM_ADMIN], indirect=True)
async def test_person_platform_admin_sees_every_revision(settings_client: TestClient) -> None:
    await _seed_tenants_and_target()
    await _seed_revision_of_other_tenant()
    response = settings_client.get("/api/v1/settings/history")
    assert response.status_code == 200, response.text
    assert [entry["actor_sub"] for entry in response.json()] == ["admin-person"]
