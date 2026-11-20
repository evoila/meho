# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reading the admins of a tenant from the sign-in realm.

:func:`tenant_admin_ids` counts the admins of a tenant against a small fake
Keycloak (respx). A count MEHO is not sure of is ``None``. The answer to a
change of rights that waits for approval carries no extra note, and
``/api/v1/health`` has no admin count.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.api.v1.health import VaultStatus, build_health_response
from meho_backplane.auth import tenant_admins
from meho_backplane.auth.approval_sign_in_flow import ApprovalSignInStatus
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.tenant_admins import (
    tenant_admin_ids,
)
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatcher, grant_approval
from meho_backplane.settings import get_settings
from tests.test_api_v1_approvals_fresh_sign_in import _settings_env  # noqa: F401 -- autouse

_KC = "https://keycloak.test"
_ADMIN = f"{_KC}/admin/realms/meho"
_TOKEN = f"{_KC}/realms/meho/protocol/openid-connect/token"
_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006641")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006642")
_ADMIN_A = "user-admin-a"
_ADMIN_B = "user-admin-b"
_RIGHTS_OP = "demo.rights.change"


@pytest.fixture(autouse=True)
def _admin_client_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", _ADMIN)
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "made-up-admin-secret")
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"{_KC}/realms/meho")
    monkeypatch.delenv("KEYCLOAK_DISCOVERY_URL", raising=False)
    monkeypatch.delenv("JWT_PLATFORM_ADMIN_ROLE_NAME", raising=False)
    get_settings.cache_clear()
    tenant_admins._kept.clear()
    yield
    get_settings.cache_clear()
    tenant_admins._kept.clear()


def _group(
    group_id: str, attributes: dict[str, list[str]] | None = None, *, children: int = 0
) -> dict[str, Any]:
    return {
        "id": group_id,
        "name": group_id,
        "attributes": attributes or {},
        "subGroupCount": children,
    }


def _admins_group(tenant: uuid.UUID = _TENANT) -> dict[str, Any]:
    """The first tenant's MEHO-Admins group, as the realm file makes it."""
    return _group(
        "g-admins",
        {
            "tenant_id": [str(tenant)],
            "tenant_role": ["tenant_admin"],
            "meho_tenant": [f"{tenant}:main:Admins"],
        },
    )


def _operators_group() -> dict[str, Any]:
    return _group(
        "g-operators",
        {
            "tenant_id": [str(_TENANT)],
            "tenant_role": ["operator"],
            "meho_tenant": [f"{_TENANT}:main:Operators"],
        },
    )


def _platform_group() -> dict[str, Any]:
    return _group("g-platform", {"platform_admin": ["true"]})


def _user(user_id: str, *, enabled: bool = True) -> dict[str, Any]:
    return {"id": user_id, "username": user_id, "enabled": enabled}


def _mount(
    mock: respx.MockRouter,
    groups: list[dict[str, Any]] | httpx.Response,
    members: dict[str, list[dict[str, Any]]],
    children: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    mock.post(_TOKEN).respond(200, json={"access_token": "made-up-token"})
    if isinstance(groups, httpx.Response):
        mock.get(f"{_ADMIN}/groups").mock(return_value=groups)
    else:
        mock.get(f"{_ADMIN}/groups").respond(200, json=groups)
    for group_id, rows in (children or {}).items():
        mock.get(f"{_ADMIN}/groups/{group_id}/children").respond(200, json=rows)
    for group_id, rows in members.items():
        mock.get(f"{_ADMIN}/groups/{group_id}/members").respond(200, json=rows)


async def _count(tenant: uuid.UUID) -> int | None:
    ids = await tenant_admin_ids(tenant)
    return None if ids is None else len(ids)


# --- counting --------------------------------------------------------------------------


async def test_one_admin_in_the_admin_group() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(
            mock,
            [_admins_group(), _operators_group()],
            {"g-admins": [_user(_ADMIN_A)], "g-operators": [_user("user-op")]},
        )
        assert await tenant_admin_ids(_TENANT) == frozenset({_ADMIN_A})


async def test_platform_admins_count_and_one_person_in_both_groups_counts_once() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(
            mock,
            [_admins_group(), _platform_group()],
            {"g-admins": [_user(_ADMIN_A)], "g-platform": [_user(_ADMIN_A), _user(_ADMIN_B)]},
        )
        assert await _count(_TENANT) == 2


async def test_another_tenants_admins_do_not_count() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(
            mock,
            [_admins_group(), _admins_group(_OTHER_TENANT) | {"id": "g-other"}],
            {"g-admins": [_user(_ADMIN_A)], "g-other": [_user(_ADMIN_B)]},
        )
        assert await _count(_TENANT) == 1


async def test_an_extra_tenant_group_with_only_meho_tenant_counts() -> None:
    extra = _group("g-extra", {"meho_tenant": [f"{_TENANT}:main:Admins"]})
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [extra], {"g-extra": [_user(_ADMIN_A), _user(_ADMIN_B)]})
        assert await _count(_TENANT) == 2


async def test_a_child_group_takes_the_attributes_of_its_parent() -> None:
    parent = _admins_group() | {"subGroupCount": 1}
    child = _group("g-child", {"description": ["company directory"]})
    with respx.mock(assert_all_called=False) as mock:
        _mount(
            mock,
            [parent],
            {"g-admins": [_user(_ADMIN_A)], "g-child": [_user(_ADMIN_B)]},
            children={"g-admins": [child]},
        )
        assert await _count(_TENANT) == 2


async def test_a_disabled_person_does_not_count() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(
            mock,
            [_admins_group()],
            {"g-admins": [_user(_ADMIN_A), _user(_ADMIN_B, enabled=False)]},
        )
        assert await _count(_TENANT) == 1


async def test_a_service_account_in_the_admin_group_does_not_count() -> None:
    """Only a person is an admin here, so a client login is not counted."""
    robot = {"id": "user-robot", "username": "service-account-meho-agent", "enabled": True}
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_admins_group()], {"g-admins": [_user(_ADMIN_A), robot]})
        assert await _count(_TENANT) == 1


async def test_the_service_account_prefix_follows_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SERVICE_ACCOUNT_USERNAME_PREFIX", "robot-")
    get_settings.cache_clear()
    robot = {"id": "user-robot", "username": "robot-meho-agent", "enabled": True}
    with respx.mock(assert_all_called=False) as mock:
        _mount(
            mock,
            [_admins_group()],
            {"g-admins": [_user(_ADMIN_A), _user("service-account-is-a-person"), robot]},
        )
        assert await _count(_TENANT) == 2


async def test_members_are_read_page_by_page() -> None:
    first = [_user(f"user-{n}") for n in range(tenant_admins._PAGE_SIZE)]
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_admins_group()], {})
        route = mock.get(f"{_ADMIN}/groups/g-admins/members")
        route.side_effect = [
            httpx.Response(200, json=first),
            httpx.Response(200, json=[_user("user-last")]),
        ]
        assert await _count(_TENANT) == tenant_admins._PAGE_SIZE + 1
    assert route.calls[1].request.url.params["first"] == str(tenant_admins._PAGE_SIZE)


@pytest.mark.parametrize(
    "groups",
    [httpx.Response(403), httpx.Response(500), httpx.Response(200, json={"odd": "body"})],
    ids=["refused", "server error", "not a list"],
)
async def test_a_failed_group_read_is_not_known(groups: httpx.Response) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, groups, {})
        assert await tenant_admin_ids(_TENANT) is None


async def test_a_failed_member_read_is_not_known() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_admins_group()], {})
        mock.get(f"{_ADMIN}/groups/g-admins/members").mock(
            side_effect=httpx.ConnectError("refused")
        )
        assert await tenant_admin_ids(_TENANT) is None


async def test_a_failed_admin_login_is_not_known() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_TOKEN).respond(401)
        assert await tenant_admin_ids(_TENANT) is None


async def test_no_admin_group_or_no_member_is_not_known() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_operators_group()], {"g-operators": [_user("user-op")]})
        assert await tenant_admin_ids(_TENANT) is None
    tenant_admins._kept.clear()
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_admins_group()], {"g-admins": []})
        assert await tenant_admin_ids(_TENANT) is None


async def test_no_admin_client_is_not_known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYCLOAK_ADMIN_URL")
    get_settings.cache_clear()
    assert await tenant_admin_ids(_TENANT) is None


async def test_platform_admins_from_a_realm_role_are_not_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_PLATFORM_ADMIN_ROLE_NAME", "platform-admin")
    get_settings.cache_clear()
    assert await tenant_admin_ids(_TENANT) is None


async def test_the_count_is_kept_and_not_known_only_briefly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_admins_group()], {"g-admins": [_user(_ADMIN_A)]})
        assert await _count(_TENANT) == 1
        _mount(mock, [_admins_group()], {"g-admins": [_user(_ADMIN_A), _user(_ADMIN_B)]})
        assert await _count(_TENANT) == 1
    key = (get_settings().keycloak_admin_url, _TENANT)
    tenant_admins._kept[key] = (time.monotonic() - tenant_admins._KEEP_FAILED_S - 1, None)
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_admins_group()], {"g-admins": [_user(_ADMIN_A), _user(_ADMIN_B)]})
        assert await _count(_TENANT) == 2


# --- the answer at request time -----------------------------------------------------------


def _descriptor() -> EndpointDescriptor:
    return EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product="demo",
        version="1",
        impl_id="demo",
        op_id=_RIGHTS_OP,
        source_kind="typed",
        handler_ref=None,
        summary="Give a person a role.",
        description="A made-up change of rights.",
        tags=[],
        parameter_schema={"type": "object"},
        response_schema=None,
        llm_instructions=None,
        safety_level="dangerous",
        requires_approval=True,
        is_enabled=True,
    )


async def _ask(monkeypatch: pytest.MonkeyPatch, op_id: str = _RIGHTS_OP) -> Any:
    monkeypatch.setattr(grant_approval, "_RIGHTS_CHANGE_OPS", {_RIGHTS_OP})
    operator = Operator(
        sub=_ADMIN_A,
        raw_jwt="<jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )
    descriptor = _descriptor()
    descriptor.op_id = op_id
    return await dispatcher._handle_needs_approval(
        op_id=op_id,
        connector_id="demo-1",
        operator=operator,
        descriptor=descriptor,
        target=None,
        params={"user": "someone"},
        params_hash="hash",
        duration_ms=1.0,
    )


async def test_a_change_of_rights_waits_with_the_plain_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _ask(monkeypatch)
    assert result.status == "awaiting_approval"
    assert result.error == f"awaiting_approval: {_RIGHTS_OP!r} requires approval before execution"
    assert "approval_note" not in result.extras
    assert result.extras["approval_request_id"]


# --- /api/v1/health -------------------------------------------------------------------


async def test_health_approvals_block_has_no_admin_count(monkeypatch: pytest.MonkeyPatch) -> None:
    async def vault(*_args: object, **_kwargs: object) -> VaultStatus:
        return VaultStatus(reachable=True, read_ok=True, detail=None)

    async def sign_in() -> ApprovalSignInStatus:
        return ApprovalSignInStatus(state="ok", client="meho-cli-approve")

    monkeypatch.setattr("meho_backplane.api.v1.health._probe_federation", vault)
    monkeypatch.setattr("meho_backplane.api.v1.health.read_approval_sign_in", sign_in)
    operator = Operator(
        sub="op-health",
        raw_jwt="<jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    response = await build_health_response(operator)
    assert response.approvals is not None
    assert "rights_change_admins" not in response.approvals.model_dump()
    assert "rights_change_admins" not in response.model_dump_json()
