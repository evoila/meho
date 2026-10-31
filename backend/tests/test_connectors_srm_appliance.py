# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the SRM appliance setup reads, registration, network and time changes.

What is covered, with a respx-mocked appliance setup API
(``/api/rest/configure/v2``) and no ingested catalog:

* the risk and approval fields of every operation of the setup table;
* the setup API login with the appliance admin user from Vault, the
  ``x-dr-session`` header, a wrong admin password (no password in the
  answer), and a login that expired between a start and a status read;
* ``srm.appliance.show`` and ``srm.appliance.task.get``: the vendor states,
  a failed task with its error, and ``not_reachable`` with the task id and
  the time;
* ``srm.appliance.register``: the vCenter address always as
  ``https://<name>:443``, the SHA-256 fingerprint in capitals with colons,
  a fingerprint that does not match, an existing registration, a running
  registration task, both registration forms (up to 9.0 and 9.1), a wrong
  SSO password at step two (steps that ran, failed and did not run);
* ``srm.appliance.network.set``: a new address on MEHO's interface needs
  the explicit input and ends the session; a vendor error at step two;
* ``srm.appliance.time.set``: success, a vendor error, a bad input;
* a change waits for approval when a person calls it.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.srm import (
    SRM_CONNECTOR_ID,
    SRM_IMPL_ID,
    SRM_PRODUCT,
    SRM_TYPED_OPS,
    SRM_VERSION,
    SrmConnector,
    register_srm_typed_operations,
)
from meho_backplane.connectors.srm.appliance_register import (
    normalise_sha256_fingerprint,
    vcenter_uri,
)
from meho_backplane.connectors.srm.typed_ops import SRM_TYPED_WHEN_TO_USE_BY_GROUP
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings

_HOST = "srm-setup.test.invalid"
_BASE_URL = f"https://{_HOST}"
_API = "/api/rest/configure/v2"
_ADMIN_PASSWORD = "appliance-admin-made-up"
_SSO_PASSWORD = "sso-admin-made-up"
_ADMIN_REF = "<vault-path-of-appliance-admin>"
_SSO_REF = "<vault-path-of-sso-admin>"
_VC = "vc-01.test.invalid"
_PEM_BODY = base64.b64encode(b"made-up-der-bytes-of-a-vcenter-certificate").decode()
_VC_PEM = f"-----BEGIN CERTIFICATE-----\n{_PEM_BODY}\n-----END CERTIFICATE-----"
_VC_FP = normalise_sha256_fingerprint(hashlib.sha256(base64.b64decode(_PEM_BODY)).hexdigest())

#: The table of the Task: op id -> (safety level, requires approval).
_TABLE: dict[str, tuple[str, bool]] = {
    "srm.appliance.show": ("safe", False),
    "srm.appliance.task.get": ("safe", False),
    "srm.appliance.register": ("dangerous", True),
    "srm.appliance.network.set": ("dangerous", True),
    "srm.appliance.time.set": ("dangerous", True),
    "srm.appliance.csr.create": ("caution", True),
    "srm.appliance.certificate.set": ("dangerous", True),
    "srm.storage_adapter.install": ("destructive", True),
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=SRM_PRODUCT, version=SRM_VERSION, impl_id=SRM_IMPL_ID, cls=SrmConnector
    )
    monkeypatch.setattr("meho_backplane.connectors.srm.setup_api.TASK_POLL_SECONDS", 0.0)
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    return AsyncMock()


class _Target:
    def __init__(self, host: str = _HOST) -> None:
        self.product = SRM_PRODUCT
        self.fingerprint = type("_FP", (), {"version": SRM_VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = f"srm-setup-{self.id.hex[:6]}"
        self.host = host
        self.port = 443
        self.fqdn: str | None = None
        self.secret_ref = "<vault-path-of-srm-login>"
        self.auth_model = "shared_service_account"


def _operator() -> Operator:
    return Operator(
        sub="op-srm-setup",
        name="SRM Setup Operator",
        email=None,
        raw_jwt="op.srm.setup.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


class _Loader:
    """Records which reference each credential read used."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self, _target: object, _operator: Operator, param: str, ref: str
    ) -> dict[str, str]:
        self.calls.append((param, ref))
        if param == "sso_secret_ref":
            return {"username": "sso-admin", "password": _SSO_PASSWORD}
        return {"username": "admin", "password": _ADMIN_PASSWORD}


async def _connector(_stub_embedding: AsyncMock) -> tuple[SrmConnector, _Loader]:
    await register_srm_typed_operations()
    instance = get_or_create_connector_instance(SrmConnector)
    assert isinstance(instance, SrmConnector)
    loader = _Loader()
    instance.setup_credentials_loader = loader
    return instance, loader


def _login(mock: respx.MockRouter, *ids: str) -> respx.Route:
    replies = [httpx.Response(200, json={"session_id": sid}) for sid in ids or ("setup-1",)]
    return mock.post(f"{_API}/session").mock(side_effect=replies)


def _no_secret(body: Any) -> None:
    text = json.dumps(body, default=str)
    assert _ADMIN_PASSWORD not in text
    assert _SSO_PASSWORD not in text


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("op_id", "expected"), sorted(_TABLE.items()))
def test_risk_and_approval_follow_the_table(op_id: str, expected: tuple[str, bool]) -> None:
    op = next(op for op in SRM_TYPED_OPS if op.op_id == op_id)
    assert (op.safety_level, op.requires_approval) == expected


def test_every_setup_op_has_a_group_text_and_a_secret_reference_input() -> None:
    for op in SRM_TYPED_OPS:
        if op.op_id not in _TABLE:
            continue
        assert op.group_key in SRM_TYPED_WHEN_TO_USE_BY_GROUP
        assert "appliance_secret_ref" in op.parameter_schema["required"]
        assert op.handler_attr in vars(SrmConnector)
        for word in ("password", "token"):
            assert word not in op.parameter_schema["properties"]


@pytest.mark.parametrize(
    "raw", [_VC, f"https://{_VC}", f"{_VC}:443", f"https://{_VC}:443/", _VC.upper()]
)
def test_vcenter_address_always_goes_out_with_https_and_port(raw: str) -> None:
    assert vcenter_uri(raw) == f"https://{_VC}:443"


@pytest.mark.parametrize("raw", [f"http://{_VC}", f"{_VC}:8443", f"https://{_VC}/ui", ""])
def test_other_vcenter_forms_are_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        vcenter_uri(raw)


def test_fingerprint_is_sha256_in_capitals_with_colons() -> None:
    flat = "ab" * 32
    assert normalise_sha256_fingerprint(flat) == ":".join(["AB"] * 32)
    assert normalise_sha256_fingerprint(":".join(["ab"] * 32)) == ":".join(["AB"] * 32)
    with pytest.raises(ValueError):
        normalise_sha256_fingerprint("ab" * 20)  # a SHA-1 fingerprint


# ---------------------------------------------------------------------------
# srm.appliance.show and the setup API login
# ---------------------------------------------------------------------------


def _mock_show(mock: respx.MockRouter, *, configured: bool = True) -> respx.Route:
    mock.get(f"{_API}/appliance").respond(
        200, json={"hostname": "<srm-fqdn>", "appliance_version": "9.1.1.0", "product_name": "x"}
    )
    if configured:
        mock.get(f"{_API}/appliance/configuration-manager/configuration").respond(
            200,
            json={
                "site_name": "<site-name>",
                "overall_configuration_state": "CONFIGURED",
                "connection": {"psc_uri": f"https://{_VC}:443"},
                "credentials": {"admin_user": "sso-admin", "admin_password": _SSO_PASSWORD},
                "services_configuration": [{"name": "srm-service", "state": "CONFIGURED"}],
            },
        )
    else:
        mock.get(f"{_API}/appliance/configuration-manager/configuration").respond(
            404, json={"message": "not configured"}
        )
    mock.get(f"{_API}/appliance/settings/network").respond(
        200,
        json={
            "dns": {"mode": "STATIC", "hostname": "<srm-fqdn>", "servers": ["<dns-ip>"]},
            "interfaces": [
                {
                    "name": "eth0",
                    "status": "UP",
                    "ipv4": {"assignment_mode": "STATIC", "address": "<old-ip>", "prefix": 24},
                }
            ],
        },
    )
    mock.get(f"{_API}/appliance/settings/time").respond(
        200, json={"zone": "Etc/UTC", "synchronization": {"mode": "NTP", "ntp_servers": ["<n>"]}}
    )
    return mock.get(f"{_API}/appliance/certificates/server").respond(
        200,
        json={"subject_dn": "CN=<srm-fqdn>", "not_after": 1893456000000, "certificate": _VC_PEM},
    )


async def test_show_logs_in_with_the_appliance_admin_and_reduces_the_reads(
    _stub_embedding: AsyncMock,
) -> None:
    _, loader = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL) as mock:
        login = _login(mock)
        cert = _mock_show(mock)
        result = await dispatch(
            operator=_operator(),
            connector_id=SRM_CONNECTOR_ID,
            op_id="srm.appliance.show",
            target=_Target(),
            params={"appliance_secret_ref": _ADMIN_REF},
        )
    assert result.status == "ok", result.error
    assert loader.calls == [("appliance_secret_ref", _ADMIN_REF)]
    assert login.calls[0].request.headers["authorization"].startswith("Basic ")
    assert cert.calls[0].request.headers["x-dr-session"] == "setup-1"
    body = result.result
    assert body["registration"]["state"] == "CONFIGURED"
    assert body["registration"]["psc_uri"] == f"https://{_VC}:443"
    assert body["registration"]["services"] == [{"name": "srm-service", "state": "CONFIGURED"}]
    assert body["network"]["interfaces"][0]["ipv4"]["address"] == "<old-ip>"
    assert body["time"] == {
        "zone": "Etc/UTC",
        "current_time": None,
        "sync_mode": "NTP",
        "ntp_servers": ["<n>"],
    }
    assert body["certificate"]["subject_dn"] == "CN=<srm-fqdn>"
    assert "credentials" not in json.dumps(body)
    assert _PEM_BODY not in json.dumps(body)
    _no_secret(body)


async def test_show_says_unconfigured_when_the_vendor_answers_404(
    _stub_embedding: AsyncMock,
) -> None:
    connector, _ = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL) as mock:
        _login(mock)
        _mock_show(mock, configured=False)
        body = await connector.appliance_show(
            _operator(), _Target(), {"appliance_secret_ref": _ADMIN_REF}
        )
    assert body["registration"]["state"] == "UNCONFIGURED"


async def test_wrong_appliance_admin_password_is_named_and_not_shown(
    _stub_embedding: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(f"{_API}/session").respond(401, json={"message": "bad credentials"})
        appliance = mock.get(f"{_API}/appliance").respond(200, json={})
        result = await dispatch(
            operator=_operator(),
            connector_id=SRM_CONNECTOR_ID,
            op_id="srm.appliance.show",
            target=_Target(),
            params={"appliance_secret_ref": _ADMIN_REF},
        )
    assert result.status == "error"
    assert appliance.call_count == 0
    text = json.dumps(result.model_dump(mode="json"), default=str)
    assert "appliance_secret_ref was refused (HTTP 401)" in text
    assert _ADMIN_PASSWORD not in text
    assert _ADMIN_PASSWORD not in caplog.text


# ---------------------------------------------------------------------------
# srm.appliance.task.get
# ---------------------------------------------------------------------------


async def test_task_get_returns_state_progress_and_error(_stub_embedding: AsyncMock) -> None:
    connector, _ = await _connector(_stub_embedding)
    task = {
        "id": "task-1",
        "status": "ERROR",
        "progress": 40,
        "type": "CONFIGURE",
        "error": {"message": "vendor says no", "details": "made-up detail"},
    }
    async with respx.mock(base_url=_BASE_URL) as mock:
        _login(mock)
        mock.get(f"{_API}/tasks/task-1").respond(200, json=task)
        body = await connector.appliance_task_get(
            _operator(), _Target(), {"appliance_secret_ref": _ADMIN_REF, "task_id": "task-1"}
        )
    assert body["state"] == "failed"
    assert body["progress"] == 40
    assert body["error"] == {"message": "vendor says no", "details": "made-up detail"}
    assert body["reachable"] is True


async def test_task_get_logs_in_again_when_the_session_expired(
    _stub_embedding: AsyncMock,
) -> None:
    connector, _ = await _connector(_stub_embedding)
    params = {"appliance_secret_ref": _ADMIN_REF, "task_id": "task-1"}
    async with respx.mock(base_url=_BASE_URL) as mock:
        login = _login(mock, "setup-1", "setup-2")
        mock.get(f"{_API}/tasks/task-1").respond(200, json={"id": "task-1", "status": "RUNNING"})
        first = await connector.appliance_task_get(_operator(), _Target(), params)
        task = mock.get(f"{_API}/tasks/task-1").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json={"id": "task-1", "status": "SUCCESS", "progress": 100}),
            ]
        )
        second = await connector.appliance_task_get(_operator(), _Target(), params)
    assert first["state"] == "running"
    assert second["state"] == "succeeded"
    assert login.call_count == 2
    assert task.calls[-1].request.headers["x-dr-session"] == "setup-2"


@pytest.mark.parametrize(
    "reply", [httpx.ConnectError("refused"), httpx.Response(503, text="upgrading")]
)
async def test_task_get_says_not_reachable_with_the_task_id_and_time(
    _stub_embedding: AsyncMock, reply: Any
) -> None:
    connector, _ = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(f"{_API}/session").mock(side_effect=[reply])
        body = await connector.appliance_task_get(
            _operator(), _Target(), {"appliance_secret_ref": _ADMIN_REF, "task_id": "task-9"}
        )
    assert body["state"] == "not_reachable"
    assert body["state"] != "failed"
    assert body["task_id"] == "task-9"
    assert body["reachable"] is False
    assert body["checked_at"]


# ---------------------------------------------------------------------------
# srm.appliance.register
# ---------------------------------------------------------------------------


def _register_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "appliance_secret_ref": _ADMIN_REF,
        "sso_secret_ref": _SSO_REF,
        "vcenter": _VC,
        "vcenter_fingerprint": _VC_FP.replace(":", "").lower(),
        "site_name": "<site-name>",
        "admin_email": "<admin-email>",
    }
    params.update(overrides)
    return params


def _mock_register(
    mock: respx.MockRouter, *, version: str = "9.1.1.0", existing: list[Any] | None = None
) -> dict[str, respx.Route]:
    _login(mock)
    mock.get(f"{_API}/appliance").respond(200, json={"appliance_version": version})
    base = f"{_API}/appliance/configuration-manager"
    return {
        "tasks": mock.get(f"{_API}/tasks").respond(200, json={"list": []}),
        "probe": mock.post(
            f"{_API}/appliance/certificates/server/actions/probe-ssl-connection"
        ).respond(200, json={"certificate": _VC_PEM, "thumbprint": "made-up-sha1"}),
        "validate": mock.post(f"{base}/actions/validate-connection").respond(204),
        "existing": mock.post(f"{base}/actions/check-existing-registrations").respond(
            200, json={"list": existing or []}
        ),
        "list_vc": mock.post(f"{base}/actions/list-vc-services").respond(
            200, json={"list": [{"url": f"https://{_VC}/sdk", "service_id": "vc-service-1"}]}
        ),
        "put": mock.put(f"{base}/configuration").respond(
            202, json={"id": "task-reg", "status": "RUNNING", "type": "CONFIGURE"}
        ),
    }


async def test_register_on_91_sends_https_and_port_and_returns_the_task(
    _stub_embedding: AsyncMock,
) -> None:
    connector, loader = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_register(mock)
        routes["list_vc"].respond(500)
        body = await connector.appliance_register(_operator(), _Target(), _register_params())
    assert body["outcome"] == "accepted", body
    assert body["task_id"] == "task-reg"
    assert body["status_read"] == {
        "op_id": "srm.appliance.task.get",
        "params": {"task_id": "task-reg"},
    }
    assert json.loads(routes["probe"].calls[0].request.content) == {"uri": f"https://{_VC}:443"}
    sent = json.loads(routes["put"].calls[0].request.content)
    assert sent["connection"] == {"psc_uri": f"https://{_VC}:443", "psc_thumbprint": _VC_FP}
    assert sent["credentials"] == {"admin_user": "sso-admin", "admin_password": _SSO_PASSWORD}
    assert sent["extension_key"] == "com.vmware.vcDr"
    assert routes["list_vc"].call_count == 0
    assert [row["step"] for row in body["steps"]] == [
        "running_task",
        "probe_certificate",
        "validate_connection",
        "check_existing_registrations",
        "register",
    ]
    assert ("sso_secret_ref", _SSO_REF) in loader.calls
    _no_secret(body)


async def test_register_up_to_90_also_sends_the_vcenter_id_and_fingerprint(
    _stub_embedding: AsyncMock,
) -> None:
    connector, _ = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_register(mock, version="9.0.2.0")
        body = await connector.appliance_register(_operator(), _Target(), _register_params())
    assert body["outcome"] == "accepted", body
    assert json.loads(routes["list_vc"].calls[0].request.content) == {
        "psc_uri": f"https://{_VC}:443",
        "psc_thumbprint": _VC_FP,
    }
    sent = json.loads(routes["put"].calls[0].request.content)
    assert sent["connection"]["vc_instance_id"] == "vc-service-1"
    assert sent["connection"]["vc_thumbprint"] == _VC_FP


async def test_register_stops_when_the_fingerprint_differs(_stub_embedding: AsyncMock) -> None:
    connector, _ = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_register(mock)
        body = await connector.appliance_register(
            _operator(), _Target(), _register_params(vcenter_fingerprint="00" * 32)
        )
    assert body["outcome"] == "refused"
    assert body["seen_fingerprint"] == _VC_FP
    assert routes["validate"].call_count == 0
    assert routes["put"].call_count == 0


async def test_register_stops_on_an_existing_registration(_stub_embedding: AsyncMock) -> None:
    connector, _ = await _connector(_stub_embedding)
    existing = [
        {
            "registration_type": "VCENTER_SERVICE",
            "extension_key": "com.vmware.vcDr",
            "server_url": "https://<other-srm>",
        }
    ]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_register(mock, existing=existing)
        body = await connector.appliance_register(_operator(), _Target(), _register_params())
    assert body["outcome"] == "refused"
    assert body["existing_registrations"][0]["registration_type"] == "VCENTER_SERVICE"
    assert routes["put"].call_count == 0
    sent = json.loads(routes["existing"].calls[0].request.content)
    assert sent["extension_key"] == "com.vmware.vcDr"
    assert sent["connection_spec"]["connection_spec"]["psc_uri"] == f"https://{_VC}:443"


async def test_register_returns_a_running_task_and_starts_nothing(
    _stub_embedding: AsyncMock,
) -> None:
    connector, _ = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_register(mock)
        routes["tasks"].respond(
            200,
            json={"list": [{"id": "task-old", "status": "RUNNING", "type": "CONFIGURE"}]},
        )
        body = await connector.appliance_register(_operator(), _Target(), _register_params())
    assert body["outcome"] == "already_running"
    assert body["task_id"] == "task-old"
    assert body["status_read"]["params"] == {"task_id": "task-old"}
    assert routes["probe"].call_count == 0
    assert routes["put"].call_count == 0


async def test_register_wrong_sso_password_lists_the_steps(_stub_embedding: AsyncMock) -> None:
    connector, _ = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_register(mock)
        routes["validate"].respond(400, json={"message": "Cannot login to SSO: wrong password"})
        body = await connector.appliance_register(_operator(), _Target(), _register_params())
    assert body["outcome"] == "failed"
    status = {row["step"]: row["status"] for row in body["steps"]}
    assert status == {
        "running_task": "done",
        "probe_certificate": "done",
        "validate_connection": "failed",
        "check_existing_registrations": "not_run",
        "list_vc_services": "not_run",
        "register": "not_run",
    }
    failed = next(row for row in body["steps"] if row["status"] == "failed")
    assert "wrong password" in failed["error"]
    assert routes["put"].call_count == 0
    _no_secret(body)


async def test_register_waits_for_approval(_stub_embedding: AsyncMock) -> None:
    await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_register(mock)
        result = await dispatch(
            operator=_operator(),
            connector_id=SRM_CONNECTOR_ID,
            op_id="srm.appliance.register",
            target=_Target(),
            params=_register_params(),
        )
    assert result.status == "awaiting_approval", result
    assert routes["put"].call_count == 0


# ---------------------------------------------------------------------------
# srm.appliance.network.set
# ---------------------------------------------------------------------------


def _network(mock: respx.MockRouter) -> None:
    _login(mock)
    mock.get(f"{_API}/appliance/settings/network").respond(
        200,
        json={
            "dns": {"mode": "STATIC", "servers": ["<dns-ip>"]},
            "interfaces": [
                {"name": "eth0", "ipv4": {"assignment_mode": "STATIC", "address": "<old-ip>"}}
            ],
        },
    )


_NEW_ADDRESS = {
    "name": "eth0",
    "ipv4": {"assignment_mode": "STATIC", "address": "<new-ip>", "prefix": 24},
}


async def test_new_address_on_mehos_interface_needs_the_explicit_input(
    _stub_embedding: AsyncMock,
) -> None:
    connector, _ = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _network(mock)
        change = mock.post(f"{_API}/appliance/settings/network/interfaces/eth0").respond(204)
        with pytest.raises(ValueError, match="allow_address_change"):
            await connector.appliance_network_set(
                _operator(),
                _Target(),
                {"appliance_secret_ref": _ADMIN_REF, "interface": _NEW_ADDRESS},
            )
    assert change.call_count == 0


async def test_new_address_ends_the_session_and_names_the_target_change(
    _stub_embedding: AsyncMock,
) -> None:
    connector, _ = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL) as mock:
        _network(mock)
        change = mock.post(f"{_API}/appliance/settings/network/interfaces/eth0").mock(
            side_effect=httpx.ReadError("connection closed")
        )
        body = await connector.appliance_network_set(
            _operator(),
            _Target(),
            {
                "appliance_secret_ref": _ADMIN_REF,
                "interface": _NEW_ADDRESS,
                "allow_address_change": True,
            },
        )
    assert body["outcome"] == "done"
    assert body["session_ended"] is True
    assert body["target_must_change"]["new_address"] == "<new-ip>"
    assert json.loads(change.calls[0].request.content) == {"ipv4": _NEW_ADDRESS["ipv4"]}
    assert connector.setup_sessions == {}


async def test_network_failure_at_step_two_keeps_step_one(_stub_embedding: AsyncMock) -> None:
    connector, _ = await _connector(_stub_embedding)
    same = {"name": "eth0", "ipv4": {"assignment_mode": "STATIC", "address": "<old-ip>"}}
    same["ipv4"]["prefix"] = 24
    async with respx.mock(base_url=_BASE_URL) as mock:
        _network(mock)
        dns = mock.put(f"{_API}/appliance/settings/network/dns").respond(204)
        mock.post(f"{_API}/appliance/settings/network/interfaces/eth0").respond(
            400, json={"message": "gateway not in subnet"}
        )
        body = await connector.appliance_network_set(
            _operator(),
            _Target(),
            {
                "appliance_secret_ref": _ADMIN_REF,
                "dns": {"mode": "STATIC", "servers": ["<dns-ip>"]},
                "interface": same,
            },
        )
    assert dns.call_count == 1
    assert body["outcome"] == "failed"
    assert body["steps"][0] == {"step": "set_dns", "status": "done"}
    assert body["steps"][1]["status"] == "failed"
    assert "gateway not in subnet" in body["steps"][1]["error"]
    assert body["session_ended"] is False


# ---------------------------------------------------------------------------
# srm.appliance.time.set
# ---------------------------------------------------------------------------


async def test_time_set_runs_after_approval_and_reads_back(_stub_embedding: AsyncMock) -> None:
    await _connector(_stub_embedding)
    params = {
        "appliance_secret_ref": _ADMIN_REF,
        "zone": "Etc/UTC",
        "sync_mode": "NTP",
        "ntp_servers": ["<ntp-server>"],
    }
    async with respx.mock(base_url=_BASE_URL) as mock:
        _login(mock)
        put = mock.put(f"{_API}/appliance/settings/time").respond(204)
        mock.get(f"{_API}/appliance/settings/time").respond(
            200, json={"zone": "Etc/UTC", "synchronization": {"mode": "NTP", "ntp_servers": ["x"]}}
        )
        waiting = await dispatch(
            operator=_operator(),
            connector_id=SRM_CONNECTOR_ID,
            op_id="srm.appliance.time.set",
            target=_Target(),
            params=params,
        )
        assert waiting.status == "awaiting_approval"
        assert put.call_count == 0
        result = await dispatch(
            operator=_operator(),
            connector_id=SRM_CONNECTOR_ID,
            op_id="srm.appliance.time.set",
            target=_Target(),
            params=params,
            _approved=True,
        )
    assert result.status == "ok", result.error
    assert json.loads(put.calls[0].request.content) == {
        "zone": "Etc/UTC",
        "synchronization": {"mode": "NTP", "ntp_servers": ["<ntp-server>"]},
    }
    assert result.result["time"]["sync_mode"] == "NTP"


async def test_time_set_vendor_error_reaches_the_answer(_stub_embedding: AsyncMock) -> None:
    await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL) as mock:
        _login(mock)
        mock.put(f"{_API}/appliance/settings/time").respond(
            400, json={"message": "unknown time zone"}
        )
        result = await dispatch(
            operator=_operator(),
            connector_id=SRM_CONNECTOR_ID,
            op_id="srm.appliance.time.set",
            target=_Target(),
            params={"appliance_secret_ref": _ADMIN_REF, "zone": "Mars/Base"},
            _approved=True,
        )
    assert result.status == "error"
    assert "unknown time zone" in json.dumps(result.model_dump(mode="json"), default=str)


async def test_time_set_ntp_needs_servers(_stub_embedding: AsyncMock) -> None:
    connector, _ = await _connector(_stub_embedding)
    with pytest.raises(ValueError, match="ntp_servers"):
        await connector.appliance_time_set(
            _operator(), _Target(), {"appliance_secret_ref": _ADMIN_REF, "sync_mode": "NTP"}
        )
