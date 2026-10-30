# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCFA roles and VM Apps orgs: the role.create refusal, role.delete, org role.list (#3894).

A VM Apps org (``isClassicTenant: true``) has its own fixed roles and the
appliance refuses to publish a global role to it. These tests drive the ops
through the real dispatcher (approval already granted) against a
respx-mocked appliance and pin:

* ``vcfa.provider.role.create`` answers ``invalid_request`` for a VM Apps
  org before any write, and names the org's own roles;
* ``vcfa.provider.role.delete`` answers ``deleted`` / ``unchanged`` /
  ``invalid_request`` (built-in role, or a name several roles match only
  by case) and maps an upstream 4xx, and its approval preview agrees;
* ``vcfa.provider.role.list`` with ``org`` returns a VM Apps org's own roles.

All names are synthetic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import unquote

import httpx
import pytest
import respx

from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from tests._vcfa_provisioning_support import (
    BASE_URL,
    OPERATOR,
    ORG,
    ORG_UUID,
    by_filter,
    mount_logins,
    page,
    resolved_target,
    run,
    seed_target,
    wire_connector,
)

_ORGS = "/cloudapi/1.0.0/orgs"
_GLOBAL_ROLES = "/cloudapi/1.0.0/globalRoles"
_ROLES = "/cloudapi/1.0.0/roles"
_RIGHTS = "/cloudapi/1.0.0/rights"

#: ``ORG`` in the support module is a VM Apps org (``isClassicTenant: true``).
_VM_APPS_ORG = ORG
_ORG_OWN_ROLES = [
    {"id": "urn:vcloud:role:owner", "name": "Organization Owner"},
    {"id": "urn:vcloud:role:member", "name": "Organization Member"},
]
_CUSTOM_ROLE = {
    "id": "urn:vcloud:globalRole:c",
    "name": "Custom Org Admin",
    "readOnly": False,
}
_CUSTOM_SEG = "urn%3Avcloud%3AglobalRole%3Ac"


@pytest.fixture(autouse=True)
def _env_and_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    get_settings.cache_clear()


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await seed_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


def _mount_writes(mock: respx.MockRouter) -> list[respx.Route]:
    """Every write route ``role.create`` could send; none may be called."""
    return [
        mock.post(_GLOBAL_ROLES).respond(201, json={"id": "urn:vcloud:globalRole:new"}),
        mock.put(url__regex=r".*/globalRoles/.*").respond(200, json=page([])),
        mock.post(url__regex=r".*/globalRoles/.*/tenants/.*").respond(200, json={}),
        mock.delete(url__regex=r".*/globalRoles/.*").respond(204),
    ]


# ---------------------------------------------------------------------------
# role.create and a VM Apps org
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role_exists", [False, True], ids=["new-role", "existing-role"])
async def test_role_create_refuses_vm_apps_org_before_any_write(
    vcfa: VcfAutomationConnector, role_exists: bool
) -> None:
    roles = {"name==Custom Org Admin": [_CUSTOM_ROLE]} if role_exists else {}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ORGS).mock(side_effect=by_filter({"name==example-org": [_VM_APPS_ORG]}))
        global_roles = mock.get(_GLOBAL_ROLES).mock(side_effect=by_filter(roles))
        mock.get(_RIGHTS).respond(200, json=page([]))
        org_roles = mock.get(_ROLES).respond(200, json=page(_ORG_OWN_ROLES))
        writes = _mount_writes(mock)
        result = await run(
            "vcfa.provider.role.create",
            {
                "name": "Custom Org Admin",
                "rights": ["API Tokens: Manage"],
                "publish_to_org": "example-org",
            },
        )
    out = result["result"]
    assert out["status"] == "invalid_request", out
    assert "VM Apps org" in out["guidance"]
    assert "'Organization Owner', 'Organization Member'" in out["guidance"]
    assert out["role"] is None
    assert not global_roles.called  # refused before the role lookup
    assert org_roles.calls.last.request.headers["X-VMWARE-VCLOUD-TENANT-CONTEXT"] == ORG_UUID
    for route in writes:
        assert not route.called


async def test_role_create_vm_apps_refusal_survives_a_failed_role_read(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ORGS).mock(side_effect=by_filter({"name==example-org": [_VM_APPS_ORG]}))
        mock.get(_ROLES).respond(403, json={"message": "denied"})
        writes = _mount_writes(mock)
        result = await run(
            "vcfa.provider.role.create",
            {"name": "Custom Org Admin", "rights": ["X"], "publish_to_org": "example-org"},
        )
    out = result["result"]
    assert out["status"] == "invalid_request", out
    assert "vcfa.provider.role.list" in out["guidance"]
    for route in writes:
        assert not route.called


# ---------------------------------------------------------------------------
# role.delete
# ---------------------------------------------------------------------------


async def test_role_delete_deleted(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GLOBAL_ROLES).mock(
            side_effect=by_filter({"name==Custom Org Admin": [_CUSTOM_ROLE]})
        )
        delete = mock.delete(f"{_GLOBAL_ROLES}/{_CUSTOM_SEG}").respond(204)
        result = await run("vcfa.provider.role.delete", {"name": "Custom Org Admin"})
    out = result["result"]
    assert out == {
        "status": "deleted",
        "role": {"id": _CUSTOM_ROLE["id"], "name": "Custom Org Admin"},
        "guidance": None,
    }
    assert delete.called


async def test_role_delete_unchanged_when_missing(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GLOBAL_ROLES).mock(side_effect=by_filter({}))
        delete = mock.delete(url__regex=r".*/globalRoles/.*").respond(204)
        result = await run("vcfa.provider.role.delete", {"name": "Custom Org Admin"})
    out = result["result"]
    assert out["status"] == "unchanged", out
    assert out["role"] is None
    assert not delete.called


async def test_role_delete_unchanged_when_gone_before_the_delete(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GLOBAL_ROLES).mock(
            side_effect=by_filter({"name==Custom Org Admin": [_CUSTOM_ROLE]})
        )
        mock.delete(f"{_GLOBAL_ROLES}/{_CUSTOM_SEG}").respond(404, json={"message": "gone"})
        result = await run("vcfa.provider.role.delete", {"name": "Custom Org Admin"})
    assert result["result"]["status"] == "unchanged", result


async def test_role_delete_refuses_a_built_in_role(vcfa: VcfAutomationConnector) -> None:
    built_in = {"id": "urn:vcloud:globalRole:base", "name": "Organization Administrator"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GLOBAL_ROLES).mock(
            side_effect=by_filter(
                {"name==Organization Administrator": [{**built_in, "readOnly": True}]}
            )
        )
        delete = mock.delete(url__regex=r".*/globalRoles/.*").respond(204)
        result = await run("vcfa.provider.role.delete", {"name": "Organization Administrator"})
    out = result["result"]
    assert out["status"] == "invalid_request", out
    assert out["role"] == built_in
    assert not delete.called


async def test_role_delete_upstream_400_is_connector_error(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GLOBAL_ROLES).mock(
            side_effect=by_filter({"name==Custom Org Admin": [_CUSTOM_ROLE]})
        )
        mock.delete(f"{_GLOBAL_ROLES}/{_CUSTOM_SEG}").respond(
            400, json={"message": "Role is in use"}
        )
        result = await run("vcfa.provider.role.delete", {"name": "Custom Org Admin"})
    assert result["status"] == "error"
    assert result["extras"]["error_code"] == "connector_error"
    assert result["extras"]["http_status"] == 400
    assert "Role is in use" in (result["extras"].get("upstream_message") or "")


async def test_role_delete_preview_reads_the_role(vcfa: VcfAutomationConnector) -> None:
    builder = _PREVIEW_BUILDERS["vcfa.provider.role.delete"]
    target = await resolved_target()

    def ctx(name: str, connector: Any) -> Any:
        return SimpleNamespace(
            connector_instance=connector, operator=OPERATOR, target=target, params={"name": name}
        )

    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GLOBAL_ROLES).mock(
            side_effect=by_filter({"name==Custom Org Admin": [_CUSTOM_ROLE]})
        )
        found = await builder(ctx("Custom Org Admin", vcfa))
        missing = await builder(ctx("Nope", vcfa))
    offline = await builder(ctx("Custom Org Admin", None))
    assert found == {
        "action": "delete_global_role",
        "name": "Custom Org Admin",
        "would": "delete",
        "exists": True,
        "existing_id": _CUSTOM_ROLE["id"],
    }
    assert (missing["would"], missing["exists"]) == ("unchanged", False)
    assert (offline["would"], offline["exists"]) == ("delete_if_present", None)


async def test_role_delete_preview_refuses_a_built_in_role(
    vcfa: VcfAutomationConnector,
) -> None:
    builder = _PREVIEW_BUILDERS["vcfa.provider.role.delete"]
    target = await resolved_target()
    built_in = {
        "id": "urn:vcloud:globalRole:base",
        "name": "Organization Administrator",
        "readOnly": True,
    }
    ctx = SimpleNamespace(
        connector_instance=vcfa,
        operator=OPERATOR,
        target=target,
        params={"name": "Organization Administrator"},
    )
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GLOBAL_ROLES).mock(
            side_effect=by_filter({"name==Organization Administrator": [built_in]})
        )
        preview = await builder(ctx)
    assert preview["would"] == "refuse_built_in", preview
    assert preview["exists"] is True
    assert preview["existing_id"] == built_in["id"]
    assert "built in" in preview["reason"]


#: Two roles whose names differ only by case.
_CASE_TWINS = [
    _CUSTOM_ROLE,
    {"id": "urn:vcloud:globalRole:d", "name": "custom org admin", "readOnly": False},
]


async def test_role_delete_picks_the_exact_name_match(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GLOBAL_ROLES).mock(side_effect=by_filter({"name==custom org admin": _CASE_TWINS}))
        exact = mock.delete(url__regex=r".*/globalRoles/urn%3Avcloud%3AglobalRole%3Ad$").respond(
            204
        )
        other = mock.delete(f"{_GLOBAL_ROLES}/{_CUSTOM_SEG}").respond(204)
        result = await run("vcfa.provider.role.delete", {"name": "custom org admin"})
    out = result["result"]
    assert out["status"] == "deleted", out
    assert out["role"]["id"] == "urn:vcloud:globalRole:d"
    assert exact.called
    assert not other.called


async def test_role_delete_repeated_leaves_the_case_twin_alone(
    vcfa: VcfAutomationConnector,
) -> None:
    """The second run of the same delete must not take the role that differs by case."""
    roles = list(_CASE_TWINS)

    def _list(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page(roles))

    def _delete(request: httpx.Request) -> httpx.Response:
        role_id = unquote(request.url.path.rsplit("/", 1)[-1])
        roles[:] = [r for r in roles if r["id"] != role_id]
        return httpx.Response(204)

    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GLOBAL_ROLES).mock(side_effect=_list)
        delete = mock.delete(url__regex=r".*/globalRoles/.*").mock(side_effect=_delete)
        first = await run("vcfa.provider.role.delete", {"name": "custom org admin"})
        second = await run("vcfa.provider.role.delete", {"name": "custom org admin"})
    assert first["result"]["status"] == "deleted", first
    assert first["result"]["role"]["id"] == "urn:vcloud:globalRole:d"
    assert second["result"]["status"] == "unchanged", second
    assert second["result"]["role"] is None
    assert delete.call_count == 1
    assert roles == [_CUSTOM_ROLE]


async def test_role_delete_never_takes_a_role_that_differs_only_by_case(
    vcfa: VcfAutomationConnector,
) -> None:
    builder = _PREVIEW_BUILDERS["vcfa.provider.role.delete"]
    target = await resolved_target()
    ctx = SimpleNamespace(
        connector_instance=vcfa,
        operator=OPERATOR,
        target=target,
        params={"name": "CUSTOM ORG ADMIN"},
    )
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GLOBAL_ROLES).mock(side_effect=by_filter({"name==CUSTOM ORG ADMIN": _CASE_TWINS}))
        delete = mock.delete(url__regex=r".*/globalRoles/.*").respond(204)
        result = await run("vcfa.provider.role.delete", {"name": "CUSTOM ORG ADMIN"})
        preview = await builder(ctx)
    out = result["result"]
    assert out["status"] == "unchanged", out
    assert out["role"] is None
    assert not delete.called
    assert preview["would"] == "unchanged", preview
    assert preview["exists"] is False


#: Two roles that carry exactly the same name.
_EXACT_TWINS = [
    _CUSTOM_ROLE,
    {"id": "urn:vcloud:globalRole:e", "name": "Custom Org Admin", "readOnly": False},
]


async def test_role_delete_refuses_a_name_shared_by_two_roles(
    vcfa: VcfAutomationConnector,
) -> None:
    builder = _PREVIEW_BUILDERS["vcfa.provider.role.delete"]
    target = await resolved_target()
    ctx = SimpleNamespace(
        connector_instance=vcfa,
        operator=OPERATOR,
        target=target,
        params={"name": "Custom Org Admin"},
    )
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GLOBAL_ROLES).mock(
            side_effect=by_filter({"name==Custom Org Admin": _EXACT_TWINS})
        )
        delete = mock.delete(url__regex=r".*/globalRoles/.*").respond(204)
        result = await run("vcfa.provider.role.delete", {"name": "Custom Org Admin"})
        preview = await builder(ctx)
    out = result["result"]
    assert out["status"] == "invalid_request", out
    assert out["role"] is None
    assert "2 global roles" in out["guidance"]
    assert not delete.called
    assert preview["would"] == "refuse_ambiguous_name", preview
    assert preview["reason"] == out["guidance"]


# ---------------------------------------------------------------------------
# role.list with org
# ---------------------------------------------------------------------------


async def test_role_list_with_org_returns_the_vm_apps_org_own_roles(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ORGS).mock(side_effect=by_filter({"name==example-org": [_VM_APPS_ORG]}))
        global_roles = mock.get(_GLOBAL_ROLES).respond(200, json=page([]))
        org_roles = mock.get(_ROLES).respond(200, json=page(_ORG_OWN_ROLES))
        result = await run("vcfa.provider.role.list", {"org": "example-org"})
    assert result["status"] == "ok", result
    assert [row["name"] for row in result["result"]["values"]] == [
        "Organization Owner",
        "Organization Member",
    ]
    headers = org_roles.calls.last.request.headers
    assert headers["X-VMWARE-VCLOUD-TENANT-CONTEXT"] == ORG_UUID
    assert headers["X-VMWARE-VCLOUD-AUTH-CONTEXT"] == "example-org"
    assert not global_roles.called
