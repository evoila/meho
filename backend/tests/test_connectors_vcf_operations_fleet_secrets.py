# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the fleet certificate and password operations of VCF Operations 9.1.

Checks, without a network call (respx, hand-written answers with made-up ids):

* on a 9.0 target each operation stops early with the version message and
  sends nothing; on 9.1.0 and 9.1.1 it runs;
* the lists send the vendor filters and paging, and return flat rows; the
  password list keeps only its known fields, so no password can come back;
* the two changes send the vendor body, return ``accepted`` with the request
  id, start nothing when an earlier request still runs, and pass a vendor
  error on; the password change reads both passwords from the secret store;
* the status read gives the state, the outcome and the vendor error text; it
  logs in again after an expired login, and says ``not_reachable`` (not
  ``failed``) when VCF Operations does not answer;
* the metadata: reads are safe, changes wait for approval, only the 9.x
  registrar registers them, and the password change is a credential write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from tenacity import wait_none

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors._shared.vcf_auth import VcfTargetLike
from meho_backplane.connectors.base import ConnectorUpstreamError
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_operations import VROPS_TYPED_OPS, VcfOperationsConnector
from meho_backplane.connectors.vcf_operations import fleet_secrets_checks as checks
from meho_backplane.connectors.vcf_operations import fleet_secrets_handlers as handlers
from meho_backplane.connectors.vcf_operations.fleet_secrets_ops import VROPS_FLEET_SECRETS_OPS
from meho_backplane.operations._errors import result_connector_error, result_invalid_params
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._validate import validate_params

_HOST = "vrops-fleet.test.invalid"
_BASE_URL = f"https://{_HOST}"
_ACQUIRE_PATH = "/suite-api/api/auth/token/acquire"
_CERTS = "/suite-api/api/fleet-management/certificate-management/certificates"
_ACCOUNTS = "/suite-api/api/fleet-management/password-management/accounts"
_REQUESTS = "/suite-api/api/workflows/requests"

#: The table of the Task: op id -> (safety level, requires approval).
_TABLE: dict[str, tuple[str, bool]] = {
    "vrops.fleet.certificate.list": ("safe", False),
    "vrops.fleet.certificate.replace": ("dangerous", True),
    "vrops.fleet.password.list": ("safe", False),
    "vrops.fleet.password.rotate": ("dangerous", True),
    "vrops.fleet.request.get": ("safe", False),
}


@dataclass
class _StubTarget:
    name: str = "vrops-fleet"
    host: str = _HOST
    port: int | None = 443
    secret_ref: str = "vrops/vrops-fleet"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    auth_source: str | None = None
    tls_server_name: str | None = None
    version: str | None = "9.1.0"
    id: UUID = field(default_factory=uuid4)
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


_ROTATE_PARAMS: dict[str, Any] = {
    "passwordAccountKey": "acct-1",
    "userName": "svc-a",
    "current_password_ref": "fleet/acct-1#current",
    "new_password_ref": "fleet/acct-1#next",
}

#: One minimal valid call per operation.
_CALLS: dict[str, dict[str, Any]] = {
    "vrops.fleet.certificate.list": {},
    "vrops.fleet.certificate.replace": {"certificateId": "cert-1", "caType": "VMCA"},
    "vrops.fleet.password.list": {},
    "vrops.fleet.password.rotate": _ROTATE_PARAMS,
    "vrops.fleet.request.get": {"request_id": "req-1"},
}

_ACCEPTED = {
    "requestId": "req-1",
    "state": "CREATED",
    "category": "VCF_CERTIFICATE_MANAGEMENT",
    "requestName": "Replace certificate",
    "requestType": "REPLACE_CERTIFICATE",
}


def _handler(op_id: str) -> Any:
    op = next(op for op in VROPS_FLEET_SECRETS_OPS if op.op_id == op_id)
    return getattr(VcfOperationsConnector, op.handler_attr)


@pytest.fixture
def secrets(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The secret store: ``fleet/acct-1`` holds two made-up passwords."""

    async def _load(target: Any, _operator: Operator, *, param: str) -> dict[str, Any]:
        assert target.secret_ref == "fleet/acct-1", param
        return {"current": "old-pass-1\n", "next": "new-pass-2"}

    mock = AsyncMock(side_effect=_load)
    monkeypatch.setattr(handlers, "load_param_secret_data", mock)
    return mock


@pytest.fixture
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        VcfOperationsConnector._request_json.retry,  # type: ignore[attr-defined]
        "wait",
        wait_none(),
    )


# --- metadata -----------------------------------------------------------


def test_each_operation_has_the_safety_level_and_approval_of_the_table() -> None:
    assert {
        op.op_id: (op.safety_level, op.requires_approval) for op in VROPS_FLEET_SECRETS_OPS
    } == (_TABLE)
    for op in VROPS_FLEET_SECRETS_OPS:
        assert ("read-only" in op.tags) is (op.safety_level == "safe")
        assert callable(getattr(VcfOperationsConnector, op.handler_attr))


def test_fleet_ops_stay_out_of_the_shared_8x_tuple() -> None:
    shared = {op.op_id for op in VROPS_TYPED_OPS}
    assert not shared & set(_TABLE)


@pytest.mark.asyncio
async def test_9x_registrar_registers_the_fleet_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    from meho_backplane.connectors.vcf_operations import typed_ops

    register = AsyncMock()
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.register_typed_operation", register
    )
    await typed_ops.register_vcf_operations_typed_operations(embedding_service=None)

    registered = {call.kwargs["op_id"]: call.kwargs for call in register.await_args_list}
    for op_id in _TABLE:
        assert registered[op_id]["version"] == "9.0"
        assert registered[op_id]["when_to_use"]


def test_examples_validate_and_use_placeholders() -> None:
    for op in VROPS_FLEET_SECRETS_OPS:
        for example in op.parameter_schema["examples"]:
            assert validate_params(op.parameter_schema, example) == [], op.op_id
        for op_id, params in _CALLS.items():
            if op_id == op.op_id:
                assert validate_params(op.parameter_schema, params) == [], op_id


def test_rotate_takes_references_not_passwords() -> None:
    schema = next(
        op for op in VROPS_FLEET_SECRETS_OPS if op.op_id == "vrops.fleet.password.rotate"
    ).parameter_schema
    assert validate_params(schema, {**_ROTATE_PARAMS, "new_password_ref": "plain-text"})
    assert validate_params(schema, {**_ROTATE_PARAMS, "newPassword": "x"})


def test_the_schema_refuses_a_private_key_without_quoting_it() -> None:
    """The check runs before the call waits for approval, so the key is never stored."""
    schema = next(
        op for op in VROPS_FLEET_SECRETS_OPS if op.op_id == "vrops.fleet.certificate.replace"
    ).parameter_schema
    chain = "-----BEGIN " + "PRIVATE KEY-----\nkey-body-123"  # split for the hook
    errors = validate_params(
        schema, {"certificateId": "c-1", "caType": "VMCA", "certificateChain": chain}
    )
    assert errors
    result = result_invalid_params("vrops.fleet.certificate.replace", errors, 1.0)
    assert "key-body-123" not in json.dumps(errors)
    assert "key-body-123" not in result.model_dump_json()


def test_password_rotate_is_a_credential_write() -> None:
    assert classify_op("vrops.fleet.password.rotate") == "credential_write"


# --- version check --------------------------------------------------------


@pytest.mark.parametrize("op_id", list(_TABLE))
@pytest.mark.asyncio
async def test_on_9_0_each_operation_stops_with_the_version_message(op_id: str) -> None:
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        with pytest.raises(NotImplementedError) as info:
            await _handler(op_id)(
                connector, _operator(), _StubTarget(version="9.0.0"), dict(_CALLS[op_id])
            )
        assert not mock.calls
    await connector.aclose()
    assert str(info.value) == (
        f"{op_id}: fleet certificates and passwords need VCF Operations 9.1 or newer; "
        "this target is 9.0.0."
    )


@pytest.mark.parametrize("version", ["9.1.0", "9.1.1", "9.1.1.0"])
@pytest.mark.parametrize("op_id", list(_TABLE))
@pytest.mark.asyncio
async def test_on_9_1_each_operation_runs(op_id: str, version: str, secrets: AsyncMock) -> None:
    del secrets
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        mock.post(f"{_CERTS}/query").respond(200, json={"vcfCertificateModels": []})
        mock.put(f"{_CERTS}/cert-1").respond(202, json=_ACCEPTED)
        mock.post(f"{_ACCOUNTS}/query").respond(200, json={"vcfPasswordAccounts": []})
        mock.put(f"{_ACCOUNTS}/acct-1/password").respond(200, json=_ACCEPTED)
        mock.get(f"{_REQUESTS}/req-1").respond(200, json=_ACCEPTED)
        result = await _handler(op_id)(
            connector, _operator(), _StubTarget(version=version), dict(_CALLS[op_id])
        )
    await connector.aclose()
    assert isinstance(result, dict)


def test_unknown_version_goes_on() -> None:
    assert checks.fleet_version_problem(_StubTarget(version=None), op_id="x") is None
    assert checks.fleet_version_problem(_StubTarget(version="latest"), op_id="x") is None


# --- lists ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_certificate_list_sends_filters_and_paging_and_returns_flat_rows() -> None:
    vendor = {
        "pageInfo": {"page": 0, "pageSize": 1000, "totalCount": 1},
        "links": [{"href": "/x", "rel": "SELF"}],
        "vcfCertificateModels": [
            {
                "certificateResourceKey": "cert-1",
                "appliance": "VCENTER",
                "applianceFqdn": "<appliance-address>",
                "status": "EXPIRING_30",
                "daysToExpire": 12,
                "autoRenewInfo": {"autoRenewStatus": "DISABLED"},
                "subjectAlternativeNames": {"dns": ["<name>"]},
            }
        ],
    }
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        route = mock.post(f"{_CERTS}/query").respond(200, json=vendor)
        result = await connector.fleet_certificate_list(
            _operator(), _StubTarget(), {"status": "EXPIRING_30", "sortOrder": "ASCENDING"}
        )
    await connector.aclose()

    request = route.calls[0].request
    assert json.loads(request.content) == {"status": "EXPIRING_30"}
    assert dict(request.url.params) == {"page": "0", "pageSize": "1000", "sortOrder": "ASCENDING"}
    assert request.headers["authorization"] == "OpsToken tok"
    assert result["total_count"] == 1
    (row,) = result["certificates"]
    assert row["certificateResourceKey"] == "cert-1"
    assert row["autoRenewStatus"] == "DISABLED"
    assert all(not isinstance(value, (dict, list)) for value in row.values())


@pytest.mark.asyncio
async def test_certificate_list_reads_one_certificate_by_id() -> None:
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        mock.get(f"{_CERTS}/cert%2F1").respond(200, json={"certificateResourceKey": "cert/1"})
        result = await connector.fleet_certificate_list(
            _operator(), _StubTarget(), {"certificateId": "cert/1"}
        )
    await connector.aclose()
    assert result["total_count"] == 1
    assert result["certificates"][0]["certificateResourceKey"] == "cert/1"


@pytest.mark.asyncio
async def test_password_list_keeps_only_known_fields() -> None:
    vendor = {
        "pageInfo": {"totalCount": 3},
        "vcfPasswordAccounts": [
            {
                "passwordAccountKey": "acct-1",
                "userName": "svc-a",
                "status": "EXPIRING",
                "applianceOpsResourceId": "res-1",
                "password": "must-not-leak",
                "newPassword": "must-not-leak",
            }
        ],
    }
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        route = mock.post(f"{_ACCOUNTS}/query").respond(200, json=vendor)
        result = await connector.fleet_password_list(
            _operator(), _StubTarget(), {"username": "svc-a", "pageSize": 50}
        )
    await connector.aclose()

    assert json.loads(route.calls[0].request.content) == {"username": "svc-a"}
    assert route.calls[0].request.url.params["pageSize"] == "50"
    assert result["total_count"] == 3
    (row,) = result["accounts"]
    assert set(row) == set(handlers.PASSWORD_ROW_FIELDS)
    assert "must-not-leak" not in json.dumps(result)


# --- changes --------------------------------------------------------------


@pytest.mark.asyncio
async def test_certificate_replace_sends_the_body_and_returns_accepted() -> None:
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        route = mock.put(f"{_CERTS}/cert-1").respond(202, json=_ACCEPTED)
        result = await connector.fleet_certificate_replace(
            _operator(),
            _StubTarget(),
            {
                "certificateId": "cert-1",
                "caType": "EXTERNAL_CA",
                "certificateChain": "-----BEGIN CERTIFICATE-----\n<pem>",
            },
        )
    await connector.aclose()

    assert json.loads(route.calls[0].request.content) == {
        "caType": "EXTERNAL_CA",
        "certificateChain": "-----BEGIN CERTIFICATE-----\n<pem>",
    }
    assert result["status"] == "accepted"
    assert result["started"] is True
    assert result["request_id"] == "req-1"
    assert result["status_read"] == "vrops.fleet.request.get"
    assert "not done" in result["note"]


@pytest.mark.asyncio
async def test_certificate_replace_refuses_a_private_key() -> None:
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        with pytest.raises(ValueError, match="private key"):
            await connector.fleet_certificate_replace(
                _operator(),
                _StubTarget(),
                {
                    "certificateId": "cert-1",
                    "caType": "EXTERNAL_CA",
                    "certificateChain": "-----BEGIN "
                    + "PRIVATE KEY-----\n<pem>",  # split for the hook
                },
            )
        assert not mock.calls
    await connector.aclose()


@pytest.mark.asyncio
async def test_a_running_request_blocks_a_second_start() -> None:
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        mock.get(f"{_REQUESTS}/req-0").respond(200, json={**_ACCEPTED, "state": "IN_PROGRESS"})
        start = mock.put(f"{_CERTS}/cert-1").respond(202, json=_ACCEPTED)
        result = await connector.fleet_certificate_replace(
            _operator(),
            _StubTarget(),
            {"certificateId": "cert-1", "caType": "VMCA", "previous_request_id": "req-0"},
        )
    await connector.aclose()

    assert not start.called
    assert result["status"] == "already_running"
    assert result["started"] is False
    assert result["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_an_ended_request_does_not_block_a_new_start() -> None:
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        mock.get(f"{_REQUESTS}/req-0").respond(200, json={**_ACCEPTED, "state": "FAILED"})
        start = mock.put(f"{_CERTS}/cert-1").respond(202, json=_ACCEPTED)
        result = await connector.fleet_certificate_replace(
            _operator(),
            _StubTarget(),
            {"certificateId": "cert-1", "caType": "VMCA", "previous_request_id": "req-0"},
        )
    await connector.aclose()
    assert start.called
    assert result["status"] == "accepted"


@pytest.mark.asyncio
async def test_password_rotate_reads_both_passwords_from_the_secret_store(
    secrets: AsyncMock,
) -> None:
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        route = mock.put(f"{_ACCOUNTS}/acct-1/password").respond(200, json=_ACCEPTED)
        result = await connector.fleet_password_rotate(
            _operator(), _StubTarget(), dict(_ROTATE_PARAMS)
        )
    await connector.aclose()

    assert json.loads(route.calls[0].request.content) == {
        "currentPassword": "old-pass-1",
        "newPassword": "new-pass-2",
        "userName": "svc-a",
    }
    assert [call.kwargs["param"] for call in secrets.await_args_list] == [
        "current_password_ref",
        "new_password_ref",
    ]
    assert result["status"] == "accepted"
    assert "pass-" not in json.dumps(result)


@pytest.mark.asyncio
async def test_password_rotate_refuses_a_missing_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "load_param_secret_data", AsyncMock(return_value={}))
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        start = mock.put(f"{_ACCOUNTS}/acct-1/password").respond(200, json=_ACCEPTED)
        with pytest.raises(ValueError, match="current_password_ref") as info:
            await connector.fleet_password_rotate(_operator(), _StubTarget(), dict(_ROTATE_PARAMS))
    await connector.aclose()
    assert not start.called
    assert "pass" not in str(info.value).replace("password_ref", "")


@pytest.mark.parametrize(
    ("op_id", "method", "path"),
    [
        ("vrops.fleet.certificate.replace", "PUT", f"{_CERTS}/cert-1"),
        ("vrops.fleet.password.rotate", "PUT", f"{_ACCOUNTS}/acct-1/password"),
    ],
)
@pytest.mark.asyncio
async def test_a_vendor_error_reaches_the_answer(
    op_id: str, method: str, path: str, secrets: AsyncMock
) -> None:
    del secrets
    vendor_error = {"message": "The new password does not meet the password policy."}
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        mock.request(method, path).respond(400, json=vendor_error)
        with pytest.raises((httpx.HTTPStatusError, ConnectorUpstreamError)) as info:
            await _handler(op_id)(connector, _operator(), _StubTarget(), dict(_CALLS[op_id]))
    await connector.aclose()

    result = result_connector_error(op_id, info.value, 1.0)
    assert "does not meet the password policy" in str(result.extras["upstream_message"])


@pytest.mark.parametrize(
    "echo",
    ["new-pass-2", "old-pass-1"],
)
@pytest.mark.asyncio
async def test_password_rotate_drops_vendor_text_that_repeats_a_password(
    echo: str, secrets: AsyncMock
) -> None:
    del secrets
    vendor_error = {
        "message": f"The password {echo} does not meet the policy.",
        "errorCause": [{"message": "Policy", "exceptionMessage": f"value={echo}"}],
    }
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        mock.put(f"{_ACCOUNTS}/acct-1/password").respond(422, json=vendor_error)
        with pytest.raises(ConnectorUpstreamError) as info:
            await connector.fleet_password_rotate(_operator(), _StubTarget(), dict(_ROTATE_PARAMS))
    await connector.aclose()

    exc = info.value
    assert exc.http_status == 422
    assert exc.upstream_message is None
    assert exc.__context__ is None
    assert exc.__cause__ is None
    result = result_connector_error("vrops.fleet.password.rotate", exc, 1.0)
    for secret in ("new-pass-2", "old-pass-1"):
        assert secret not in str(exc)
        assert secret not in result.model_dump_json()


@pytest.mark.asyncio
async def test_password_rotate_keeps_vendor_text_without_a_password(secrets: AsyncMock) -> None:
    del secrets
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        mock.put(f"{_ACCOUNTS}/acct-1/password").respond(
            400,
            json={
                "message": "Bad request.",
                "errorCause": [{"message": "Too short", "exceptionMessage": "min 15"}],
            },
        )
        with pytest.raises(ConnectorUpstreamError) as info:
            await connector.fleet_password_rotate(_operator(), _StubTarget(), dict(_ROTATE_PARAMS))
    await connector.aclose()
    assert info.value.upstream_message == "Bad request.; Too short — min 15"


@pytest.mark.asyncio
async def test_password_rotate_401_keeps_the_login_header_and_drops_the_body(
    secrets: AsyncMock,
) -> None:
    del secrets
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        mock.put(f"{_ACCOUNTS}/acct-1/password").respond(
            401, json={"message": "new-pass-2 rejected"}
        )
        with pytest.raises(httpx.HTTPStatusError) as info:
            await connector.fleet_password_rotate(_operator(), _StubTarget(), dict(_ROTATE_PARAMS))
    await connector.aclose()

    exc = info.value
    assert exc.response.status_code == 401
    assert exc.response.content == b""
    assert exc.request.content == b""
    assert exc.request.method == "PUT"
    assert exc.request.headers["Authorization"].endswith("tok")
    assert exc.__context__ is None
    assert "new-pass-2" not in str(exc)


# --- status read ------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_read_gives_state_outcome_and_error_text() -> None:
    vendor = {
        **_ACCEPTED,
        "state": "FAILED",
        "duration": 5300,
        "errorCause": [
            {
                "messageId": "E1",
                "message": "Certificate replacement failed",
                "exceptionMessage": "The appliance refused the chain.",
            }
        ],
    }
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        mock.get(f"{_REQUESTS}/req-1").respond(200, json=vendor)
        result = await connector.fleet_request_get(
            _operator(), _StubTarget(), {"request_id": "req-1"}
        )
    await connector.aclose()

    assert result["reachable"] is True
    assert result["state"] == "FAILED"
    assert result["outcome"] == "failed"
    assert result["ended"] is True
    assert result["error_text"] == (
        "Certificate replacement failed — The appliance refused the chain."
    )
    assert result["checked_at"]


@pytest.mark.asyncio
async def test_status_read_logs_in_again_after_the_login_expired() -> None:
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        login = mock.post(_ACQUIRE_PATH)
        login.side_effect = [
            httpx.Response(200, json={"token": "old"}),
            httpx.Response(200, json={"token": "new"}),
        ]
        read = mock.get(f"{_REQUESTS}/req-1")
        read.side_effect = [
            httpx.Response(401, json={"message": "expired"}),
            httpx.Response(200, json={**_ACCEPTED, "state": "IN_PROGRESS"}),
        ]
        result = await connector.fleet_request_get(
            _operator(), _StubTarget(), {"request_id": "req-1"}
        )
    await connector.aclose()

    assert login.call_count == 2
    assert read.calls[1].request.headers["authorization"] == "OpsToken new"
    assert result["outcome"] == "running"
    assert result["ended"] is False


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("connection refused"), httpx.Response(503)],
    ids=["no-answer", "service-unavailable"],
)
@pytest.mark.asyncio
async def test_status_read_says_not_reachable_with_the_id_and_time(
    failure: Any, no_retry_wait: None
) -> None:
    del no_retry_wait
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        mock.get(f"{_REQUESTS}/req-1").mock(side_effect=failure)
        result = await connector.fleet_request_get(
            _operator(), _StubTarget(), {"request_id": "req-1"}
        )
    await connector.aclose()

    assert result["reachable"] is False
    assert result["outcome"] == "not_reachable"
    assert result["request_id"] == "req-1"
    assert result["checked_at"]
    assert result["state"] != "FAILED"


@pytest.mark.asyncio
async def test_status_read_not_reachable_at_login(no_retry_wait: None) -> None:
    del no_retry_wait
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(503)
        result = await connector.fleet_request_get(
            _operator(), _StubTarget(), {"request_id": "req-1"}
        )
    await connector.aclose()
    assert result["outcome"] == "not_reachable"


@pytest.mark.asyncio
async def test_status_read_passes_a_not_found_on() -> None:
    connector = VcfOperationsConnector(credentials_loader=_stub_loader)
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "tok"})
        mock.get(f"{_REQUESTS}/req-1").respond(404)
        with pytest.raises(httpx.HTTPStatusError):
            await connector.fleet_request_get(_operator(), _StubTarget(), {"request_id": "req-1"})
    await connector.aclose()


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        ("CREATED", "queued"),
        ("IN_PROGRESS", "running"),
        ("COMPLETED", "succeeded"),
        ("FAILED", "failed"),
        ("SOMETHING_NEW", "unknown"),
        (None, "unknown"),
    ],
)
def test_request_outcome(state: Any, outcome: str) -> None:
    assert checks.request_outcome(state) == outcome


# --- previews ---------------------------------------------------------------


def _preview_ctx(op_id: str, params: dict[str, Any], version: str) -> PreviewContext:
    descriptor = type("D", (), {"op_id": op_id})()
    return PreviewContext(
        descriptor=descriptor,  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_StubTarget(version=version),
        params=params,
    )


@pytest.mark.asyncio
async def test_rotate_preview_shows_references_and_the_version_problem() -> None:
    builder = _PREVIEW_BUILDERS["vrops.fleet.password.rotate"]
    preview = await builder(_preview_ctx("vrops.fleet.password.rotate", _ROTATE_PARAMS, "9.0.0"))
    assert preview is not None
    assert preview["new_password_ref"] == "fleet/acct-1#next"
    assert "9.1 or newer" in preview["version_problem"]

    ok = await builder(_preview_ctx("vrops.fleet.password.rotate", _ROTATE_PARAMS, "9.1.1"))
    assert ok is not None
    assert "version_problem" not in ok


@pytest.mark.asyncio
async def test_replace_preview_shows_the_certificate_not_the_chain() -> None:
    builder = _PREVIEW_BUILDERS["vrops.fleet.certificate.replace"]
    params = {"certificateId": "cert-1", "caType": "EXTERNAL_CA", "certificateChain": "<pem>"}
    preview = await builder(_preview_ctx("vrops.fleet.certificate.replace", params, "9.1.0"))
    assert preview is not None
    assert preview["certificateId"] == "cert-1"
    assert preview["certificate_chain_chars"] == 5
    assert "<pem>" not in json.dumps(preview)
