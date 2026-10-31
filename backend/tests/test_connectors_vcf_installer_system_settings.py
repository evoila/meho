# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests: Installer proxy, CEIP and system settings, certificate remove, resource calculation.

Each operation is called through its bound-method shim on
:class:`InstallerConnector`, with every vendor call mocked by ``respx``.
The ``connector`` fixture checks every answer against the operation's
published response schema (``jsonschema``, Draft 2020-12), so an answer
shape the schema does not allow fails the test that produced it. The Vault
read of a password is replaced by a fake. All names and ids are made up.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_installer import (
    InstallerConnector,
    InstallerTargetLike,
    _secret_refs,
    resources_calc,
)
from meho_backplane.connectors.vcf_installer.typed_ops_system import INSTALLER_SYSTEM_TYPED_OPS
from meho_backplane.operations._validate import validate_params

_BASE = "https://installer-a.test.invalid"
_PROXY_PASSWORD = "made-up-proxy-secret"
_SPEC_PASSWORD = "made-up-spec-secret"


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    name: str = "installer-a"
    host: str = "installer-a.test.invalid"
    port: int | None = 443
    secret_ref: str = "installer/installer-a"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    version: str | None = "9.1.0"
    id: UUID = field(default_factory=lambda: UUID(int=1))
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET_91 = _Target()
_TARGET_90 = _Target(version="9.0.2")


async def _stub_loader(_target: InstallerTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "admin@local", "password": "stub-login-password"}


@pytest.fixture(autouse=True)
def _installer_registry() -> Iterator[None]:
    clear_registry()
    register_connector_v2(
        product=InstallerConnector.product,
        version=InstallerConnector.version,
        impl_id=InstallerConnector.impl_id,
        cls=InstallerConnector,
    )
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _fake_vault(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, str]]:
    """Vault paths the tests may name, with made-up passwords."""
    store = {
        "kv/proxy": {"password": _PROXY_PASSWORD},
        "kv/vc-root": {"password": _SPEC_PASSWORD},
        "kv/empty": {},
    }

    async def _load(target: Any, _operator: Operator, *, param: str, mount: str) -> dict[str, Any]:
        return dict(store[target.secret_ref])

    monkeypatch.setattr(_secret_refs, "load_param_secret_data", _load)
    monkeypatch.setattr(resources_calc, "POLL_INTERVAL_SECONDS", 0.0)
    return store


def _check_answers_against_schemas(conn: InstallerConnector) -> None:
    """Wrap each operation shim so its answer must fit the operation's response schema."""
    for op in INSTALLER_SYSTEM_TYPED_OPS:
        assert op.response_schema is not None, op.op_id
        Draft202012Validator.check_schema(op.response_schema)
        validator = Draft202012Validator(op.response_schema)
        handler = getattr(conn, op.handler_attr)

        async def checked(
            *args: Any,
            _handler: Any = handler,
            _validator: Draft202012Validator = validator,
            _op_id: str = op.op_id,
            **kwargs: Any,
        ) -> dict[str, Any]:
            result: dict[str, Any] = await _handler(*args, **kwargs)
            problems = [f"{list(e.path)}: {e.message}" for e in _validator.iter_errors(result)]
            assert problems == [], (_op_id, problems)
            return result

        setattr(conn, op.handler_attr, checked)


@pytest.fixture
async def connector() -> Any:
    conn = InstallerConnector(credentials_loader=_stub_loader)
    _check_answers_against_schemas(conn)
    yield conn
    await conn.aclose()


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post("/v1/tokens").respond(201, json={"accessToken": "tok-1"})


def _no_tasks(mock: respx.MockRouter) -> respx.Route:
    return mock.get("/v1/tasks").respond(200, json={"elements": []})


# --------------------------------------------------------------- risk levels


def test_every_change_waits_for_approval_and_every_read_is_safe() -> None:
    expected = {
        "installer.system.proxy.get": ("safe", False),
        "installer.system.proxy.set": ("dangerous", True),
        "installer.system.settings.get": ("safe", False),
        "installer.system.settings.set": ("caution", True),
        "installer.system.trusted-certificates.remove": ("dangerous", True),
        "installer.sddc.resources.calculate": ("safe", False),
    }
    got = {op.op_id: (op.safety_level, op.requires_approval) for op in INSTALLER_SYSTEM_TYPED_OPS}
    assert got == expected


def test_operations_that_send_a_password_keep_bodies_out_of_traces() -> None:
    tags = {op.op_id: op.tags for op in INSTALLER_SYSTEM_TYPED_OPS}
    assert "credential" in tags["installer.system.proxy.set"]
    assert "credential" in tags["installer.sddc.resources.calculate"]


# --------------------------------------------------------------------- proxy


async def test_proxy_get_never_shows_the_password(connector: InstallerConnector) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.get("/v1/system/proxy-configuration").respond(
            200,
            json={
                "isEnabled": True,
                "host": "<proxy-host>",
                "port": 3128,
                "username": "proxy-user",
                "password": _PROXY_PASSWORD,
            },
        )
        result = await connector.system_proxy_get(_operator(), _TARGET_91, {})
    assert result["proxy"]["host"] == "<proxy-host>"
    assert "password" not in result["proxy"]
    assert _PROXY_PASSWORD not in json.dumps(result)


async def test_proxy_set_https_with_login_sends_the_vault_password_and_hides_it(
    connector: InstallerConnector,
) -> None:
    params = {
        "enabled": True,
        "host": "proxy.test.invalid",
        "port": 8443,
        "transfer_protocol": "HTTPS",
        "username": "proxy-user",
        "password_secret_ref": "kv/proxy",
    }
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        _no_tasks(mock)
        patch = mock.patch("/v1/system/proxy-configuration").respond(
            202, json={"id": "task-proxy-1", "status": "IN_PROGRESS"}
        )
        result = await connector.system_proxy_set(_operator(), _TARGET_91, params)

    sent = json.loads(patch.calls[0].request.content)
    assert sent == {
        "isEnabled": True,
        "host": "proxy.test.invalid",
        "port": 8443,
        "transferProtocol": "HTTPS",
        "isAuthenticated": True,
        "username": "proxy-user",
        "password": _PROXY_PASSWORD,
    }
    assert result["status"] == "accepted"
    assert result["task_id"] == "task-proxy-1"
    assert result["status_read"] == {
        "op_id": "installer.system.proxy.get",
        "params": {"task_id": "task-proxy-1"},
    }
    assert _PROXY_PASSWORD not in json.dumps(result)


async def test_proxy_set_off_sends_only_is_enabled_false(connector: InstallerConnector) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        _no_tasks(mock)
        patch = mock.patch("/v1/system/proxy-configuration").respond(
            202, json={"id": "task-proxy-2", "status": "PENDING"}
        )
        result = await connector.system_proxy_set(_operator(), _TARGET_91, {"enabled": False})
    assert json.loads(patch.calls[0].request.content) == {"isEnabled": False}
    assert result["status"] == "accepted"


async def test_proxy_set_unreachable_proxy_returns_the_vendor_text_without_the_password(
    connector: InstallerConnector,
) -> None:
    params = {
        "enabled": True,
        "host": "proxy.test.invalid",
        "port": 3128,
        "username": "proxy-user",
        "password_secret_ref": "kv/proxy",
    }
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        _no_tasks(mock)
        mock.patch("/v1/system/proxy-configuration").respond(
            400,
            json={
                "errorCode": "PROXY_NOT_REACHABLE",
                "message": f"Proxy proxy.test.invalid:3128 not reachable with {_PROXY_PASSWORD}",
            },
        )
        result = await connector.system_proxy_set(_operator(), _TARGET_91, params)
    assert result["status"] == "rejected"
    assert result["http_status"] == 400
    assert result["error_code"] == "PROXY_NOT_REACHABLE"
    assert _PROXY_PASSWORD not in json.dumps(result)


async def test_proxy_set_wrong_login_shows_up_as_a_failed_job(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.get("/v1/tasks/task-proxy-3").respond(
            200,
            json={
                "id": "task-proxy-3",
                "status": "FAILED",
                "errors": [{"errorCode": "PROXY_AUTH", "message": "Proxy login refused"}],
            },
        )
        mock.get("/v1/system/proxy-configuration").respond(200, json={"isEnabled": False})
        result = await connector.system_proxy_get(
            _operator(), _TARGET_91, {"task_id": "task-proxy-3"}
        )
    assert result["task"]["outcome"] == "failed"
    assert result["task"]["errors"] == ["Proxy login refused"]


async def test_proxy_set_starts_nothing_while_a_proxy_job_runs(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _login(mock)
        mock.get("/v1/tasks").respond(
            200,
            json={
                "elements": [
                    {"id": "task-old", "type": "PROXY_CONFIGURATION", "status": "SUCCESSFUL"},
                    {"id": "task-cp", "type": "VCF_OPS_CLOUD_PROXY", "status": "IN_PROGRESS"},
                    {"id": "task-run", "type": "PROXY_CONFIGURATION", "status": "In Progress"},
                ]
            },
        )
        patch = mock.patch("/v1/system/proxy-configuration")
        result = await connector.system_proxy_set(_operator(), _TARGET_91, {"enabled": False})
    assert patch.call_count == 0
    assert result["status"] == "already_running"
    assert result["task_id"] == "task-run"


def _task_page(mock: respx.MockRouter, number: int, total: int, *tasks: dict[str, Any]) -> None:
    mock.get("/v1/tasks", params={"pageNumber": str(number), "pageSize": "100"}).respond(
        200,
        json={
            "elements": list(tasks),
            "pageMetadata": {
                "pageNumber": number,
                "pageSize": len(tasks),
                "totalElements": 3,
                "totalPages": total,
            },
        },
    )


async def test_proxy_set_finds_a_running_job_on_a_later_page(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _login(mock)
        _task_page(
            mock, 0, 3, {"id": "task-old", "type": "PROXY_CONFIGURATION", "status": "SUCCESSFUL"}
        )
        _task_page(
            mock, 1, 3, {"id": "task-run", "type": "PROXY_CONFIGURATION", "status": "IN_PROGRESS"}
        )
        third = mock.get("/v1/tasks", params={"pageNumber": "2"})
        patch = mock.patch("/v1/system/proxy-configuration")
        result = await connector.system_proxy_set(_operator(), _TARGET_91, {"enabled": False})
    assert patch.call_count == 0
    assert third.call_count == 0
    assert result["status"] == "already_running"
    assert result["task_id"] == "task-run"


async def test_proxy_set_starts_when_no_page_holds_a_running_job(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        _task_page(
            mock, 0, 2, {"id": "task-old", "type": "PROXY_CONFIGURATION", "status": "SUCCESSFUL"}
        )
        _task_page(mock, 1, 2, {"id": "task-ceip", "type": "CEIP_UPDATE", "status": "IN_PROGRESS"})
        patch = mock.patch("/v1/system/proxy-configuration").respond(
            202, json={"id": "task-new", "status": "PENDING"}
        )
        result = await connector.system_proxy_set(_operator(), _TARGET_91, {"enabled": False})
    assert patch.call_count == 1
    assert result["status"] == "accepted"
    assert result["task_id"] == "task-new"


async def test_status_read_logs_in_again_when_the_login_expired(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        login = mock.post("/v1/tokens")
        login.side_effect = [
            httpx.Response(201, json={"accessToken": "tok-old"}),
            httpx.Response(201, json={"accessToken": "tok-new"}),
        ]
        task = mock.get("/v1/tasks/task-4")
        task.side_effect = [
            httpx.Response(401),
            httpx.Response(200, json={"id": "task-4", "status": "SUCCESSFUL"}),
        ]
        mock.get("/v1/system/proxy-configuration").respond(200, json={"isEnabled": True})
        result = await connector.system_proxy_get(_operator(), _TARGET_91, {"task_id": "task-4"})
    assert login.call_count == 2
    assert result["task"]["outcome"] == "succeeded"


async def test_status_read_says_not_reachable_with_the_job_id_and_time(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.get("/v1/tasks/task-5").mock(side_effect=httpx.ConnectError("made-up refusal"))
        result = await connector.system_proxy_get(_operator(), _TARGET_91, {"task_id": "task-5"})
    assert result["proxy"] is None
    assert result["task"]["outcome"] == "not_reachable"
    assert result["task"]["id"] == "task-5"
    assert result["task"]["checked_at"]
    assert result["task"]["outcome"] != "failed"


async def test_proxy_read_keeps_the_job_when_the_appliance_stops_answering_after_it(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.get("/v1/tasks/task-6").respond(200, json={"id": "task-6", "status": "IN_PROGRESS"})
        mock.get("/v1/system/proxy-configuration").mock(
            side_effect=httpx.ConnectError("made-up refusal")
        )
        result = await connector.system_proxy_get(_operator(), _TARGET_91, {"task_id": "task-6"})
    assert result["task"]["id"] == "task-6"
    assert result["task"]["outcome"] == "running"
    assert result["task"]["checked_at"]
    assert result["proxy"] is None
    assert "no answer from the appliance" in result["message"]


async def test_settings_read_keeps_the_job_and_ceip_when_the_system_read_gets_no_answer(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.get("/v1/tasks/task-7").respond(200, json={"id": "task-7", "status": "SUCCESSFUL"})
        mock.get("/v1/system/ceip").respond(200, json={"status": "ENABLED"})
        mock.get("/v1/system").mock(side_effect=httpx.ConnectError("made-up refusal"))
        result = await connector.system_settings_get(_operator(), _TARGET_91, {"task_id": "task-7"})
    assert result["task"]["id"] == "task-7"
    assert result["task"]["outcome"] == "succeeded"
    assert result["ceip"] == {"status": "ENABLED"}
    assert result["system"] is None
    assert "no answer from the appliance" in result["message"]


async def test_settings_read_without_a_job_reports_a_lost_answer_as_an_error(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.get("/v1/system/ceip").mock(side_effect=httpx.ConnectError("made-up refusal"))
        with pytest.raises(httpx.ConnectError):
            await connector.system_settings_get(_operator(), _TARGET_91, {})


# ------------------------------------------------------- CEIP and system


async def test_ceip_body_is_a_plain_value_on_9_0(connector: InstallerConnector) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        _no_tasks(mock)
        mock.get("/v1/system/ceip").respond(200, json={"status": "ENABLED"})
        patch = mock.patch("/v1/system/ceip").respond(202, json={"id": "task-c1"})
        result = await connector.system_settings_set(
            _operator(), _TARGET_90, {"ceip_enabled": False}
        )
    assert json.loads(patch.calls[0].request.content) == "DISABLED"
    assert result["status"] == "accepted"
    assert result["task_id"] == "task-c1"


async def test_ceip_body_is_an_object_from_9_1(connector: InstallerConnector) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        _no_tasks(mock)
        mock.get("/v1/system/ceip").respond(200, json={"status": "DISABLED"})
        patch = mock.patch("/v1/system/ceip").respond(202, json={"id": "task-c2"})
        await connector.system_settings_set(_operator(), _TARGET_91, {"ceip_enabled": True})
    assert json.loads(patch.calls[0].request.content) == {"status": "ENABLE"}


async def test_ceip_change_while_another_runs_returns_the_vendor_409(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _login(mock)
        _no_tasks(mock)
        mock.get("/v1/system/ceip").respond(200, json={"status": "ENABLED"})
        mock.patch("/v1/system/ceip").respond(
            409, json={"errorCode": "CEIP_CONFLICT", "message": "A CEIP operation is running"}
        )
        system = mock.patch("/v1/system")
        result = await connector.system_settings_set(
            _operator(),
            _TARGET_91,
            {"ceip_enabled": False, "max_domains_in_subscription": 4},
        )
    assert result["status"] == "failed"
    assert result["steps"] == [
        {
            "step": "ceip",
            "result": "failed",
            "error": {
                "http_status": 409,
                "error_code": "CEIP_CONFLICT",
                "message": "A CEIP operation is running",
            },
        },
        {"step": "system", "result": "not_run"},
    ]
    assert system.call_count == 0


async def test_ceip_change_starts_nothing_while_ceip_is_changing(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _login(mock)
        _no_tasks(mock)
        mock.get("/v1/system/ceip").respond(200, json={"status": "ENABLING"})
        patch = mock.patch("/v1/system/ceip")
        result = await connector.system_settings_set(
            _operator(), _TARGET_91, {"ceip_enabled": True}
        )
    assert patch.call_count == 0
    assert result["status"] == "already_running"
    assert result["vendor_status"] == "ENABLING"


async def test_failure_at_step_two_lists_what_ran_failed_and_did_not_run(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        _no_tasks(mock)
        mock.get("/v1/system/ceip").respond(200, json={"status": "DISABLED"})
        mock.patch("/v1/system/ceip").respond(202, json={"id": "task-c3", "status": "PENDING"})
        mock.patch("/v1/system").respond(500, json={"message": "Internal error"})
        result = await connector.system_settings_set(
            _operator(),
            _TARGET_91,
            {"ceip_enabled": True, "max_domains_in_subscription": 3},
        )
    assert result["status"] == "partial"
    assert [(row["step"], row["result"]) for row in result["steps"]] == [
        ("ceip", "accepted"),
        ("system", "failed"),
    ]
    assert result["steps"][1]["error"]["message"] == "Internal error"
    assert result["task_id"] == "task-c3"


async def test_system_setting_alone_is_done_at_once(connector: InstallerConnector) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        patch = mock.patch("/v1/system").respond(200)
        result = await connector.system_settings_set(
            _operator(), _TARGET_91, {"max_domains_in_subscription": 5}
        )
    assert json.loads(patch.calls[0].request.content) == {"maxAllowedDomainsInSubscription": 5}
    assert result["status"] == "done"


async def test_settings_get_reads_ceip_and_system(connector: InstallerConnector) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.get("/v1/system/ceip").respond(200, json={"status": "ENABLED"})
        mock.get("/v1/system").respond(
            200, json={"id": "sys-1", "maxAllowedDomainsInSubscription": 2}
        )
        result = await connector.system_settings_get(_operator(), _TARGET_91, {})
    assert result == {
        "ceip": {"status": "ENABLED"},
        "system": {"id": "sys-1", "maxAllowedDomainsInSubscription": 2},
    }


@pytest.mark.parametrize("value", [0, -1])
def test_system_setting_zero_or_negative_is_refused(value: int) -> None:
    schema = next(
        op.parameter_schema
        for op in INSTALLER_SYSTEM_TYPED_OPS
        if op.op_id == "installer.system.settings.set"
    )
    assert validate_params(schema, {"max_domains_in_subscription": value})
    assert validate_params(schema, {"max_domains_in_subscription": 1}) == []


# ------------------------------------------------- trusted certificate remove


def _pem(common_name: str) -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


async def test_remove_warns_when_the_certificate_belongs_to_the_depot(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.get("/v1/system/settings/depot").respond(
            200, json={"depotConfiguration": {"hostname": "Depot.test.invalid", "port": 443}}
        )
        mock.get("/v1/sddc-manager/trusted-certificates").respond(
            200, json={"elements": [{"alias": "cert-a", "certificate": _pem("depot.test.invalid")}]}
        )
        delete = mock.delete("/v1/sddc-manager/trusted-certificates/cert-a").respond(204)
        result = await connector.system_trusted_certificate_remove(
            _operator(), _TARGET_91, {"alias": "cert-a"}
        )
    assert delete.call_count == 1
    assert result["status"] == "removed"
    assert "depot.test.invalid" in result["warning"]


@pytest.mark.parametrize(
    ("name", "warns"),
    [("*.test.invalid", True), ("*.invalid", False), ("*.depot.test.invalid", False)],
)
async def test_remove_warns_when_a_wildcard_certificate_covers_the_depot(
    connector: InstallerConnector, name: str, warns: bool
) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.get("/v1/system/settings/depot").respond(
            200, json={"depotConfiguration": {"hostname": "depot.test.invalid", "port": 443}}
        )
        mock.get("/v1/sddc-manager/trusted-certificates").respond(
            200, json={"elements": [{"alias": "wild-1", "certificate": _pem(name)}]}
        )
        mock.delete("/v1/sddc-manager/trusted-certificates/wild-1").respond(204)
        result = await connector.system_trusted_certificate_remove(
            _operator(), _TARGET_91, {"alias": "wild-1"}
        )
    assert result["status"] == "removed"
    assert ("warning" in result) is warns


async def test_remove_of_another_certificate_has_no_warning(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.get("/v1/system/settings/depot").respond(
            200, json={"depotConfiguration": {"hostname": "depot.test.invalid"}}
        )
        mock.get("/v1/sddc-manager/trusted-certificates").respond(
            200, json={"elements": [{"alias": "cert-b", "certificate": _pem("other.test.invalid")}]}
        )
        mock.delete("/v1/sddc-manager/trusted-certificates/cert-b").respond(204)
        result = await connector.system_trusted_certificate_remove(
            _operator(), _TARGET_91, {"alias": "cert-b"}
        )
    assert result == {"status": "removed", "alias": "cert-b"}


async def test_remove_of_an_unknown_alias_is_not_found(connector: InstallerConnector) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.get("/v1/system/settings/depot").respond(200, json={})
        mock.get("/v1/sddc-manager/trusted-certificates").respond(200, json={"elements": []})
        mock.delete("/v1/sddc-manager/trusted-certificates/no-such").respond(404)
        with pytest.raises(ConnectorResourceNotFoundError) as exc_info:
            await connector.system_trusted_certificate_remove(
                _operator(), _TARGET_91, {"alias": "no-such"}
            )
    assert exc_info.value.resource_ids == ["no-such"]


# ------------------------------------------------------ resource calculation


def _calc_params(**extra: Any) -> dict[str, Any]:
    return {
        "spec": {"sddcId": "<sddc-id>", "vcenterSpec": {"vcenterHostname": "<vcenter-fqdn>"}},
        "password_refs": [
            {"pointer": "/vcenterSpec/rootVcenterPassword", "secret_ref": "kv/vc-root"}
        ],
        **extra,
    }


async def test_resource_calculation_stops_early_on_9_0(connector: InstallerConnector) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        login = _login(mock)
        calc = mock.post("/v1/sddcs/resources-calculation")
        with pytest.raises(NotImplementedError) as exc_info:
            await connector.sddc_resources_calculate(_operator(), _TARGET_90, _calc_params())
    message = str(exc_info.value)
    assert "installer.sddc.resources.calculate" in message
    assert "9.0.2" in message
    assert "9.1 or newer" in message
    assert login.call_count == 0
    assert calc.call_count == 0


async def test_resource_calculation_runs_to_the_end_and_flattens_the_checks(
    connector: InstallerConnector,
) -> None:
    done = {
        "id": "calc-1",
        "executionStatus": "COMPLETED",
        "resultStatus": "FAILED",
        "requiredCapacity": {"numberOfCores": 64},
        "availableCapacity": {"numberOfCores": 32},
        "validationChecks": [
            {
                "description": "CPU",
                "resultStatus": "FAILED",
                "severity": "ERROR",
                "errorResponse": {"message": "Not enough cores"},
                "nestedValidationChecks": [{"description": "Host 1", "resultStatus": "FAILED"}],
            }
        ],
    }
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        start = mock.post("/v1/sddcs/resources-calculation").respond(
            202, json={"id": "calc-1", "executionStatus": "IN_PROGRESS"}
        )
        poll = mock.get("/v1/sddcs/resources-calculation/calc-1")
        poll.side_effect = [
            httpx.Response(200, json={"id": "calc-1", "executionStatus": "IN_PROGRESS"}),
            httpx.Response(200, json=done),
        ]
        result = await connector.sddc_resources_calculate(_operator(), _TARGET_91, _calc_params())

    sent = json.loads(start.calls[0].request.content)
    assert sent["vcenterSpec"]["rootVcenterPassword"] == _SPEC_PASSWORD
    assert poll.call_count == 2
    assert result["status"] == "done"
    assert result["result_status"] == "FAILED"
    assert result["required"] == {"numberOfCores": 64}
    assert result["checks"] == [
        {
            "description": "CPU",
            "severity": "ERROR",
            "result_status": "FAILED",
            "error": "Not enough cores",
            "parent": None,
        },
        {
            "description": "Host 1",
            "severity": None,
            "result_status": "FAILED",
            "error": None,
            "parent": "CPU",
        },
    ]
    assert _SPEC_PASSWORD not in json.dumps(result)


async def test_resource_calculation_hides_the_password_in_every_text_of_the_answer(
    connector: InstallerConnector,
) -> None:
    done = {
        "id": "calc-4",
        "executionStatus": "COMPLETED",
        "resultStatus": "FAILED",
        "requiredCapacity": {"numberOfCores": 8, "note": f"spec sent {_SPEC_PASSWORD}"},
        "validationChecks": [
            {
                "description": f"vCenter login with {_SPEC_PASSWORD}",
                "resultStatus": "FAILED",
                "nestedValidationChecks": [
                    {
                        "description": "Host 1",
                        "resultStatus": "FAILED",
                        "errorResponse": {"message": f"{_SPEC_PASSWORD} was refused"},
                    }
                ],
            }
        ],
    }
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.post("/v1/sddcs/resources-calculation").respond(202, json=done)
        result = await connector.sddc_resources_calculate(_operator(), _TARGET_91, _calc_params())
    assert result["status"] == "done"
    assert _SPEC_PASSWORD not in json.dumps(result)
    assert result["checks"][0]["description"] == _secret_refs._HIDDEN
    assert result["checks"][1]["parent"] == _secret_refs._HIDDEN
    assert result["checks"][1]["error"] == _secret_refs._HIDDEN
    assert result["required"]["note"] == _secret_refs._HIDDEN
    assert result["required_cores"] == 8


async def test_resource_calculation_with_a_spec_that_is_not_valid(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.post("/v1/sddcs/resources-calculation").respond(
            400, json={"errorCode": "INVALID_SPEC", "message": "dnsSpec is missing"}
        )
        result = await connector.sddc_resources_calculate(_operator(), _TARGET_91, _calc_params())
    assert result == {
        "status": "rejected",
        "http_status": 400,
        "error_code": "INVALID_SPEC",
        "message": "dnsSpec is missing",
    }


async def test_resource_calculation_refuses_a_password_value_in_the_spec(
    connector: InstallerConnector,
) -> None:
    params = {"spec": {"vcenterSpec": {"rootVcenterPassword": "inline-value"}}}
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        calc = mock.post("/v1/sddcs/resources-calculation")
        with pytest.raises(ValueError, match="/vcenterSpec/rootVcenterPassword") as exc_info:
            await connector.sddc_resources_calculate(_operator(), _TARGET_91, params)
    assert "inline-value" not in str(exc_info.value)
    assert calc.call_count == 0


async def test_resource_calculation_read_failure_part_way_says_what_was_done(
    connector: InstallerConnector,
) -> None:
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.post("/v1/sddcs/resources-calculation").respond(
            202, json={"id": "calc-2", "executionStatus": "IN_PROGRESS"}
        )
        mock.get("/v1/sddcs/resources-calculation/calc-2").respond(500)
        result = await connector.sddc_resources_calculate(_operator(), _TARGET_91, _calc_params())
    assert result["status"] == "read_failed"
    assert result["id"] == "calc-2"
    assert result["execution_status"] == "IN_PROGRESS"
    assert "HTTP 500" in result["error"]


async def test_resource_calculation_times_out_with_the_id(
    connector: InstallerConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources_calc, "DEFAULT_TIMEOUT_SECONDS", 0)
    async with respx.mock(base_url=_BASE) as mock:
        _login(mock)
        mock.post("/v1/sddcs/resources-calculation").respond(
            202, json={"id": "calc-3", "executionStatus": "IN_PROGRESS"}
        )
        result = await connector.sddc_resources_calculate(
            _operator(), _TARGET_91, {**_calc_params(), "timeout_seconds": 0}
        )
    assert result["status"] == "timed_out"
    assert result["id"] == "calc-3"


# ------------------------------------------------------- approval previews


async def test_proxy_set_preview_shows_the_change_and_never_the_password() -> None:
    from types import SimpleNamespace

    from meho_backplane.operations._preview import _PREVIEW_BUILDERS

    params = {
        "enabled": True,
        "host": "proxy.test.invalid",
        "port": 3128,
        "username": "proxy-user",
        "password_secret_ref": "kv/proxy",
    }
    builder = _PREVIEW_BUILDERS["installer.system.proxy.set"]
    preview = await builder(SimpleNamespace(params=params, target=_TARGET_91))
    assert preview["host"] == "proxy.test.invalid"
    assert preview["password_from_vault"] is True
    assert "kv/proxy" not in json.dumps(preview)
    remove = _PREVIEW_BUILDERS["installer.system.trusted-certificates.remove"]
    assert (await remove(SimpleNamespace(params={"alias": "cert-a"})))["alias"] == "cert-a"
