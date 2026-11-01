# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the HCX Manager setup operations, with a respx-mocked appliance API (port 9443).

* each operation is typed with the safety level of the Task table; every
  change waits for a person's approval and sends nothing before;
* each change sends the right call with HTTP basic on port 9443, and a
  vendor error reaches the answer without the password or key it sent;
* refusals: an unreachable or untrusted vCenter or NSX, a city that is not
  found or found twice, an SSO group HCX does not know, a wrong licence key,
  a restart while migrations run;
* the restart: one already running starts nothing, a failure part-way lists
  the steps, a Manager that does not answer gives ``not_reachable``.

Fixture values are made up; no real host, user, key or id appears here.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import func, select
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.hcx import (
    HCX_CONNECTOR_ID,
    HCX_IMPL_ID,
    HCX_PRODUCT,
    HCX_TYPED_OPS,
    HCX_VERSION,
    HcxConnector,
    register_hcx_typed_operations,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    DispatchTrace,
    DispatchTraceSpan,
    EndpointDescriptor,
    Tenant,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_HOST = "hcx-setup.test.invalid"
_APPLIANCE = f"https://{_HOST}:9443"
_MAIN = f"https://{_HOST}"
_TARGET_NAME = "hcx-setup"
_ADMIN_USER = "<appliance-user>"
_ADMIN_PASSWORD = "appliance-password-value"
_VC_PASSWORD = "vcenter-password-value"
_NSX_PASSWORD = "nsx-password-value"
_KEY = "AAAA-BBBB-CCCC-DDDD-EEEE"
_TOKEN = "setup-hm-token"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000004550")

_VCENTER = "/api/admin/global/config/vcenter"
_LOOKUP = "/api/admin/global/config/lookupservice"
_NSX = "/api/admin/global/config/nsx"
_LOCATION = "/api/admin/global/config/location"
_CITIES = "/api/admin/global/config/searchCities"
_ROLES = "/api/admin/global/config/roleMappings"
_HCX_CONFIG = "/api/admin/global/config/hcx"
_ACTIVATION_STATUS = "/license/hcx/activationStatus"
_APPENGINE = "/components/appengine"
_APPENGINE_STATUS = "/components/appengine/status"
_MIGRATIONS = "/hybridity/api/migrations"

_TABLE = {
    "hcx.manager.config.show": "safe",
    "hcx.manager.vcenter.set": "dangerous",
    "hcx.manager.nsx.set": "dangerous",
    "hcx.manager.location.set": "caution",
    "hcx.manager.role_mapping.set": "dangerous",
    "hcx.manager.activate": "dangerous",
    "hcx.manager.restart": "dangerous",
    "hcx.manager.service.status": "safe",
}

_VC_PARAMS = {
    "vcenter_url": "https://<vcenter-fqdn>",
    "vcenter_secret_ref": "hcx/vcenter-login",
    "lookup_service_url": "https://<vcenter-fqdn>",
}
_NSX_PARAMS = {"nsx_url": "https://<nsx-fqdn>", "nsx_secret_ref": "hcx/nsx-login"}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=HCX_PRODUCT, version=HCX_VERSION, impl_id=HCX_IMPL_ID, cls=HcxConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


def _operator() -> Operator:
    return Operator(
        sub="hcx-setup-requester",
        name="HCX Setup Test Operator",
        email=None,
        raw_jwt="<hcx-setup-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


_SESSION_LOADER = AsyncMock(return_value={"username": _ADMIN_USER, "password": _ADMIN_PASSWORD})

#: What the Vault reads of the op inputs return, by reference.
_PARAM_SECRETS: dict[str, dict[str, str]] = {
    "hcx/vcenter-login": {"username": "<vc-user>", "password": _VC_PASSWORD},
    "hcx/nsx-login": {"username": "<nsx-user>", "password": _NSX_PASSWORD},
    "hcx/activation": {"activation_key": _KEY},
}


async def _param_secret(target: Any, _operator: Operator, *, param: str) -> dict[str, str]:
    del param
    return _PARAM_SECRETS[target.secret_ref]


async def _seed(extras: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product=HCX_PRODUCT,
                host=_HOST,
                port=443,
                fqdn=None,
                secret_ref="hcx/setup",
                auth_model="shared_service_account",
                vpn_required=False,
                extras=extras,
                fingerprint={"version": "4.11.0"},
                notes="seeded by test_connectors_hcx_manager_setup",
            )
        )
        await session.commit()


@pytest.fixture
async def hcx() -> AsyncIterator[HcxConnector]:
    set_default_reducer(PassThroughReducer())
    await register_hcx_typed_operations()
    await _seed({})
    instance = get_or_create_connector_instance(HcxConnector)
    assert isinstance(instance, HcxConnector)
    instance._session_loader = _SESSION_LOADER
    with patch(
        "meho_backplane.connectors.hcx.manager_setup.load_param_secret_data",
        side_effect=_param_secret,
    ):
        yield instance


async def _run(op_id: str, params: dict[str, Any] | None = None) -> Any:
    """Run the op as an already-approved call (the handler path)."""
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    return await dispatch(
        operator=_operator(),
        connector_id=HCX_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params or {},
        _approved=True,
    )


def _a(path: str) -> str:
    return f"{_APPLIANCE}{path}"


def _body(route: respx.Route, index: int = 0) -> Any:
    return json.loads(route.calls[index].request.content or b"null")


def _basic() -> str:
    return "Basic " + base64.b64encode(f"{_ADMIN_USER}:{_ADMIN_PASSWORD}".encode()).decode()


def _items(*configs: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"items": [{"section": "<section>", "config": c} for c in configs]}}


def _no_secret_in(value: Any, *secrets: str) -> None:
    text = json.dumps(value, default=str)
    for secret in secrets:
        assert secret not in text
        assert base64.b64encode(secret.encode()).decode() not in text


# ---------------------------------------------------------------------------
# Registration and approval
# ---------------------------------------------------------------------------


async def test_each_operation_has_the_table_risk(hcx: HcxConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = {
            row.op_id: row
            for row in (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.impl_id == HCX_IMPL_ID)
                )
            )
            .scalars()
            .all()
        }
    ops = {op.op_id: op for op in HCX_TYPED_OPS}
    for op_id, safety in _TABLE.items():
        row = rows[op_id]
        assert row.source_kind == "typed"
        assert row.safety_level == safety, op_id
        assert row.requires_approval is (safety != "safe"), op_id
        assert ops[op_id].group_key == "hcx-manager-setup"
        if safety != "safe":
            assert "preconditions" in row.llm_instructions


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("hcx.manager.vcenter.set", _VC_PARAMS),
        ("hcx.manager.nsx.set", _NSX_PARAMS),
        ("hcx.manager.location.set", {"city": "<city>"}),
        ("hcx.manager.role_mapping.set", {"system_admin_groups": ["<domain>\\<group>"]}),
        ("hcx.manager.activate", {"activation_key_secret_ref": "hcx/activation"}),
        ("hcx.manager.restart", {}),
    ],
)
async def test_a_change_waits_for_approval_and_sends_nothing(
    hcx: HcxConnector, op_id: str, params: dict[str, Any]
) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        vendor = mock.route().respond(200, json={})
        call = await call_operation(
            _operator(),
            {
                "connector_id": HCX_CONNECTOR_ID,
                "op_id": op_id,
                "target": _TARGET_NAME,
                "params": params,
            },
        )
    assert call["status"] == "awaiting_approval", call
    assert not vendor.called
    async with get_sessionmaker()() as session:
        count = await session.execute(select(func.count()).select_from(ApprovalRequest))
    assert int(count.scalar_one()) == 1


# ---------------------------------------------------------------------------
# hcx.manager.config.show
# ---------------------------------------------------------------------------


async def test_config_show_reads_every_section_with_basic_auth_on_9443(hcx: HcxConnector) -> None:
    vc = {
        "url": "https://<vcenter-fqdn>",
        "userName": "<vc-user>",
        "password": "c2VjcmV0",
        "vcuuid": "<vc-uuid>",
        "UUID": "<reg-id>",
        "version": "8.0.3",
        "buildNumber": "<build>",
        "name": "<vcenter-name>",
    }
    async with respx.mock(assert_all_called=False) as mock:
        routes = [
            mock.get(_a(_VCENTER)).respond(200, json=_items(vc)),
            mock.get(_a(_LOOKUP)).respond(
                200, json=_items({"lookupServiceUrl": "https://<sso>", "providerType": "PSC"})
            ),
            mock.get(_a(_NSX)).respond(200, json={"data": {"items": []}}),
            mock.get(_a(_LOCATION)).respond(
                200,
                json={"city": "<city>", "country": "<country>", "latitude": 1.5, "longitude": 2.5},
            ),
            mock.get(_a(_ROLES)).respond(
                200, json=[{"role": "System Administrator", "userGroups": ["<domain>\\<group>"]}]
            ),
            mock.get(_a(_HCX_CONFIG)).respond(
                200, json=_items({"url": "https://<activation-server>", "activationKey": _KEY})
            ),
        ]
        result = await _run("hcx.manager.config.show")

    assert result.status == "ok", result.error
    for route in routes:
        assert route.calls[0].request.headers["authorization"] == _basic()
    answer = result.result
    assert answer["vcenter"]["url"] == "https://<vcenter-fqdn>"
    assert answer["vcenter"]["user_name"] == "<vc-user>"
    assert answer["lookup_service"] == {
        "url": "https://<sso>",
        "provider_type": "PSC",
        "uuid": None,
    }
    assert answer["nsx"] is None
    assert answer["location"]["city"] == "<city>"
    assert answer["role_mappings"] == [
        {"role": "System Administrator", "user_groups": ["<domain>\\<group>"]}
    ]
    assert answer["activation"]["activated"] is True
    assert answer["activation"]["source"] == "hcx_config"
    assert answer["missing"] == []
    _no_secret_in(answer, _KEY, "c2VjcmV0", _ADMIN_PASSWORD)


async def test_config_show_uses_the_9x_activation_status_when_the_key_call_is_missing(
    hcx: HcxConnector,
) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        for path in (_VCENTER, _LOOKUP, _NSX):
            mock.get(_a(path)).respond(200, json={"data": {"items": []}})
        mock.get(_a(_LOCATION)).respond(404, json={"message": "not found"})
        mock.get(_a(_ROLES)).respond(200, json=[])
        mock.get(_a(_HCX_CONFIG)).respond(404, json={"message": "not found"})
        mock.get(_a(_ACTIVATION_STATUS)).respond(
            200,
            json={
                "data": {
                    "activationDone": True,
                    "activationStatus": "ACTIVE",
                    "activationType": "VCF",
                }
            },
        )
        result = await _run("hcx.manager.config.show")

    assert result.status == "ok", result.error
    assert result.result["vcenter"] is None
    assert result.result["location"] is None
    assert result.result["missing"] == ["location"]
    assert result.result["activation"]["activated"] is True
    assert result.result["activation"]["source"] == "activation_status"


async def test_the_appliance_account_comes_from_extras_when_set(hcx: HcxConnector) -> None:
    async with get_sessionmaker()() as session:
        row = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
        await session.execute(
            TargetORM.__table__.update()
            .where(TargetORM.id == row.id)
            .values(extras={"appliance_secret_ref": "hcx/appliance-admin"})
        )
        await session.commit()
    loader = AsyncMock(return_value={"username": "<other-user>", "password": "other-pass"})
    with patch("meho_backplane.connectors.hcx.appliance_api.load_basic_credentials", loader):
        async with respx.mock(assert_all_called=False) as mock:
            status = mock.get(_a(_APPENGINE_STATUS)).respond(200, json={"result": "RUNNING"})
            result = await _run("hcx.manager.service.status")
    assert result.status == "ok", result.error
    assert loader.await_args.args[0].secret_ref == "hcx/appliance-admin"
    expected = "Basic " + base64.b64encode(b"<other-user>:other-pass").decode()
    assert status.calls[0].request.headers["authorization"] == expected


# ---------------------------------------------------------------------------
# hcx.manager.vcenter.set and hcx.manager.nsx.set
# ---------------------------------------------------------------------------


async def test_vcenter_set_registers_vcenter_then_sso(hcx: HcxConnector) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        vc = mock.post(_a(_VCENTER)).respond(
            200,
            json=_items(
                {
                    "url": "https://<vcenter-fqdn>",
                    "userName": "<vc-user>",
                    "password": base64.b64encode(_VC_PASSWORD.encode()).decode(),
                }
            ),
        )
        sso = mock.post(_a(_LOOKUP)).respond(
            200, json=_items({"lookupServiceUrl": "https://<vcenter-fqdn>", "providerType": "PSC"})
        )
        result = await _run("hcx.manager.vcenter.set", _VC_PARAMS)

    assert result.status == "ok", result.error
    assert _body(vc) == {
        "data": {
            "items": [
                {
                    "config": {
                        "url": "https://<vcenter-fqdn>",
                        "userName": "<vc-user>",
                        "password": base64.b64encode(_VC_PASSWORD.encode()).decode(),
                    }
                }
            ]
        }
    }
    assert _body(sso) == {
        "data": {
            "items": [
                {"config": {"lookupServiceUrl": "https://<vcenter-fqdn>", "providerType": "PSC"}}
            ]
        }
    }
    assert result.result["status"] == "done"
    assert [s["state"] for s in result.result["steps"]] == ["done", "done"]
    assert result.result["restart_needed"] is True
    _no_secret_in(result.result, _VC_PASSWORD)


async def test_vcenter_set_wrong_password_reaches_the_answer_without_the_password(
    hcx: HcxConnector,
) -> None:
    echoed = base64.b64encode(_VC_PASSWORD.encode()).decode()
    with capture_logs() as logs:
        async with respx.mock(assert_all_called=False) as mock:
            mock.post(_a(_VCENTER)).respond(
                400,
                json={"errors": [{"message": f"Invalid credentials for <vc-user> ({echoed})"}]},
            )
            sso = mock.post(_a(_LOOKUP)).respond(200, json=_items({}))
            result = await _run("hcx.manager.vcenter.set", _VC_PARAMS)

    assert result.status == "error"
    assert result.extras["http_status"] == 400
    assert "Invalid credentials" in result.extras["upstream_message"]
    assert not sso.called
    _no_secret_in(result.model_dump(), _VC_PASSWORD)
    _no_secret_in(logs, _VC_PASSWORD)


@pytest.mark.parametrize(
    ("message", "hint"),
    [
        ("Unable to connect to https://<vcenter-fqdn>: connection refused", "could not reach"),
        ("PKIX path building failed: certificate not trusted", "hcx.certificate.trust"),
    ],
)
async def test_vcenter_set_unreachable_or_untrusted_vcenter_names_the_next_step(
    hcx: HcxConnector, message: str, hint: str
) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_a(_VCENTER)).respond(500, json={"errors": [{"message": message}]})
        result = await _run("hcx.manager.vcenter.set", _VC_PARAMS)
    assert result.status == "error"
    assert message.split(":")[0] in result.extras["upstream_message"]
    assert hint in result.extras["remediation"]


async def test_vcenter_set_failing_at_the_sso_step_lists_the_steps(hcx: HcxConnector) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_a(_VCENTER)).respond(200, json=_items({"url": "https://<vcenter-fqdn>"}))
        mock.post(_a(_LOOKUP)).respond(
            400, json={"errors": [{"message": "Lookup service URL is not valid"}]}
        )
        result = await _run("hcx.manager.vcenter.set", _VC_PARAMS)

    assert result.status == "ok", result.error
    assert result.result["status"] == "failed"
    assert result.result["steps"] == [
        {"step": "register_vcenter", "state": "done", "error": None},
        {
            "step": "register_lookup_service",
            "state": "failed",
            "error": "HTTP 400: Lookup service URL is not valid",
        },
    ]


async def test_nsx_set_registers_nsx(hcx: HcxConnector) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        nsx = mock.post(_a(_NSX)).respond(
            200,
            json=_items({"url": "https://<nsx-fqdn>", "userName": "<nsx-user>", "version": "9.0"}),
        )
        result = await _run("hcx.manager.nsx.set", _NSX_PARAMS)
    assert result.status == "ok", result.error
    config = _body(nsx)["data"]["items"][0]["config"]
    assert config["password"] == base64.b64encode(_NSX_PASSWORD.encode()).decode()
    assert result.result["nsx"]["version"] == "9.0"
    _no_secret_in(result.result, _NSX_PASSWORD)


async def test_nsx_set_wrong_password_and_untrusted_certificate(hcx: HcxConnector) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_a(_NSX)).respond(
            200,
            json={
                "errors": [{"message": f"SSL handshake failed; password {_NSX_PASSWORD} rejected"}]
            },
        )
        result = await _run("hcx.manager.nsx.set", _NSX_PARAMS)
    assert result.status == "error"
    assert "SSL handshake failed" in result.extras["upstream_message"]
    assert "hcx.certificate.trust" in result.extras["remediation"]
    _no_secret_in(result.model_dump(), _NSX_PASSWORD)


# ---------------------------------------------------------------------------
# hcx.manager.location.set
# ---------------------------------------------------------------------------

_CITY_A = {
    "city": "<city>",
    "cityAscii": "<city>",
    "country": "<country-a>",
    "province": "<province>",
    "latitude": 1.0,
    "longitude": 2.0,
}
_CITY_B = {**_CITY_A, "country": "<country-b>", "latitude": 3.0}


async def test_location_set_puts_the_one_matching_city(hcx: HcxConnector) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        search = mock.get(_a(_CITIES)).respond(200, json={"items": [_CITY_A, _CITY_B]})
        put = mock.put(_a(_LOCATION)).respond(204)
        result = await _run(
            "hcx.manager.location.set", {"city": "<city>", "country": "<country-b>"}
        )
    assert result.status == "ok", result.error
    assert search.calls[0].request.url.params["searchString"] == "<city>"
    assert _body(put) == _CITY_B
    assert result.result["location"]["country"] == "<country-b>"


@pytest.mark.parametrize(
    ("rows", "words"),
    [([], "has no city"), ([_CITY_A, _CITY_B], "has 2 cities")],
)
async def test_location_set_refuses_a_city_not_found_or_found_twice(
    hcx: HcxConnector, rows: list[dict[str, Any]], words: str
) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_CITIES)).respond(200, json={"items": rows})
        put = mock.put(_a(_LOCATION)).respond(204)
        result = await _run("hcx.manager.location.set", {"city": "<city>"})
    assert result.status == "error"
    assert words in result.extras["exception_message"]
    assert not put.called


# ---------------------------------------------------------------------------
# hcx.manager.role_mapping.set
# ---------------------------------------------------------------------------


async def test_role_mapping_set_keeps_other_roles_and_the_9_1_names(hcx: HcxConnector) -> None:
    current = [
        {"role": "Migration Admin", "userGroups": ["<domain>\\<old>"]},
        {"role": "Migration Appliance Admin", "userGroups": ["<domain>\\<appliance>"]},
    ]
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_ROLES)).respond(200, json=current)
        put = mock.put(_a(_ROLES)).respond(
            200, json={"isSuccess": True, "message": "<ok>", "httpStatusCode": 200}
        )
        result = await _run(
            "hcx.manager.role_mapping.set",
            {"system_admin_groups": ["<domain>\\<admins>"], "enterprise_admin_groups": []},
        )
    assert result.status == "ok", result.error
    assert _body(put) == [
        {"role": "Migration Admin", "userGroups": ["<domain>\\<admins>"]},
        {"role": "Migration Appliance Admin", "userGroups": ["<domain>\\<appliance>"]},
        {"role": "Migration User", "userGroups": []},
    ]
    assert result.result["message"] == "<ok>"


async def test_role_mapping_set_with_a_group_sso_does_not_know_is_refused(
    hcx: HcxConnector,
) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_ROLES)).respond(200, json=[])
        put = mock.put(_a(_ROLES)).respond(
            200,
            json={
                "isSuccess": False,
                "message": "Group <domain>\\<nobody> not found in SSO",
                "httpStatusCode": 400,
            },
        )
        result = await _run(
            "hcx.manager.role_mapping.set", {"system_admin_groups": ["<domain>\\<nobody>"]}
        )
    assert result.status == "error"
    assert result.extras["http_status"] == 400
    assert "not found in SSO" in result.extras["upstream_message"]
    assert _body(put) == [{"role": "System Administrator", "userGroups": ["<domain>\\<nobody>"]}]


# ---------------------------------------------------------------------------
# hcx.manager.activate
# ---------------------------------------------------------------------------


async def test_activate_on_4x_posts_the_key_and_never_returns_it(hcx: HcxConnector) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_HCX_CONFIG)).mock(
            side_effect=[
                httpx.Response(200, json={"data": {"items": []}}),
                httpx.Response(
                    200,
                    json=_items({"url": "https://connect.hcx.vmware.com", "activationKey": _KEY}),
                ),
            ]
        )
        post = mock.post(_a(_HCX_CONFIG)).respond(200, json=_items({"activationKey": _KEY}))
        result = await _run("hcx.manager.activate", {"activation_key_secret_ref": "hcx/activation"})
    assert result.status == "ok", result.error
    assert _body(post) == {
        "data": {
            "items": [{"config": {"url": "https://connect.hcx.vmware.com", "activationKey": _KEY}}]
        }
    }
    assert result.result["status"] == "activated"
    assert result.result["activation"]["activated"] is True
    _no_secret_in(result.result, _KEY)


async def test_activate_with_a_wrong_or_used_key_is_refused_without_the_key(
    hcx: HcxConnector,
) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_HCX_CONFIG)).respond(200, json={"data": {"items": []}})
        mock.post(_a(_HCX_CONFIG)).respond(
            400, json={"errors": [{"message": f"Activation key {_KEY} is already in use"}]}
        )
        result = await _run("hcx.manager.activate", {"activation_key_secret_ref": "hcx/activation"})
    assert result.status == "error"
    assert "already in use" in result.extras["upstream_message"]
    _no_secret_in(result.model_dump(), _KEY)


async def test_activate_reports_vcf_licensing_as_already_active_and_reads_no_key(
    hcx: HcxConnector,
) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_HCX_CONFIG)).respond(404, json={"message": "not found"})
        mock.get(_a(_ACTIVATION_STATUS)).respond(
            200, json={"data": {"activationDone": True, "activationType": "VCF"}}
        )
        post = mock.post(_a(_HCX_CONFIG)).respond(200, json={})
        with patch("meho_backplane.connectors.hcx.manager_setup.load_param_secret_data") as read:
            result = await _run(
                "hcx.manager.activate", {"activation_key_secret_ref": "hcx/activation"}
            )
    assert result.status == "ok", result.error
    assert result.result["status"] == "already_active"
    assert result.result["activation"]["type"] == "VCF"
    assert not post.called
    assert not read.called


async def test_activate_on_9x_that_is_not_active_names_vcenter_licensing(
    hcx: HcxConnector,
) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_HCX_CONFIG)).respond(404, json={"message": "not found"})
        mock.get(_a(_ACTIVATION_STATUS)).respond(200, json={"data": {"activationDone": False}})
        result = await _run("hcx.manager.activate", {"activation_key_secret_ref": "hcx/activation"})
    assert result.status == "error"
    assert "VCF licensing" in result.extras["exception_message"]
    assert "vCenter" in result.extras["remediation"]


# ---------------------------------------------------------------------------
# hcx.manager.restart and hcx.manager.service.status
# ---------------------------------------------------------------------------


def _login(mock: respx.MockRouter) -> None:
    mock.post(f"{_MAIN}/hybridity/api/sessions").respond(
        200, headers={"x-hm-authorization": _TOKEN}
    )


def _with_vcenter(mock: respx.MockRouter) -> None:
    mock.get(_a(_VCENTER)).respond(200, json=_items({"url": "https://<vcenter-fqdn>"}))


async def test_restart_while_migrations_run_is_refused(hcx: HcxConnector) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _with_vcenter(mock)
        mock.get(_a(_APPENGINE_STATUS)).respond(200, json={"result": "RUNNING"})
        query = mock.post(f"{_MAIN}{_MIGRATIONS}").respond(
            200,
            json={
                "items": [
                    {"migrationId": "<migration-1>", "progress": {"isDone": False}},
                    {"migrationId": "<migration-2>", "progress": {"isDone": True}},
                ]
            },
        )
        action = mock.post(_a(_APPENGINE)).respond(200, json={"result": "STOPPED"})
        result = await _run("hcx.manager.restart")
    assert result.status == "error"
    assert "<migration-1>" in result.extras["exception_message"]
    assert "<migration-2>" not in result.extras["exception_message"]
    assert query.calls[0].request.url.params["action"] == "query"
    assert not action.called


async def test_restart_stops_then_starts_and_answers_accepted(hcx: HcxConnector) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _with_vcenter(mock)
        mock.get(_a(_APPENGINE_STATUS)).respond(200, json={"result": "RUNNING"})
        mock.post(f"{_MAIN}{_MIGRATIONS}").respond(200, json={"items": []})
        action = mock.post(_a(_APPENGINE)).mock(
            side_effect=[
                httpx.Response(200, json={"result": "STOPPED", "operationStatus": "SUCCESS"}),
                httpx.Response(200, json={"result": "STARTING", "operationStatus": "SUCCESS"}),
            ]
        )
        result = await _run("hcx.manager.restart")
    assert result.status == "ok", result.error
    assert [c.request.url.params["action"] for c in action.calls] == ["stop", "start"]
    answer = result.result
    assert answer["status"] == "accepted"
    assert answer["job_id"] is None
    assert answer["service_state"] == "STARTING"
    assert answer["status_read"] == "hcx.manager.service.status"
    assert answer["migration_check"]["state"] == "passed"
    assert [s["state"] for s in answer["steps"]] == ["done", "done"]


async def test_restart_that_already_runs_starts_nothing(hcx: HcxConnector) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_APPENGINE_STATUS)).respond(200, json={"result": "STARTING"})
        action = mock.post(_a(_APPENGINE)).respond(200, json={})
        result = await _run("hcx.manager.restart")
    assert result.status == "ok", result.error
    assert result.result["status"] == "already_running"
    assert not action.called


async def test_restart_on_a_new_manager_without_vcenter_skips_the_migration_check(
    hcx: HcxConnector,
) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_VCENTER)).respond(200, json={"data": {"items": []}})
        mock.get(_a(_APPENGINE_STATUS)).respond(200, json={"result": "STOPPED"})
        query = mock.post(f"{_MAIN}{_MIGRATIONS}").respond(200, json={"items": []})
        action = mock.post(_a(_APPENGINE)).respond(200, json={"result": "RUNNING"})
        result = await _run("hcx.manager.restart")
    assert result.status == "ok", result.error
    assert result.result["migration_check"]["state"] == "skipped"
    assert not query.called
    assert [c.request.url.params["action"] for c in action.calls] == ["start"]
    assert [s["state"] for s in result.result["steps"]] == ["skipped", "done"]


async def test_restart_that_cannot_check_migrations_is_refused(hcx: HcxConnector) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{_MAIN}/hybridity/api/sessions").respond(401)
        _with_vcenter(mock)
        mock.get(_a(_APPENGINE_STATUS)).respond(200, json={"result": "RUNNING"})
        action = mock.post(_a(_APPENGINE)).respond(200, json={})
        result = await _run("hcx.manager.restart")
    assert result.status == "error"
    assert "skip_migration_check" in result.extras["remediation"]
    assert not action.called


async def test_restart_failing_at_the_start_lists_the_steps(hcx: HcxConnector) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_APPENGINE_STATUS)).respond(200, json={"result": "RUNNING"})
        mock.post(_a(_APPENGINE)).mock(
            side_effect=[
                httpx.Response(200, json={"result": "STOPPED", "operationStatus": "SUCCESS"}),
                httpx.Response(500, json={"errors": [{"message": "appengine did not start"}]}),
            ]
        )
        result = await _run("hcx.manager.restart", {"skip_migration_check": True})
    assert result.status == "ok", result.error
    assert result.result["status"] == "failed"
    assert result.result["steps"] == [
        {"step": "stop", "state": "done", "error": None},
        {"step": "start", "state": "failed", "error": "HTTP 500: appengine did not start"},
    ]


async def test_restart_without_an_answer_at_the_stop_says_no_answer(hcx: HcxConnector) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_APPENGINE_STATUS)).respond(200, json={"result": "RUNNING"})
        mock.post(_a(_APPENGINE)).mock(side_effect=httpx.ReadTimeout("timed out"))
        result = await _run("hcx.manager.restart", {"skip_migration_check": True})
    assert result.status == "ok", result.error
    assert result.result["status"] == "no_answer"
    assert [s["state"] for s in result.result["steps"]] == ["unknown", "not_run"]


async def test_service_status_logs_in_on_every_read_and_then_hcx_about_answers(
    hcx: HcxConnector,
) -> None:
    _SESSION_LOADER.reset_mock()
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_APPENGINE_STATUS)).mock(
            side_effect=[
                httpx.Response(200, json={"result": "STARTING"}),
                httpx.Response(200, json={"result": "RUNNING", "operationStatus": "SUCCESS"}),
            ]
        )
        first = await _run("hcx.manager.service.status")
        second = await _run("hcx.manager.service.status")
        _login(mock)
        mock.post(f"{_MAIN}/hybridity/api/hcspAppliances").respond(
            200, json=[{"version": "4.11.0", "systemType": "HCX Connector"}]
        )
        mock.post(f"{_MAIN}/hybridity/api/service/inventory/cloud/list").respond(
            200, json={"data": {"items": []}}
        )
        about = await _run("hcx.about")
    assert first.result["state"] == "STARTING"
    assert first.result["running"] is False
    assert second.result["running"] is True
    assert _SESSION_LOADER.await_count >= 2
    assert about.status == "ok", about.error
    assert about.result["version"] == "4.11.0"


async def test_service_status_says_not_reachable_when_the_manager_does_not_answer(
    hcx: HcxConnector,
) -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_APPENGINE_STATUS)).mock(side_effect=httpx.ConnectError("refused"))
        result = await _run("hcx.manager.service.status")
    assert result.status == "ok", result.error
    assert result.result["state"] == "not_reachable"
    assert result.result["reachable"] is False
    assert result.result["checked_at"]


# ---------------------------------------------------------------------------
# Flight recorder
# ---------------------------------------------------------------------------


async def _enable_tracing() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(id=_TENANT_ID, slug="hcx-setup", name="hcx-setup", flight_recorder_enabled=True)
        )
        await session.commit()
    reset_flight_recorder_config_cache_for_testing()


async def _recorded_spans(audit_id: Any) -> list[DispatchTraceSpan]:
    async with get_sessionmaker()() as session:
        trace = (
            await session.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one()
        spans = await session.execute(
            select(DispatchTraceSpan).where(DispatchTraceSpan.trace_id == trace.id)
        )
        return list(spans.scalars().all())


async def test_the_recorded_trace_holds_no_password_or_key(hcx: HcxConnector) -> None:
    await _enable_tracing()
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_a(_HCX_CONFIG)).mock(
            side_effect=[
                httpx.Response(200, json={"data": {"items": []}}),
                httpx.Response(200, json=_items({"activationKey": _KEY})),
            ]
        )
        mock.post(_a(_HCX_CONFIG)).respond(200, json=_items({"activationKey": _KEY}))
        result = await _run("hcx.manager.activate", {"activation_key_secret_ref": "hcx/activation"})
    assert result.status == "ok", result.error
    spans = await _recorded_spans(result.audit_id)
    vendor_calls = [span for span in spans if span.span_kind == "vendor_call"]
    assert len(vendor_calls) == 3
    _no_secret_in([span.attributes for span in spans], _KEY, _ADMIN_PASSWORD)


def _json_error(text: str) -> httpx.Response:
    return httpx.Response(400, json={"errors": [{"message": text}]})


def _text_error(text: str) -> httpx.Response:
    return httpx.Response(400, text=text, headers={"content-type": "text/plain"})


@pytest.mark.parametrize(
    ("echo", "message_kept"), [(_json_error, True), (_text_error, False)], ids=["json", "text"]
)
async def test_the_recorded_trace_of_a_failed_call_holds_no_echoed_password(
    hcx: HcxConnector, echo: Any, message_kept: bool
) -> None:
    """A vendor error that echoes the sent password (base64) is cut before the trace.

    The JSON error keeps its message in the trace, without the password.
    A body that is not JSON is omitted by the shared trace redaction.
    """
    await _enable_tracing()
    echoed = base64.b64encode(_VC_PASSWORD.encode()).decode()
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_a(_VCENTER)).mock(
            return_value=echo(f"Invalid credentials for <vc-user> ({echoed})")
        )
        result = await _run("hcx.manager.vcenter.set", _VC_PARAMS)
    assert result.status == "error"
    assert result.extras["http_status"] == 400
    spans = await _recorded_spans(result.audit_id)
    vendor_calls = [span for span in spans if span.span_kind == "vendor_call"]
    assert len(vendor_calls) == 1
    recorded = json.dumps(vendor_calls[0].attributes, default=str)
    assert ("Invalid credentials" in recorded) is message_kept
    _no_secret_in([span.attributes for span in spans], _VC_PASSWORD, _ADMIN_PASSWORD)
