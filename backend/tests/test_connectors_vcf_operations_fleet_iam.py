# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the fleet role operations of the VCF Operations connector (9.1+).

Checks, without a network call (respx, hand-written answers with made-up ids):

* the version check: VCF Operations 9.0 stops before any vendor call with the
  version message; 9.1.0, 9.1.1 and an unknown version go on;
* ``vrops.fleet.role.list`` reads every role page and the SSO realms, and
  returns flat rows;
* ``vrops.fleet.principal.find`` sends the user and group queries, reads one
  principal's assignments, and reports an unknown realm, user or group;
* ``vrops.fleet.role.assign`` keeps the other roles (the vendor PUT replaces
  them all), uses DELETE when no role is left, refuses an unknown role, and
  refuses to remove the last VCF Administrator; a vendor error reaches the
  caller;
* the metadata: reads are safe, the change waits for approval, and only the
  9.x registrar registers the operations;
* through the dispatcher: the change waits for approval, and 9.0 answers
  ``connector_unsupported`` with the version message.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vcf_auth import VcfTargetLike
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_operations import VROPS_TYPED_OPS, VcfOperationsConnector
from meho_backplane.connectors.vcf_operations.fleet_iam_handlers import (
    LastFleetAdministratorError,
    merge_role_assignments,
)
from meho_backplane.connectors.vcf_operations.fleet_iam_ops import (
    VROPS_FLEET_IAM_OPS,
    VROPS_FLEET_PRINCIPAL_FIND_OP,
    VROPS_FLEET_ROLE_ASSIGN_OP,
)
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._validate import validate_params
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_HOST = "vrops-fleet-iam.test.invalid"
_BASE_URL = f"https://{_HOST}"
_ACQUIRE_PATH = "/suite-api/api/auth/token/acquire"
_OPS_TOKEN = "vrops-fleet-iam-token"
_IAM = "/suite-api/api/fleet-management/iam"
_REALM = "11111111-1111-4111-8111-111111111111"
_USER = "22222222-2222-4222-8222-222222222222"
_OTHER_USER = "33333333-3333-4333-8333-333333333333"
_GROUP = "44444444-4444-4444-8444-444444444444"
_PRINCIPAL_ROLES = f"{_IAM}/ssorealms/{_REALM}/principals/{_USER}/roles"
_USERS_QUERY = f"{_IAM}/ssorealms/{_REALM}/users/query"
_GROUPS_QUERY = f"{_IAM}/ssorealms/{_REALM}/groups/query"

_REALMS_ANSWER = {
    "ssoRealms": [
        {
            "id": _REALM,
            "name": "<realm-name>",
            "vidbResourceId": "vidb-1",
            "vcfInstanceId": "instance-1",
            "issues": [],
            "ssoRealmCoverage": [
                {"id": "instance-1", "name": "<instance-name>", "ssoEntityType": "VCF_INSTANCE"}
            ],
            "totalConfiguredComponents": 4,
        }
    ]
}
_ROLES_ANSWER = {
    "vcfRoles": [
        {
            "roleName": "vcf_administrator",
            "roleDisplayName": "VCF Administrator",
            "type": "BUILT_IN",
            "componentRoles": [{"componentType": "VCENTER", "roles": ["Admin"]}],
        },
        {"roleName": "vcf_viewer", "roleDisplayName": "VCF Viewer", "type": "BUILT_IN"},
        {"roleName": "custom_ops", "roleDisplayName": "Custom Ops", "type": "CUSTOM"},
    ],
    "pageInfo": {"page": 0, "pageSize": 100, "totalCount": 3},
}


def _assignment(role: str, scope: str = "SSO_REALM", ids: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "roleName": role,
        "roleDisplayName": role.upper(),
        "roleScope": {"scopeType": scope, "resources": [{"id": i} for i in ids]},
    }


@dataclass
class _StubTarget:
    name: str = "vrops-fleet-iam"
    host: str = _HOST
    port: int | None = 443
    secret_ref: str = "vrops/vrops-fleet-iam"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    auth_source: str | None = None
    tls_server_name: str | None = None
    version: str | None = "9.1.0"
    id: UUID = field(default_factory=uuid.uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


async def _stub_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "admin", "password": "stub-password"}


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="op.test.jwt",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


def _mount_basics(mock: respx.MockRouter) -> None:
    mock.post(_ACQUIRE_PATH).respond(200, json={"token": _OPS_TOKEN})
    mock.get(f"{_IAM}/ssorealms").respond(200, json=_REALMS_ANSWER)
    mock.get(f"{_IAM}/roles").respond(200, json=_ROLES_ANSWER)


async def _run(handler: str, params: dict[str, Any], **target: Any) -> Any:
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    try:
        return await getattr(connector, handler)(_operator(), _StubTarget(**target), params)
    finally:
        await connector.aclose()


# ---------------------------------------------------------------------------
# Version check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "params"),
    [
        ("fleet_role_list", {}),
        ("fleet_principal_find", {"ssoRealmId": _REALM, "principalType": "USER"}),
        (
            "fleet_role_assign",
            {"ssoRealmId": _REALM, "principalId": _USER, "remove": ["vcf_viewer"]},
        ),
    ],
)
@pytest.mark.parametrize("version", ["9.0.0", "9.0.2.0", "VCF Operations 9.0.1"])
@pytest.mark.asyncio
async def test_9_0_stops_with_the_version_message_before_any_call(
    handler: str, params: dict[str, Any], version: str
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_mocked=True) as mock:
        with pytest.raises(NotImplementedError, match=r"needs VCF Operations 9\.1 or newer"):
            await _run(handler, params, version=version)
    assert not mock.calls


@pytest.mark.parametrize("version", ["9.1.0", "9.1.1.0", None])
@pytest.mark.asyncio
async def test_role_list_runs_on_9_1_and_on_an_unknown_version(version: str | None) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        result = await _run("fleet_role_list", {}, version=version)
    assert [r["roleName"] for r in result["roles"]] == [
        "vcf_administrator",
        "vcf_viewer",
        "custom_ops",
    ]


# ---------------------------------------------------------------------------
# vrops.fleet.role.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_role_list_reads_every_page_and_returns_flat_rows() -> None:
    first = {
        "vcfRoles": [{"roleName": f"r{i}"} for i in range(100)],
        "pageInfo": {"totalCount": 101},
    }
    second = {"vcfRoles": [{"roleName": "r100"}], "pageInfo": {"totalCount": 101}}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": _OPS_TOKEN})
        roles = mock.get(f"{_IAM}/roles").mock(
            side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
        )
        mock.get(f"{_IAM}/ssorealms").respond(200, json=_REALMS_ANSWER)
        result = await _run("fleet_role_list", {"type": "CUSTOM"})

    assert len(result["roles"]) == 101
    assert [dict(c.request.url.params) for c in roles.calls] == [
        {"page": "0", "pageSize": "100", "type": "CUSTOM"},
        {"page": "1", "pageSize": "100", "type": "CUSTOM"},
    ]
    assert result["ssoRealms"] == [
        {
            "id": _REALM,
            "name": "<realm-name>",
            "vcfInstanceId": "instance-1",
            "idpId": None,
            "totalConfiguredComponents": 4,
            "coverage": ["VCF_INSTANCE: <instance-name>"],
            "issues": [],
        }
    ]
    assert roles.calls[0].request.headers["authorization"] == f"OpsToken {_OPS_TOKEN}"


@pytest.mark.asyncio
async def test_role_list_flattens_component_roles() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        result = await _run("fleet_role_list", {})
    assert result["roles"][0]["componentRoles"] == ["VCENTER: Admin"]


# ---------------------------------------------------------------------------
# vrops.fleet.principal.find
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_users_sends_the_search_and_returns_rows() -> None:
    answer = {
        "vidbUsers": [
            {
                "userId": _USER,
                "userName": "<user-name>",
                "firstName": "<first>",
                "lastName": "<last>",
                "domainName": "<domain>",
                "email": "<email>",
                "active": True,
                "roles": [
                    {"roleName": "vcf_viewer", "roleAssignmentTypes": ["DIRECT"]},
                    {"roleName": "custom_ops", "roleAssignmentTypes": ["GROUP"]},
                ],
            }
        ],
        "pageInfo": {"totalCount": 1},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        route = mock.post(_USERS_QUERY).respond(200, json=answer)
        result = await _run(
            "fleet_principal_find",
            {
                "ssoRealmId": _REALM,
                "principalType": "USER",
                "name": "<name>",
                "roleFilter": "HAS_ROLE",
            },
        )

    request = route.calls[0].request
    assert dict(request.url.params) == {"page": "0", "pageSize": "100"}
    body = json.loads(request.content)
    assert body["filters"] == "HAS_ROLE"
    assert {t["searchableField"] for t in body["searchTerms"]["anyOf"]} == {
        "USERNAME",
        "FIRSTNAME",
        "LASTNAME",
    }
    assert all(t["terms"] == ["<name>"] for t in body["searchTerms"]["anyOf"])
    assert result["totalCount"] == 1
    assert result["principals"] == [
        {
            "principalType": "USER",
            "principalId": _USER,
            "name": "<user-name>",
            "displayName": "<first> <last>",
            "domain": "<domain>",
            "email": "<email>",
            "active": True,
            "roles": ["vcf_viewer", "custom_ops"],
            "directRoles": ["vcf_viewer"],
        }
    ]


@pytest.mark.asyncio
async def test_find_groups_uses_the_group_query() -> None:
    answer = {"vidbGroups": [{"groupId": _GROUP, "displayName": "<group>", "roles": ["r1"]}]}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        route = mock.post(_GROUPS_QUERY).respond(200, json=answer)
        result = await _run(
            "fleet_principal_find",
            {"ssoRealmId": _REALM, "principalType": "GROUP", "page": 2, "pageSize": 10},
        )
    assert json.loads(route.calls[0].request.content) == {}
    assert dict(route.calls[0].request.url.params) == {"page": "2", "pageSize": "10"}
    assert result["principals"][0]["principalId"] == _GROUP
    assert result["principals"][0]["roles"] == ["r1"]


@pytest.mark.asyncio
async def test_find_one_principal_returns_its_assignments() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        mock.get(_PRINCIPAL_ROLES).respond(
            200,
            json={"vcfRoleAssignments": [_assignment("vcf_viewer", "VCF_INSTANCE", ("i-1",))]},
        )
        result = await _run("fleet_principal_find", {"ssoRealmId": _REALM, "principalId": _USER})
    assert result["assignments"] == [
        {
            "roleName": "vcf_viewer",
            "roleDisplayName": "VCF_VIEWER",
            "scopeType": "VCF_INSTANCE",
            "resourceIds": ["i-1"],
            "expiresAt": None,
        }
    ]


@pytest.mark.asyncio
async def test_unknown_sso_realm_is_not_found() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        with pytest.raises(ConnectorResourceNotFoundError, match="SSO realm") as caught:
            await _run("fleet_principal_find", {"ssoRealmId": "nope", "principalType": "USER"})
    assert caught.value.resource_ids == ["nope"]


@pytest.mark.parametrize("handler", ["fleet_principal_find", "fleet_role_assign"])
@pytest.mark.asyncio
async def test_unknown_user_or_group_is_not_found(handler: str) -> None:
    params: dict[str, Any] = {"ssoRealmId": _REALM, "principalId": _USER}
    if handler == "fleet_role_assign":
        params["add"] = [{"roleName": "vcf_viewer"}]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        mock.get(_PRINCIPAL_ROLES).respond(404, json={"message": "not found"})
        put = mock.put(_PRINCIPAL_ROLES).respond(200)
        with pytest.raises(ConnectorResourceNotFoundError, match="No user or group"):
            await _run(handler, params)
    assert not put.called


# ---------------------------------------------------------------------------
# vrops.fleet.role.assign
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_giving_one_role_keeps_the_others() -> None:
    current = [
        _assignment("vcf_viewer", "VCF_INSTANCE", ("i-1",)),
        {**_assignment("custom_ops"), "expiresAt": 1900000000000},
    ]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        mock.get(_PRINCIPAL_ROLES).respond(200, json={"vcfRoleAssignments": current})
        put = mock.put(_PRINCIPAL_ROLES).respond(200)
        result = await _run(
            "fleet_role_assign",
            {
                "ssoRealmId": _REALM,
                "principalId": _USER,
                "add": [{"roleName": "vcf_administrator"}],
            },
        )

    sent = json.loads(put.calls[0].request.content)["vcfRoleAssignments"]
    assert sent == [
        {
            "roleName": "vcf_viewer",
            "roleScope": {"scopeType": "VCF_INSTANCE", "resources": [{"id": "i-1"}]},
        },
        {
            "roleName": "custom_ops",
            "roleScope": {"scopeType": "SSO_REALM", "resources": []},
            "expiresAt": 1900000000000,
        },
        {
            "roleName": "vcf_administrator",
            "roleScope": {"scopeType": "SSO_REALM", "resources": []},
        },
    ]
    assert result["changed"] is True
    assert result["vendorCall"] == "PUT"
    assert [r["roleName"] for r in result["before"]] == ["vcf_viewer", "custom_ops"]


@pytest.mark.asyncio
async def test_removing_one_role_keeps_the_others_and_scoped_add_is_sent() -> None:
    current = [_assignment("vcf_viewer"), _assignment("custom_ops")]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        mock.get(_PRINCIPAL_ROLES).respond(200, json={"vcfRoleAssignments": current})
        put = mock.put(_PRINCIPAL_ROLES).respond(200)
        await _run(
            "fleet_role_assign",
            {
                "ssoRealmId": _REALM,
                "principalId": _USER,
                "remove": ["custom_ops"],
                "add": [
                    {
                        "roleName": "vcf_viewer",
                        "scopeType": "VCF_INSTANCE",
                        "resourceIds": ["i-2"],
                        "expiresAt": 1,
                    }
                ],
            },
        )
    assert json.loads(put.calls[0].request.content)["vcfRoleAssignments"] == [
        {
            "roleName": "vcf_viewer",
            "roleScope": {"scopeType": "VCF_INSTANCE", "resources": [{"id": "i-2"}]},
            "expiresAt": 1,
        }
    ]


@pytest.mark.asyncio
async def test_removing_the_last_role_uses_delete() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        mock.get(_PRINCIPAL_ROLES).respond(
            200, json={"vcfRoleAssignments": [_assignment("vcf_viewer")]}
        )
        delete = mock.delete(_PRINCIPAL_ROLES).respond(204)
        result = await _run(
            "fleet_role_assign",
            {"ssoRealmId": _REALM, "principalId": _USER, "remove": ["vcf_viewer"]},
        )
    assert delete.called
    assert result["vendorCall"] == "DELETE"
    assert result["after"] == []


@pytest.mark.asyncio
async def test_no_change_makes_no_vendor_write() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        mock.get(_PRINCIPAL_ROLES).respond(
            200, json={"vcfRoleAssignments": [_assignment("vcf_viewer")]}
        )
        put = mock.put(_PRINCIPAL_ROLES).respond(200)
        result = await _run(
            "fleet_role_assign",
            {
                "ssoRealmId": _REALM,
                "principalId": _USER,
                "add": [{"roleName": "vcf_viewer"}],
                "remove": ["custom_ops"],
            },
        )
    assert not put.called
    assert result["changed"] is False
    assert result["vendorCall"] is None


@pytest.mark.parametrize("params", [{"add": [{"roleName": "no_such_role"}]}, {"remove": ["gone"]}])
@pytest.mark.asyncio
async def test_unknown_role_is_not_found_and_nothing_is_written(params: dict[str, Any]) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        mock.get(_PRINCIPAL_ROLES).respond(
            200, json={"vcfRoleAssignments": [_assignment("vcf_viewer")]}
        )
        put = mock.put(_PRINCIPAL_ROLES).respond(200)
        with pytest.raises(ConnectorResourceNotFoundError, match="Unknown VCF role"):
            await _run("fleet_role_assign", {"ssoRealmId": _REALM, "principalId": _USER, **params})
    assert not put.called


@pytest.mark.asyncio
async def test_unknown_realm_on_assign_is_not_found() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        with pytest.raises(ConnectorResourceNotFoundError, match="SSO realm"):
            await _run(
                "fleet_role_assign",
                {"ssoRealmId": "nope", "principalId": _USER, "remove": ["vcf_viewer"]},
            )


@pytest.mark.asyncio
async def test_removing_the_last_role_of_the_last_fleet_administrator_is_refused() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        mock.get(_PRINCIPAL_ROLES).respond(
            200, json={"vcfRoleAssignments": [_assignment("vcf_administrator")]}
        )
        users = mock.post(_USERS_QUERY).respond(
            200,
            json={
                "vidbUsers": [
                    {"userId": _USER, "roles": [{"roleName": "VCF Administrator"}]},
                    {"userId": _OTHER_USER, "roles": [{"roleName": "VCF Viewer"}]},
                ]
            },
        )
        mock.post(_GROUPS_QUERY).respond(200, json={"vidbGroups": []})
        delete = mock.delete(_PRINCIPAL_ROLES).respond(204)
        with pytest.raises(LastFleetAdministratorError, match="last user or group"):
            await _run(
                "fleet_role_assign",
                {"ssoRealmId": _REALM, "principalId": _USER, "remove": ["vcf_administrator"]},
            )
    assert not delete.called
    assert json.loads(users.calls[0].request.content) == {"filters": "HAS_ROLE"}


@pytest.mark.asyncio
async def test_removing_an_administrator_is_allowed_when_a_group_still_has_the_role() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        mock.get(_PRINCIPAL_ROLES).respond(
            200, json={"vcfRoleAssignments": [_assignment("vcf_administrator")]}
        )
        mock.post(_USERS_QUERY).respond(200, json={"vidbUsers": []})
        mock.post(_GROUPS_QUERY).respond(
            200, json={"vidbGroups": [{"groupId": _GROUP, "roles": ["vcf_administrator"]}]}
        )
        delete = mock.delete(_PRINCIPAL_ROLES).respond(204)
        await _run(
            "fleet_role_assign",
            {"ssoRealmId": _REALM, "principalId": _USER, "remove": ["vcf_administrator"]},
        )
    assert delete.called


@pytest.mark.asyncio
async def test_vendor_error_on_put_reaches_the_caller() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        mock.get(_PRINCIPAL_ROLES).respond(200, json={"vcfRoleAssignments": []})
        mock.put(_PRINCIPAL_ROLES).respond(400, json={"message": "<vendor error text>"})
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await _run(
                "fleet_role_assign",
                {"ssoRealmId": _REALM, "principalId": _USER, "add": [{"roleName": "vcf_viewer"}]},
            )
    assert "<vendor error text>" in caught.value.response.text


@pytest.mark.parametrize(
    ("params", "message"),
    [
        (
            {"add": [{"roleName": "vcf_viewer"}], "remove": ["vcf_viewer"]},
            "both add and remove",
        ),
        ({"add": [{"roleName": "vcf_viewer", "scopeType": "VCF_INSTANCE"}]}, "resourceIds"),
    ],
)
@pytest.mark.asyncio
async def test_bad_role_input_is_refused(params: dict[str, Any], message: str) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        mock.get(_PRINCIPAL_ROLES).respond(200, json={"vcfRoleAssignments": []})
        put = mock.put(_PRINCIPAL_ROLES).respond(200)
        with pytest.raises(ValueError, match=message):
            await _run("fleet_role_assign", {"ssoRealmId": _REALM, "principalId": _USER, **params})
    assert not put.called


def test_merge_keeps_order_and_replaces_a_given_role() -> None:
    current = [_assignment("a"), _assignment("b"), _assignment("c")]
    merged = merge_role_assignments(current, [{"roleName": "b"}], ["c"])
    assert [m["roleName"] for m in merged] == ["a", "b"]


# ---------------------------------------------------------------------------
# Metadata and schemas
# ---------------------------------------------------------------------------


def test_reads_are_safe_and_the_change_waits_for_approval() -> None:
    by_id = {op.op_id: op for op in VROPS_FLEET_IAM_OPS}
    assert set(by_id) == {
        "vrops.fleet.role.list",
        "vrops.fleet.principal.find",
        "vrops.fleet.role.assign",
    }
    assign = by_id.pop("vrops.fleet.role.assign")
    assert assign.safety_level == "dangerous"
    assert assign.requires_approval is True
    assert "read-only" not in assign.tags
    for op in by_id.values():
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert "read-only" in op.tags
    for op in VROPS_FLEET_IAM_OPS:
        assert callable(getattr(VcfOperationsConnector, op.handler_attr))
        for example in op.parameter_schema["examples"]:
            assert validate_params(op.parameter_schema, example) == []


def test_fleet_role_ops_stay_out_of_the_shared_8x_tuple() -> None:
    shared = {op.op_id for op in VROPS_TYPED_OPS}
    assert not shared & {op.op_id for op in VROPS_FLEET_IAM_OPS}


def test_find_takes_a_principal_type_or_a_principal_id_not_both() -> None:
    schema = VROPS_FLEET_PRINCIPAL_FIND_OP.parameter_schema
    assert validate_params(schema, {"ssoRealmId": _REALM, "principalType": "GROUP"}) == []
    assert validate_params(schema, {"ssoRealmId": _REALM, "principalId": _USER}) == []
    assert validate_params(schema, {"ssoRealmId": _REALM})
    assert validate_params(
        schema, {"ssoRealmId": _REALM, "principalType": "USER", "principalId": _USER}
    )


def test_assign_needs_add_or_remove() -> None:
    schema = VROPS_FLEET_ROLE_ASSIGN_OP.parameter_schema
    assert validate_params(schema, {"ssoRealmId": _REALM, "principalId": _USER})
    assert validate_params(schema, {"ssoRealmId": _REALM, "principalId": _USER, "remove": []})
    assert (
        validate_params(schema, {"ssoRealmId": _REALM, "principalId": _USER, "remove": ["a"]}) == []
    )


@pytest.mark.asyncio
async def test_9x_registrar_registers_the_fleet_role_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    from meho_backplane.connectors.vcf_operations import typed_ops

    register = AsyncMock()
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.register_typed_operation", register
    )
    await typed_ops.register_vcf_operations_typed_operations(embedding_service=None)

    registered = {call.kwargs["op_id"]: call.kwargs for call in register.await_args_list}
    for op in VROPS_FLEET_IAM_OPS:
        assert registered[op.op_id]["version"] == "9.0"
        assert registered[op.op_id]["when_to_use"]
        assert registered[op.op_id]["safety_level"] == op.safety_level
        assert registered[op.op_id]["requires_approval"] is op.requires_approval


# ---------------------------------------------------------------------------
# Through the dispatcher
# ---------------------------------------------------------------------------


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    install_fake_client(monkeypatch, secret={"username": "svc", "password": "stub-password"})
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product="vrops", version="9.0", impl_id="vrops-rest", cls=VcfOperationsConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    get_settings.cache_clear()


class _DispatchTarget:
    def __init__(self, version: str) -> None:
        self.product = "vrops"
        self.fingerprint = {"version": version}
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID(int=0)
        self.name = "vrops-fleet-iam"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/vrops-fleet-iam"
        self.auth_model = "shared_service_account"
        self.auth_source: str | None = None


async def _register() -> None:
    from meho_backplane.connectors.vcf_operations import register_vcf_operations_typed_operations

    stub = AsyncMock()
    stub.encode_one.return_value = [0.1] * 384
    await register_vcf_operations_typed_operations(embedding_service=stub)


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.asyncio
async def test_assign_through_the_dispatcher_waits_for_approval() -> None:
    await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        get = mock.get(_PRINCIPAL_ROLES).respond(200, json={"vcfRoleAssignments": []})
        put = mock.put(_PRINCIPAL_ROLES).respond(200)
        result = await dispatch(
            operator=_operator(),
            connector_id="vrops-rest-9.0",
            op_id="vrops.fleet.role.assign",
            target=_DispatchTarget("9.1.1"),
            params={"ssoRealmId": _REALM, "principalId": _USER, "remove": ["vcf_viewer"]},
        )
    assert result.status == "awaiting_approval", result
    assert not get.called
    assert not put.called


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.asyncio
async def test_read_through_the_dispatcher_on_9_0_gives_the_version_message() -> None:
    await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        result = await dispatch(
            operator=_operator(),
            connector_id="vrops-rest-9.0",
            op_id="vrops.fleet.role.list",
            target=_DispatchTarget("9.0.0"),
            params={},
        )
    assert result.status == "error", result
    assert "needs VCF Operations 9.1 or newer" in json.dumps(result.model_dump(mode="json"))


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.asyncio
async def test_read_through_the_dispatcher_on_9_1_returns_rows() -> None:
    await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_basics(mock)
        result = await dispatch(
            operator=_operator(),
            connector_id="vrops-rest-9.0",
            op_id="vrops.fleet.role.list",
            target=_DispatchTarget("9.1.0"),
            params={},
        )
    assert result.status == "ok", result.error
    assert result.result["ssoRealms"][0]["id"] == _REALM
