# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCFA provisioning ops: org / role / user / project writes + reads + login test (#3890).

Drives each op through the real dispatcher (approval already granted,
``_approved=True``) against a respx-mocked appliance and pins, per write:
``created`` / ``unchanged`` (idempotent on name, nothing written) /
``invalid_request`` (nothing written) / upstream-error mapping
(``connector_error`` + ``upstream_message``), plus the password
discipline of ``vcfa.provider.user.create`` (read from Vault, sent only in
the create body, never in the result). The API-token pair lives in
``test_connectors_vcf_automation_api_token.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.vcf_automation import (
    VCFA_IMPL_ID,
    VCFA_PRODUCT,
    VCFA_PROVISIONING_OPS,
    VCFA_VERSION,
    VcfAutomationConnector,
)
from meho_backplane.connectors.vcf_automation import _lookups as lookups_module
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from tests._vcfa_provisioning_support import (
    BASE_URL,
    ORG,
    ORG_UUID,
    PASSWORD,
    TENANT_TOKEN,
    by_filter,
    mount_logins,
    page,
    run,
    seed_target,
    wire_connector,
)

_ORGS = "/cloudapi/1.0.0/orgs"
_GLOBAL_ROLES = "/cloudapi/1.0.0/globalRoles"
_RIGHTS = "/cloudapi/1.0.0/rights"

_EXPECTED_TIERS: dict[str, tuple[str, bool]] = {
    "vcfa.provider.right.list": ("safe", False),
    "vcfa.provider.role.list": ("safe", False),
    "vcfa.provider.org.create": ("caution", True),
    "vcfa.provider.role.create": ("caution", True),
    "vcfa.provider.user.create": ("caution", True),
    "vcfa.provider.api_token.create": ("dangerous", True),
    "vcfa.provider.api_token.revoke": ("dangerous", True),
    "vcfa.tenant.project.create": ("caution", True),
    "vcfa.tenant.login.test": ("safe", False),
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr("meho_backplane.operations._audit.publish_event", AsyncMock())
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()


@pytest.fixture
def vault_secret(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the operator-context Vault read behind ``password_secret_ref``."""
    data: dict[str, Any] = {"password": PASSWORD}

    async def _load(target: Any, _operator: Any, *, mount: str = "secret") -> dict[str, Any]:
        data["_last_ref"] = (target.secret_ref, mount)
        return {k: v for k, v in data.items() if not k.startswith("_")}

    monkeypatch.setattr(lookups_module, "load_vault_secret_data", _load)
    return data


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await seed_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


def _body(route: respx.Route) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(route.calls.last.request.content)
    return parsed


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_provisioning_tiers() -> None:
    assert {op.op_id: (op.safety_level, op.requires_approval) for op in VCFA_PROVISIONING_OPS} == (
        _EXPECTED_TIERS
    )


async def test_provisioning_ops_register_as_typed_rows() -> None:
    await VcfAutomationConnector.register_typed_operations()
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == VCFA_PRODUCT,
                    EndpointDescriptor.version == VCFA_VERSION,
                    EndpointDescriptor.impl_id == VCFA_IMPL_ID,
                )
            )
        ).scalars()
        by_op = {row.op_id: row for row in rows}
    for op_id, (safety, approval) in _EXPECTED_TIERS.items():
        row = by_op[op_id]
        assert row.source_kind == "typed"
        assert (row.safety_level, row.requires_approval) == (safety, approval)


# ---------------------------------------------------------------------------
# Org create
# ---------------------------------------------------------------------------


async def test_org_create_created(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ORGS).mock(side_effect=by_filter({}))
        post = mock.post(_ORGS).respond(201, json={**ORG, "isEnabled": True})
        result = await run("vcfa.provider.org.create", {"name": "example-org"})
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["status"] == "created"
    assert out["org"] == {
        "id": ORG["id"],
        "name": "example-org",
        "displayName": "Example",
        "isEnabled": True,
        "type": "vm_apps",
    }
    assert _body(post) == {
        "name": "example-org",
        "displayName": "example-org",
        "isEnabled": True,
        "isClassicTenant": True,
    }


async def test_org_create_async_202_rereads_by_name(vcfa: VcfAutomationConnector) -> None:
    lists = iter([[], [ORG]])
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ORGS).mock(side_effect=lambda _r: httpx.Response(200, json=page(next(lists))))
        mock.post(_ORGS).respond(202)
        result = await run(
            "vcfa.provider.org.create", {"name": "example-org", "org_type": "vm_apps"}
        )
    assert result["result"]["status"] == "created"
    assert result["result"]["org"]["id"] == ORG["id"]


async def test_org_create_unchanged_when_name_exists(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        # The appliance matches names case-insensitively; so does the handler.
        listing = mock.get(_ORGS).mock(side_effect=by_filter({"name==Example-Org": [ORG]}))
        post = mock.post(_ORGS).respond(201, json=ORG)
        result = await run("vcfa.provider.org.create", {"name": "Example-Org"})
    assert result["result"]["status"] == "unchanged"
    assert result["result"]["org"]["id"] == ORG["id"]
    assert not post.called
    assert listing.calls.last.request.url.params["filter"] == "name==Example-Org"


async def test_org_create_system_is_invalid_request(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        post = mock.post(_ORGS).respond(201, json=ORG)
        result = await run("vcfa.provider.org.create", {"name": "System"})
    assert result["result"]["status"] == "invalid_request"
    assert not post.called


async def test_org_create_upstream_400_is_connector_error(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ORGS).mock(side_effect=by_filter({}))
        mock.post(_ORGS).respond(400, json={"message": "Classic tenant creation is disabled"})
        result = await run("vcfa.provider.org.create", {"name": "example-org"})
    assert result["status"] == "error"
    assert result["extras"]["error_code"] == "connector_error"
    assert result["extras"]["http_status"] == 400
    assert "Classic tenant creation is disabled" in result["extras"]["upstream_message"]


async def test_org_create_bad_name_fails_schema(vcfa: VcfAutomationConnector) -> None:
    result = await run("vcfa.provider.org.create", {"name": "bad name"})
    assert result["status"] == "error"
    assert result["extras"]["error_code"] == "invalid_params"


# ---------------------------------------------------------------------------
# Global role create
# ---------------------------------------------------------------------------

_BASE_ROLE = {"id": "urn:vcloud:globalRole:base", "name": "Organization Administrator"}
_TOKEN_RIGHT = {"id": "urn:vcloud:right:tok", "name": "API Tokens: Manage"}
_BASE_RIGHTS = [{"id": "urn:vcloud:right:a", "name": "Right A"}, _TOKEN_RIGHT]


def _mount_role_reads(mock: respx.MockRouter, *, existing: bool = False) -> None:
    roles = {"name==Organization Administrator": [_BASE_ROLE]}
    if existing:
        roles["name==Custom Org Admin"] = [
            {"id": "urn:vcloud:globalRole:c", "name": "Custom Org Admin"}
        ]
    mock.get(_GLOBAL_ROLES).mock(side_effect=by_filter(roles))
    mock.get(f"{_GLOBAL_ROLES}/urn%3Avcloud%3AglobalRole%3Abase/rights").respond(
        200, json=page(_BASE_RIGHTS)
    )
    mock.get(_RIGHTS).mock(
        side_effect=by_filter({"name==API Tokens: Manage": [_TOKEN_RIGHT]}, default=[])
    )
    mock.get(_ORGS).mock(side_effect=by_filter({"name==example-org": [ORG]}))


_ROLE_PARAMS = {
    "name": "Custom Org Admin",
    "base_role": "Organization Administrator",
    "rights": ["API Tokens: Manage", "Extra Right"],
    "publish_to_org": "example-org",
}


async def test_role_create_created_and_published(vcfa: VcfAutomationConnector) -> None:
    new_id = "urn:vcloud:globalRole:new"
    seg = "urn%3Avcloud%3AglobalRole%3Anew"
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_role_reads(mock)
        mock.get(_RIGHTS).mock(
            side_effect=by_filter(
                {
                    "name==API Tokens: Manage": [_TOKEN_RIGHT],
                    "name==Extra Right": [{"id": "urn:vcloud:right:x", "name": "Extra Right"}],
                }
            )
        )
        create = mock.post(_GLOBAL_ROLES).respond(
            201, json={"id": new_id, "name": "Custom Org Admin"}
        )
        put_rights = mock.put(f"{_GLOBAL_ROLES}/{seg}/rights").respond(200, json=page([]))
        publish = mock.post(f"{_GLOBAL_ROLES}/{seg}/tenants/publish").respond(200, json=page([]))
        result = await run("vcfa.provider.role.create", _ROLE_PARAMS)
    out = result["result"]
    assert out["status"] == "created", out
    assert out["role"] == {"id": new_id, "name": "Custom Org Admin"}
    assert out["rights_count"] == 3  # base (2, incl. the token right) + extra, de-duplicated
    assert out["published_to"] == ["example-org"]
    assert _body(create)["bundleKey"] == "com.vmware.vcloud.undefined.key"
    assert {r["id"] for r in _body(put_rights)["values"]} == {
        "urn:vcloud:right:a",
        "urn:vcloud:right:tok",
        "urn:vcloud:right:x",
    }
    assert _body(publish) == {"values": [{"name": "example-org", "id": ORG["id"]}]}


async def test_role_create_publish_all(vcfa: VcfAutomationConnector) -> None:
    seg = "urn%3Avcloud%3AglobalRole%3Anew"
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_role_reads(mock)
        mock.post(_GLOBAL_ROLES).respond(201, json={"id": "urn:vcloud:globalRole:new"})
        mock.put(f"{_GLOBAL_ROLES}/{seg}/rights").respond(200, json=page([]))
        publish_all = mock.post(f"{_GLOBAL_ROLES}/{seg}/tenants/publishAll").respond(200, json={})
        result = await run(
            "vcfa.provider.role.create",
            {
                "name": "Custom Org Admin",
                "base_role": "Organization Administrator",
                "publish_all": True,
            },
        )
    assert result["result"]["published_to"] == "all"
    assert publish_all.called


async def test_role_create_unchanged_when_name_exists(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_role_reads(mock, existing=True)
        create = mock.post(_GLOBAL_ROLES).respond(201, json={"id": "x"})
        result = await run("vcfa.provider.role.create", _ROLE_PARAMS)
    assert result["result"]["status"] == "unchanged"
    assert result["result"]["role"]["id"] == "urn:vcloud:globalRole:c"
    assert not create.called


async def test_role_create_unknown_right_is_invalid_request(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_role_reads(mock)
        create = mock.post(_GLOBAL_ROLES).respond(201, json={"id": "x"})
        result = await run("vcfa.provider.role.create", _ROLE_PARAMS)
    out = result["result"]
    assert out["status"] == "invalid_request"
    assert "'Extra Right'" in out["guidance"]
    assert not create.called


async def test_role_create_unknown_publish_org_is_invalid_request(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_role_reads(mock)
        create = mock.post(_GLOBAL_ROLES).respond(201, json={"id": "x"})
        result = await run(
            "vcfa.provider.role.create",
            {
                "name": "Custom Org Admin",
                "rights": ["API Tokens: Manage"],
                "publish_to_org": "nope",
            },
        )
    assert result["result"]["status"] == "invalid_request"
    assert not create.called


async def test_role_create_right_with_comma_is_matched_by_full_scan(
    vcfa: VcfAutomationConnector,
) -> None:
    odd = {"id": "urn:vcloud:right:odd", "name": "View Rights, Roles"}
    seg = "urn%3Avcloud%3AglobalRole%3Anew"
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GLOBAL_ROLES).mock(side_effect=by_filter({}))
        rights = mock.get(_RIGHTS).mock(side_effect=by_filter({}, default=[_TOKEN_RIGHT, odd]))
        mock.post(_GLOBAL_ROLES).respond(201, json={"id": "urn:vcloud:globalRole:new"})
        put_rights = mock.put(f"{_GLOBAL_ROLES}/{seg}/rights").respond(200, json=page([]))
        result = await run(
            "vcfa.provider.role.create", {"name": "Viewer", "rights": ["View Rights, Roles"]}
        )
    assert result["result"]["status"] == "created"
    assert "filter" not in rights.calls.last.request.url.params
    assert _body(put_rights)["values"] == [odd]


# ---------------------------------------------------------------------------
# Org user create
# ---------------------------------------------------------------------------

_USERS = "/cloudapi/1.0.0/users"
_ROLES = "/cloudapi/1.0.0/roles"
_ORG_ROLE = {"id": "urn:vcloud:role:r1", "name": "Custom Org Admin"}
_USER_PARAMS = {
    "org": "example-org",
    "username": "org-admin",
    "role": "Custom Org Admin",
    "password_secret_ref": "example/vcfa-org-admin",
}


def _mount_user_reads(mock: respx.MockRouter, *, user_exists: bool = False) -> respx.Route:
    mock.get(_ORGS).mock(side_effect=by_filter({"name==example-org": [ORG]}))
    users = {"username==org-admin": [{"id": "urn:vcloud:user:u0", "username": "org-admin"}]}
    mock.get(_USERS).mock(side_effect=by_filter(users if user_exists else {}))
    return mock.get(_ROLES).mock(side_effect=by_filter({"name==Custom Org Admin": [_ORG_ROLE]}))


async def test_user_create_created_password_from_vault_not_in_result(
    vcfa: VcfAutomationConnector, vault_secret: dict[str, Any]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        roles = _mount_user_reads(mock)
        post = mock.post(_USERS).respond(
            201, json={"id": "urn:vcloud:user:u1", "username": "org-admin"}
        )
        result = await run("vcfa.provider.user.create", _USER_PARAMS)
    out = result["result"]
    assert out["status"] == "created", out
    assert out["user"] == {
        "id": "urn:vcloud:user:u1",
        "username": "org-admin",
        "org": "example-org",
        "role": "Custom Org Admin",
    }
    body = _body(post)
    assert body["password"] == PASSWORD
    assert body["providerType"] == "LOCAL"
    assert body["roleEntityRefs"] == [_ORG_ROLE]
    assert body["orgEntityRef"] == {"name": "example-org", "id": ORG["id"]}
    for route in (post, roles):
        headers = route.calls.last.request.headers
        assert headers["X-VMWARE-VCLOUD-TENANT-CONTEXT"] == ORG_UUID
        assert headers["X-VMWARE-VCLOUD-AUTH-CONTEXT"] == "example-org"
    assert vault_secret["_last_ref"] == ("example/vcfa-org-admin", "secret")
    assert PASSWORD not in json.dumps(result)


async def test_user_create_unchanged_when_user_exists(
    vcfa: VcfAutomationConnector, vault_secret: dict[str, Any]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_user_reads(mock, user_exists=True)
        post = mock.post(_USERS).respond(201, json={})
        result = await run("vcfa.provider.user.create", _USER_PARAMS)
    assert result["result"]["status"] == "unchanged"
    assert result["result"]["user"]["id"] == "urn:vcloud:user:u0"
    assert not post.called
    assert "_last_ref" not in vault_secret  # no Vault read for a no-op


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"org": "missing-org"}, "no org named 'missing-org'"),
        ({"role": "Nope"}, "role 'Nope' is not available"),
        ({"password_secret_key": "absent"}, "no usable string under key 'absent'"),
    ],
)
async def test_user_create_invalid_request_before_write(
    vcfa: VcfAutomationConnector,
    vault_secret: dict[str, Any],
    override: dict[str, Any],
    fragment: str,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_user_reads(mock)
        post = mock.post(_USERS).respond(201, json={})
        result = await run("vcfa.provider.user.create", {**_USER_PARAMS, **override})
    out = result["result"]
    assert out["status"] == "invalid_request"
    assert fragment in out["guidance"]
    assert not post.called


async def test_user_create_password_is_not_an_accepted_param(vcfa: VcfAutomationConnector) -> None:
    result = await run("vcfa.provider.user.create", {**_USER_PARAMS, "password": "inline"})
    assert result["extras"]["error_code"] == "invalid_params"


# ---------------------------------------------------------------------------
# Tenant project create
# ---------------------------------------------------------------------------

_PROJECTS = "/iaas/api/projects"


async def test_project_create_created(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        listing = mock.get(_PROJECTS).respond(200, json={"content": [], "totalElements": 0})
        post = mock.post(_PROJECTS).respond(
            201, json={"id": "p-1", "name": "demo-project", "organizationId": "o-1"}
        )
        result = await run(
            "vcfa.tenant.project.create",
            {
                "name": "demo-project",
                "description": "demo",
                "administrators": [{"email": "org-admin"}],
                "members": [{"email": "devs", "type": "group"}],
            },
        )
    out = result["result"]
    assert out["status"] == "created", out
    assert out["project"] == {"id": "p-1", "name": "demo-project", "organizationId": "o-1"}
    assert listing.calls.last.request.url.params["$filter"] == "name eq 'demo-project'"
    request = post.calls.last.request
    assert request.url.params["apiVersion"] == "2021-07-15"
    assert request.headers["Authorization"] == f"Bearer {TENANT_TOKEN}"
    assert json.loads(request.content) == {
        "name": "demo-project",
        "description": "demo",
        "administrators": [{"email": "org-admin", "type": "user"}],
        "members": [{"email": "devs", "type": "group"}],
    }


async def test_project_create_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_PROJECTS).respond(
            200, json={"content": [{"id": "p-0", "name": "Demo-Project"}], "totalElements": 1}
        )
        post = mock.post(_PROJECTS).respond(201, json={})
        result = await run("vcfa.tenant.project.create", {"name": "demo-project"})
    assert result["result"]["status"] == "unchanged"
    assert result["result"]["project"]["id"] == "p-0"
    assert not post.called


async def test_project_create_403_is_connector_error(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_PROJECTS).respond(200, json={"content": []})
        mock.post(_PROJECTS).respond(403, json={"message": "User is not authorized"})
        result = await run("vcfa.tenant.project.create", {"name": "demo-project"})
    assert result["status"] == "error"
    assert result["extras"]["http_status"] == 403
    assert "User is not authorized" in (result["extras"].get("upstream_message") or "")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_right_list_forwards_name_substring(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(_RIGHTS).respond(200, json=page([_TOKEN_RIGHT]))
        result = await run("vcfa.provider.right.list", {"name_contains": "Token", "pageSize": 10})
    assert result["status"] == "ok"
    params = route.calls.last.request.url.params
    assert (params["filter"], params["pageSize"]) == ("name==*Token*", "10")


async def test_role_list_global_and_org_scoped(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        global_route = mock.get(_GLOBAL_ROLES).respond(200, json=page([_BASE_ROLE]))
        mock.get(_ORGS).mock(side_effect=by_filter({"name==example-org": [ORG]}))
        org_route = mock.get(_ROLES).respond(200, json=page([_ORG_ROLE]))
        await run("vcfa.provider.role.list", {})
        scoped = await run("vcfa.provider.role.list", {"org": "example-org"})
        missing = await run("vcfa.provider.role.list", {"org": "nope"})
    assert global_route.called
    assert scoped["status"] == "ok"
    assert org_route.calls.last.request.headers["X-VMWARE-VCLOUD-TENANT-CONTEXT"] == ORG_UUID
    assert missing["status"] == "error"
    assert missing["extras"]["error_code"] == "connector_error"


# ---------------------------------------------------------------------------
# Tenant login test
# ---------------------------------------------------------------------------


async def test_login_test_success_does_not_cache_the_bearer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        login = mock.post("/iaas/api/login").respond(200, json={"token": TENANT_TOKEN})
        mock.get("/iaas/api/about").respond(200, json={"latestApiVersion": "2021-07-15"})
        result = await run("vcfa.tenant.login.test", {})
    assert result["result"] == {
        "authenticated": True,
        "login_flow": "username_password",
        "api_version": "2021-07-15",
        "error": None,
    }
    assert login.call_count == 1
    assert vcfa._tenant_tokens == {}
    assert TENANT_TOKEN not in json.dumps(result)


async def test_login_test_refused_refresh_token_is_a_result(
    vcfa: VcfAutomationConnector,
) -> None:
    async def _loader(_target: object, _operator: object) -> dict[str, str]:
        return {"username": "admin", "password": "pw", "refresh_token": "api-token-sentinel"}

    vcfa._credentials_loader = _loader
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.post("/iaas/api/login").respond(400, json={"error": "invalid_grant"})
        mock.get("/iaas/api/about").respond(503)
        result = await run("vcfa.tenant.login.test", {})
    assert result["status"] == "ok"
    out = result["result"]
    assert out["authenticated"] is False
    assert out["login_flow"] == "refresh_token"
    assert out["api_version"] is None
    assert out["error"]["cause"] == "session_establish_400"
    assert "api-token-sentinel" not in json.dumps(result)
