# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the FortiGate connector: registration, token login, targets, detection, reads.

A respx-mocked FortiGate answers from recorded, made-up answers
(``tests/_fortios_fixtures.py``). No live device is called.
"""

from __future__ import annotations

import json
import ssl
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.fortios import (
    FORTIOS_CONNECTOR_ID,
    FORTIOS_GROUP_TEXTS,
    FORTIOS_IMPL_ID,
    FORTIOS_PRODUCT,
    FORTIOS_TYPED_OPS,
    FORTIOS_VERSION,
    FortiOSConnector,
    register_fortios_typed_operations,
)
from meho_backplane.connectors.fortios._errors import FortiOSScopeError, FortiOSTargetError
from meho_backplane.connectors.fortios.target import require_scope, resolve_scope
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from tests import _fortios_fixtures as fx
from tests._fortios_support import (
    FortiTarget,
    make_operator,
    no_retry_wait,
    reset_module_state,
    settings_env,
    stub_embedding,
    token_loader,
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from settings_env(monkeypatch)


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    yield from reset_module_state()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    return stub_embedding(monkeypatch)


@pytest.fixture
async def session() -> Any:
    async with get_sessionmaker()() as s:
        yield s


async def _register() -> FortiOSConnector:
    await register_fortios_typed_operations()
    instance = get_or_create_connector_instance(FortiOSConnector)
    assert isinstance(instance, FortiOSConnector)
    instance._token_loader = token_loader
    return instance


async def _dispatch(op_id: str, fake: fx.FakeFortiGate, target: FortiTarget | None = None) -> Any:
    async with respx.mock(base_url=fx.BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake)
        return await dispatch(
            operator=make_operator(),
            connector_id=FORTIOS_CONNECTOR_ID,
            op_id=op_id,
            target=target or FortiTarget(),
            params={},
        )


def _text(result: Any) -> str:
    """The error and extras of a result, as one text."""
    return f"{result.error} {json.dumps(dict(result.extras or {}), default=str)}"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_package_registers_versioned_connector_and_wildcard() -> None:
    registry = all_connectors_v2()
    assert registry[(FORTIOS_PRODUCT, FORTIOS_VERSION, FORTIOS_IMPL_ID)] is FortiOSConnector
    assert FORTIOS_CONNECTOR_ID == "fortigate-rest-7.x"
    assert FortiOSConnector.product == "fortigate"
    assert FortiOSConnector.supported_version_range == ">=7.2,<8.0"


@pytest.mark.parametrize("version", ["7.2.8", "7.4.4", "7.6.0"])
def test_each_covered_version_resolves_to_the_connector(version: str) -> None:
    assert resolve_connector(FortiTarget(version=version)) is FortiOSConnector


@pytest.mark.asyncio
async def test_registered_ops_are_safe_device_reads(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_fortios_typed_operations()
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.impl_id == FORTIOS_IMPL_ID)
            )
        )
        .scalars()
        .all()
    )
    assert {r.op_id for r in rows} == {
        "fortios.system.status",
        "fortios.ha.status",
        "fortios.vdom.list",
        "fortios.central_management.status",
        "fortios.license.status",
        "fortios.resource.usage",
    }
    assert {r.op_id for r in rows} == {op.op_id for op in FORTIOS_TYPED_OPS}
    for row in rows:
        assert row.source_kind == "typed"
        assert row.safety_level == "safe"
        assert row.requires_approval is False
        assert row.parameter_schema == {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "examples": [{}],
        }
        assert "vdom" not in json.dumps(row.parameter_schema)
        assert {"when_to_call", "output_shape", "next_step"} <= set(row.llm_instructions)


def test_every_group_has_a_text() -> None:
    assert set(FORTIOS_GROUP_TEXTS) == {
        "fortios-device",
        "fortios-network",
        "fortios-policy",
        "fortios-vpn",
        "fortios-traffic",
    }
    assert all(text.strip() for text in FORTIOS_GROUP_TEXTS.values())


# ---------------------------------------------------------------------------
# The six reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_status_reads_model_serial_version_and_licence(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    fake = fx.FakeFortiGate()
    result = await _dispatch("fortios.system.status", fake)
    assert result.status == "ok", result.error
    body = result.result
    assert body["model"] == "FGT60F"
    assert body["platform"] == "hardware"
    assert body["serial"] == fx.SERIAL_A
    assert (body["version"], body["build"]) == ("v7.4.4", 2662)
    assert body["forticare_status"] == "registered"
    assert body["vm_licence_status"] is None
    assert body["version_warning"] is None
    assert fake.paths() == [
        "monitor/system/status",
        "monitor/system/firmware",
        "monitor/license/status",
    ]


@pytest.mark.asyncio
async def test_every_call_sends_the_token_and_the_management_vdom(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    fake = fx.FakeFortiGate()
    result = await _dispatch("fortios.ha.status", fake)
    assert result.status == "ok", result.error
    assert fake.calls
    for request in fake.calls:
        assert request.headers["authorization"] == f"Bearer {fx.TOKEN}"
        assert request.url.params.get_list("vdom") == ["root"]
        assert "cookie" not in request.headers


@pytest.mark.asyncio
async def test_ha_status_names_the_primary_and_says_in_sync(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    result = await _dispatch("fortios.ha.status", fx.FakeFortiGate())
    assert result.status == "ok", result.error
    body = result.result
    assert (body["mode"], body["group_name"]) == ("a-p", "example-cluster")
    assert body["member_count"] == 2
    assert [m["role"] for m in body["members"]] == ["primary", "secondary"]
    assert body["members"][0]["serial"] == fx.SERIAL_A
    assert body["in_sync"] is True
    assert body["device_id"] == f"ha:{fx.SERIAL_A},{fx.SERIAL_B}"

    answers = {**fx.DEVICE_ANSWERS, "monitor/system/ha-checksums": fx.CHECKSUMS_DIFFER}
    result = await _dispatch("fortios.ha.status", fx.FakeFortiGate(answers))
    assert result.result["in_sync"] is False


@pytest.mark.asyncio
async def test_vdom_list_central_management_licence_and_usage(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    vdoms = await _dispatch("fortios.vdom.list", fx.FakeFortiGate())
    assert vdoms.result["count"] == 2
    assert [v["name"] for v in vdoms.result["vdoms"]] == ["root", "vdom-a"]

    fmg = await _dispatch("fortios.central_management.status", fx.FakeFortiGate())
    assert fmg.result["fortimanager_controls"] is False
    answers = {**fx.DEVICE_ANSWERS, "monitor/system/central-management/status": fx.FMG_CONTROLS}
    fmg = await _dispatch("fortios.central_management.status", fx.FakeFortiGate(answers))
    assert fmg.result["fortimanager_controls"] is True

    licences = await _dispatch("fortios.license.status", fx.FakeFortiGate())
    services = {row["service"]: row for row in licences.result["licences"]}
    assert services["forticare"]["status"] == "registered"
    assert services["forticare.hardware"]["expires"].startswith("2030-01-01")
    assert (services["vdom"]["used"], services["vdom"]["max"]) == (2, 10)
    rendered = json.dumps(licences.result)
    assert "someone@example.invalid" not in rendered
    assert "Example Company" not in rendered

    fake = fx.FakeFortiGate()
    usage = await _dispatch("fortios.resource.usage", fake)
    assert (usage.result["cpu"], usage.result["mem"], usage.result["session"]) == (12, 40, 1500)
    assert usage.result["session6"] is None
    assert fake.calls[0].url.params["scope"] == "global"


@pytest.mark.asyncio
async def test_a_read_on_an_untested_version_adds_a_warning(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    result = await _dispatch("fortios.vdom.list", fx.FakeFortiGate(version="v7.8.1"))
    assert result.status == "ok", result.error
    assert "not on the tested list" in result.result["version_warning"]


@pytest.mark.asyncio
async def test_a_read_on_fortios_8_is_refused(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    result = await _dispatch("fortios.vdom.list", fx.FakeFortiGate(version="v8.0.0"))
    assert result.status == "error"
    assert "FortiOS 8.0.0 is not supported" in _text(result)


@pytest.mark.asyncio
async def test_an_error_wrapper_in_a_200_answer_is_an_error(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    answers = {
        **fx.DEVICE_ANSWERS,
        "cmdb/system/vdom": httpx.Response(
            200, json=fx.error_answer(500, error=-651, cli_error="made-up refusal")
        ),
    }
    result = await _dispatch("fortios.vdom.list", fx.FakeFortiGate(answers))
    assert result.status == "error"
    assert "status 'error' for cmdb/system/vdom: error -651; made-up refusal" in _text(result)


# ---------------------------------------------------------------------------
# Targets: device or VDOM, the VDOM on every call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_device_operation_on_a_vdom_target_is_refused_without_a_call(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    fake = fx.FakeFortiGate()
    result = await _dispatch("fortios.ha.status", fake, FortiTarget(scope="vdom", vdom="vdom-a"))
    assert result.status == "error"
    assert "Call it on the device target" in _text(result)
    assert fake.calls == []


def test_require_scope_refuses_both_wrong_kinds() -> None:
    vdom_target = FortiTarget(scope="vdom", vdom="vdom-a")
    with pytest.raises(FortiOSScopeError, match="device target of this FortiGate"):
        require_scope(vdom_target, "fortios.example.device_op", "device")
    with pytest.raises(FortiOSScopeError, match="VDOM target of the VDOM you mean"):
        require_scope(FortiTarget(), "fortios.example.vdom_op", "vdom")
    assert require_scope(vdom_target, "fortios.example.vdom_op", "vdom").vdom == "vdom-a"


@pytest.mark.asyncio
async def test_a_vdom_target_sends_its_vdom_and_no_parameter_reaches_another() -> None:
    connector = FortiOSConnector(token_loader=token_loader)
    target = FortiTarget(scope="vdom", vdom="vdom-a")
    fake = fx.FakeFortiGate({"cmdb/firewall/address": [fx.ADDRESS]})
    async with respx.mock(base_url=fx.BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake)
        answer = await connector.api_get(target, make_operator(), "cmdb/firewall/address")
        for query in ({"vdom": "root"}, {"VDOM": "*"}, {"global": "1"}):
            with pytest.raises(FortiOSScopeError, match="sends the target's VDOM"):
                await connector.api_get(
                    target, make_operator(), "cmdb/firewall/address", query=query
                )
        with pytest.raises(FortiOSScopeError):
            await connector.api_write(
                target, make_operator(), "PUT", "cmdb/firewall/address/x", query={"vdom": "root"}
            )
    assert answer.vdom == "vdom-a"
    assert len(fake.calls) == 1
    assert fake.calls[0].url.params.get_list("vdom") == ["vdom-a"]
    await connector.aclose()


@pytest.mark.parametrize(
    ("extras", "message"),
    [
        ({}, "extras.scope must be 'device'"),
        ({"scope": "cluster"}, "extras.scope must be 'device'"),
        ({"scope": "vdom"}, "extras.vdom must be a VDOM name"),
        ({"scope": "vdom", "vdom": "bad name"}, "extras.vdom must be a VDOM name"),
        ({"scope": "device", "vdom": "vdom-a"}, "takes no extras.vdom"),
        ({"scope": "device", "management_vdom": "x" * 32}, "extras.management_vdom"),
    ],
)
def test_target_settings_are_checked(extras: dict[str, Any], message: str) -> None:
    with pytest.raises(FortiOSTargetError, match=message):
        resolve_scope(FortiTarget(extras=extras))


def test_device_target_uses_root_or_its_management_vdom() -> None:
    assert resolve_scope(FortiTarget()).vdom == "root"
    target = FortiTarget(extras={"scope": "device", "management_vdom": "mgmt"})
    assert (resolve_scope(target).kind, resolve_scope(target).vdom) == ("device", "mgmt")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


async def _fingerprint(fake: fx.FakeFortiGate, target: FortiTarget | None = None) -> Any:
    connector = FortiOSConnector(token_loader=token_loader)
    async with respx.mock(base_url=fx.BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake)
        fp = await connector.fingerprint(target or FortiTarget(), make_operator())
    await connector.aclose()
    return fp


@pytest.mark.asyncio
async def test_detection_of_a_hardware_ha_pair() -> None:
    fp = await _fingerprint(fx.FakeFortiGate())
    assert (fp.vendor, fp.product, fp.reachable) == ("fortinet", "fortigate", True)
    assert (fp.version, fp.build, fp.edition) == ("7.4.4", "2662", "hardware")
    extras = dict(fp.extras)
    assert extras["model"] == "FGT60F"
    assert extras["serial"] == fx.SERIAL_A
    assert extras["vdoms"] == ["root", "vdom-a"]
    assert [m["serial"] for m in extras["ha_members"]] == [fx.SERIAL_A, fx.SERIAL_B]
    assert extras["device_id"] == f"ha:{fx.SERIAL_A},{fx.SERIAL_B}"
    assert extras["fortimanager_controls"] is False
    assert extras["version_warning"] is None
    assert fx.TOKEN not in json.dumps(fp.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_detection_of_a_single_vm_unit() -> None:
    answers = {
        **fx.DEVICE_ANSWERS,
        "monitor/system/status": fx.STATUS_VM,
        "monitor/system/ha-peer": fx.HA_PEERS_SINGLE,
        "monitor/system/central-management/status": fx.FMG_CONTROLS,
    }
    fp = await _fingerprint(fx.FakeFortiGate(answers, serial=fx.VM_SERIAL))
    assert fp.reachable is True
    assert fp.edition == "vm"
    assert fp.extras["device_id"] == f"serial:{fx.VM_SERIAL}"
    assert fp.extras["ha_members"] == []
    assert fp.extras["fortimanager_controls"] is True


@pytest.mark.asyncio
async def test_detection_on_a_vdom_target_sends_the_vdom_and_skips_what_it_may_not_read() -> None:
    answers = {
        **fx.DEVICE_ANSWERS,
        "monitor/system/ha-peer": httpx.Response(403, json=fx.error_answer(403)),
        "cmdb/system/vdom": httpx.Response(403, json=fx.error_answer(403)),
    }
    fake = fx.FakeFortiGate(answers)
    fp = await _fingerprint(fake, FortiTarget(scope="vdom", vdom="vdom-a"))
    assert fp.reachable is True
    assert fp.extras["ha_members"] is None
    assert fp.extras["vdoms"] is None
    assert (fp.extras["scope"], fp.extras["vdom"]) == ("vdom", "vdom-a")
    assert {tuple(c.url.params.get_list("vdom")) for c in fake.calls} == {("vdom-a",)}


@pytest.mark.parametrize(
    ("version", "message"),
    [
        ("v8.0.0", "FortiOS 8.0.0 is not supported"),
        ("v7.0.15", "FortiOS 7.0.15 is not supported"),
    ],
)
@pytest.mark.asyncio
async def test_detection_refuses_versions_outside_7_2_to_7_x(version: str, message: str) -> None:
    fp = await _fingerprint(fx.FakeFortiGate(version=version))
    assert fp.reachable is False
    assert fp.version is None
    assert fp.extras["refused"] is True
    assert message in fp.extras["error"]
    assert fp.extras["detected_version"] == version


@pytest.mark.asyncio
async def test_detection_refuses_a_model_that_is_not_a_fortigate() -> None:
    answers = {**fx.DEVICE_ANSWERS, "monitor/system/status": fx.STATUS_NOT_FORTIGATE}
    fp = await _fingerprint(fx.FakeFortiGate(answers))
    assert fp.reachable is False
    assert fp.extras["refused"] is True
    assert "not a FortiGate" in fp.extras["error"]


@pytest.mark.asyncio
async def test_detection_warns_on_an_untested_7_x_version() -> None:
    fp = await _fingerprint(fx.FakeFortiGate(version="v7.8.0"))
    assert fp.reachable is True
    assert fp.version == "7.8.0"
    assert "not on the tested list" in fp.extras["version_warning"]


# ---------------------------------------------------------------------------
# Clear messages: wrong token, missing rights, failed TLS check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_wrong_token_says_token_refused_and_is_sent_once(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    fake = fx.FakeFortiGate(
        {"cmdb/system/vdom": httpx.Response(401, json=fx.error_answer(401, error=-1))}
    )
    result = await _dispatch("fortios.vdom.list", fake)
    assert result.status == "error"
    assert result.extras["error_code"] == "connector_auth_failed"
    assert result.extras["cause"] == "token_refused"
    assert "refused the API token (HTTP 401)" in result.extras["raw_message"]
    assert "store it as api_token" in result.extras["remediation"]
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_missing_rights_say_the_profile_lacks_rights(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    fake = fx.FakeFortiGate(
        {"cmdb/system/vdom": httpx.Response(403, json=fx.error_answer(403, error=-14))}
    )
    result = await _dispatch("fortios.vdom.list", fake)
    assert result.status == "error"
    assert result.extras["cause"] == "missing_rights"
    assert "lacks the rights for this call" in result.extras["raw_message"]
    assert len(fake.calls) == 1


def _tls_failure() -> httpx.ConnectError:
    try:
        raise ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] made up")
    except ssl.SSLCertVerificationError as ssl_exc:
        error = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] made up")
        error.__cause__ = ssl_exc
        return error


@pytest.mark.asyncio
async def test_a_failed_tls_check_names_tls_ca_pin_and_tls_server_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = FortiOSConnector(token_loader=token_loader)
    no_retry_wait(connector, monkeypatch)
    async with respx.mock(base_url=fx.BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=_tls_failure())
        fp = await connector.fingerprint(FortiTarget(), make_operator())
        probe = await connector.probe(FortiTarget())
    assert fp.reachable is False
    assert "TLS check failed" in fp.extras["error"]
    assert "tls_ca_pin" in fp.extras["error"] and "tls_server_name" in fp.extras["error"]
    assert probe.ok is False and "TLS check failed" in str(probe.reason)
    await connector.aclose()


@pytest.mark.asyncio
async def test_detection_with_a_wrong_token_is_not_reachable() -> None:
    fake = fx.FakeFortiGate(
        {"monitor/system/status": httpx.Response(401, json=fx.error_answer(401))}
    )
    fp = await _fingerprint(fake)
    assert fp.reachable is False
    assert "refused the API token" in fp.extras["error"]
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_auth_headers_refuse_other_auth_models() -> None:
    connector = FortiOSConnector(token_loader=token_loader)
    target = FortiTarget()
    target.auth_model = "per_user"
    with pytest.raises(NotImplementedError, match="per_user"):
        await connector.auth_headers(target, make_operator())


@pytest.mark.asyncio
async def test_the_token_comes_from_the_api_token_field(monkeypatch: pytest.MonkeyPatch) -> None:
    from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
    from meho_backplane.connectors.fortios import target as target_module

    monkeypatch.setattr(
        target_module, "load_vault_secret_data", AsyncMock(return_value={"api_token": " t-1\n"})
    )
    assert await target_module.load_token_from_vault(FortiTarget(), make_operator()) == "t-1"
    monkeypatch.setattr(
        target_module, "load_vault_secret_data", AsyncMock(return_value={"password": "x"})
    )
    with pytest.raises(VaultCredentialsReadError, match="no 'api_token' field"):
        await target_module.load_token_from_vault(FortiTarget(), make_operator())


@pytest.mark.asyncio
async def test_a_dropped_token_is_read_again() -> None:
    loader = AsyncMock(return_value=fx.TOKEN)
    connector = FortiOSConnector(token_loader=loader)
    target = FortiTarget()
    await connector.auth_headers(target, make_operator())
    await connector.auth_headers(target, make_operator())
    assert loader.await_count == 1
    await connector.invalidate_credentials(target)
    await connector.auth_headers(target, make_operator())
    assert loader.await_count == 2
