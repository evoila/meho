# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the fleet locker ops and the product certificate replace.

The five ops of the ``fleet-rest`` connector (Aria Suite Lifecycle 8.x and
VCF 9.0 fleet management): list and add locker passwords, list and import
or generate locker certificates, and replace a product's certificate. HTTP
is mocked with ``respx``; the Vault read is mocked. All names and ids are
made up.

What is checked:

* the risk and approval of each op, and the credential-write pin of the two
  ops that send a secret;
* no answer holds a password or a private key;
* a secret input is a Vault reference, and a bad one stops the op before any
  call;
* each change waits for approval when a person calls it;
* the vendor's error text reaches the answer (minus any secret);
* the replace job: accepted, a request that already runs, a failed
  pre-validation, a vendor error and a lost reply at the replace step, an
  appliance that does not answer, and a pre-validation that does not end.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.vcf_fleet import FLEET_TYPED_OPS, VcfFleetConnector
from meho_backplane.connectors.vcf_fleet import locker as locker_module
from meho_backplane.connectors.vcf_fleet import product_certificate as replace_module
from meho_backplane.connectors.vcf_fleet.locker import FleetLockerVendorError
from meho_backplane.connectors.vcf_fleet.typed_ops_locker import (
    FLEET_LOCKER_OPS,
    FLEET_LOCKER_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.settings import get_settings

_HOST = "fleet-locker.test.invalid"
_BASE = f"https://{_HOST}"
_PASSWORD = "Pa55-w0rd-for-the-locker-test"  # NOSONAR -- made-up test value
# Built from parts so the secret scanners do not flag this made-up key.
_KEY = (
    "-----BEGIN " + "PRIVATE KEY-----\n"
    "made-up-key-line-for-the-locker-test-0001\n"
    "-----END " + "PRIVATE KEY-----"
)
_ENV = "env-0001"
_PRODUCT = "vrops"
_CERT = "cert-0001"
_PUT = f"/lcm/lcops/api/v2/environments/{_ENV}/products/{_PRODUCT}/certificates/{_CERT}"
_PRE = f"{_PUT}/pre-validate"
_REQUESTS = "/lcm/request/api/v2/requests"
_REPORT = "/lcm/request/api/v2/requests/pre-0001/pre-validation-report"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Target:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.tenant_id = UUID("00000000-0000-0000-0000-000000004473")
        self.name = "fleet-locker"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/fleet/fleet-locker"
        self.auth_model = "shared_service_account"


_OPERATOR = Operator(
    sub="op-fleet-locker",
    name="Fleet Locker Operator",
    email=None,
    raw_jwt="<test-raw-jwt>",
    tenant_id=UUID("00000000-0000-0000-0000-000000004473"),
    tenant_role=TenantRole.OPERATOR,
)


async def _stub_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "admin@local", "password": "fleet-login-pw"}  # NOSONAR -- test stub


@pytest.fixture
async def connector() -> AsyncIterator[VcfFleetConnector]:
    instance = VcfFleetConnector(credentials_loader=_stub_loader)
    yield instance
    await instance.aclose()


def _vault(data: dict[str, Any]) -> Any:
    return patch.object(locker_module, "load_param_secret_data", AsyncMock(return_value=data))


def _op(op_id: str) -> Any:
    return next(op for op in FLEET_LOCKER_OPS if op.op_id == op_id)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

_RISK = {
    "fleet.locker.password.list": ("safe", False),
    "fleet.locker.password.add": ("caution", True),
    "fleet.locker.certificate.list": ("safe", False),
    "fleet.locker.certificate.import": ("caution", True),
    "fleet.product.certificate.replace": ("dangerous", True),
}


@pytest.mark.parametrize("op_id", sorted(_RISK))
def test_risk_and_approval_match_the_table(op_id: str) -> None:
    op = _op(op_id)
    assert (op.safety_level, op.requires_approval) == _RISK[op_id]
    assert op in FLEET_TYPED_OPS
    assert op.group_key in FLEET_LOCKER_WHEN_TO_USE_BY_GROUP


@pytest.mark.parametrize("op_id", ["fleet.locker.password.add", "fleet.locker.certificate.import"])
def test_secret_sending_ops_are_credential_writes(op_id: str) -> None:
    assert classify_op(op_id) == "credential_write"
    assert "credential" in _op(op_id).tags


@pytest.mark.parametrize("op", FLEET_LOCKER_OPS, ids=lambda op: op.op_id)
def test_examples_are_valid_calls(op: Any) -> None:
    Draft202012Validator.check_schema(op.parameter_schema)
    validator = Draft202012Validator(op.parameter_schema)
    for example in op.parameter_schema["examples"]:
        assert list(validator.iter_errors(example)) == [], example


def test_import_schema_needs_the_fields_of_its_mode() -> None:
    validator = Draft202012Validator(_op("fleet.locker.certificate.import").parameter_schema)
    assert not validator.is_valid({"mode": "import", "alias": "<a>"})
    assert not validator.is_valid({"mode": "generate", "alias": "<a>"})
    assert not validator.is_valid({"mode": "import", "alias": "<a>", "private_key": "x"})


# ---------------------------------------------------------------------------
# Locker passwords
# ---------------------------------------------------------------------------


def _password_entry(index: int) -> dict[str, Any]:
    return {
        "vmid": f"pw-{index:04d}",
        "alias": f"alias-{index}",
        "userName": "admin",
        "password": "PASSWORD****",
        "passwordDescription": None,
        "referenced": index % 2 == 0,
        "createdOn": 1767225600000,
        "lastUpdatedOn": 1767225600000,
        "principal": "admin@local",
        "transactionId": "tx",
    }


async def test_password_list_reads_every_page_and_never_returns_a_password(
    connector: VcfFleetConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locker_module, "PAGE_SIZE", 2)
    pages = [
        {"page": 0, "total": 3, "passwords": [_password_entry(0), _password_entry(1)]},
        {"page": 1, "total": 3, "passwords": [_password_entry(2)]},
    ]
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        route = mock.get("/lcm/locker/api/v2/passwords").mock(
            side_effect=[httpx.Response(200, json=page) for page in pages]
        )
        result = await connector.locker_password_list(_OPERATOR, _Target(), {})
    assert [row["vmid"] for row in result["passwords"]] == ["pw-0000", "pw-0001", "pw-0002"]
    assert result["count"] == 3 and result["total"] == 3
    assert "PASSWORD" not in json.dumps(result)
    assert all("password" not in row for row in result["passwords"])
    assert route.calls[1].request.url.params["from"] == "2"


async def test_password_list_passes_the_alias_filter(connector: VcfFleetConnector) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        route = mock.get("/lcm/locker/api/v2/passwords").respond(
            200, json={"page": 0, "total": 0, "passwords": []}
        )
        result = await connector.locker_password_list(_OPERATOR, _Target(), {"alias": "alias-1"})
    assert result == {"passwords": [], "count": 0, "total": 0}
    assert route.calls[0].request.url.params["aliasQuery"] == "alias-1"


_ADD = {"alias": "alias-new", "user_name": "admin", "password_ref": "kv/fleet/new#password"}


async def test_password_add_sends_the_vault_password_and_returns_none(
    connector: VcfFleetConnector,
) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        route = mock.post("/lcm/locker/api/v2/passwords").respond(
            201, json={**_password_entry(7), "alias": "alias-new", "password": _PASSWORD}
        )
        with _vault({"password": _PASSWORD + "\n"}) as vault:
            result = await connector.locker_password_add(
                _OPERATOR, _Target(), {**_ADD, "description": "for a test"}
            )
    sent = json.loads(route.calls[0].request.content)
    assert sent == {
        "alias": "alias-new",
        "userName": "admin",
        "password": _PASSWORD,
        "passwordDescription": "for a test",
    }
    assert vault.await_args.kwargs["param"] == "password_ref"
    assert vault.await_args.args[0].secret_ref == "kv/fleet/new"
    assert result["status"] == "added" and result["vmid"] == "pw-0007"
    assert _PASSWORD not in json.dumps(result)


async def test_password_add_vendor_error_reaches_the_answer(
    connector: VcfFleetConnector,
) -> None:
    error = {"errorCode": "LCMLOCKER10003", "message": "Alias alias-new already exists"}
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.post("/lcm/locker/api/v2/passwords").respond(409, json=error)
        with _vault({"password": _PASSWORD}), pytest.raises(FleetLockerVendorError) as caught:
            await connector.locker_password_add(_OPERATOR, _Target(), _ADD)
    assert caught.value.http_status == 409
    assert "Alias alias-new already exists" in caught.value.remediation
    assert "LCMLOCKER10003" in caught.value.remediation


async def test_password_add_vendor_error_that_repeats_the_password_is_hidden(
    connector: VcfFleetConnector,
) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.post("/lcm/locker/api/v2/passwords").respond(
            400, json={"message": f"bad password {_PASSWORD}"}
        )
        with _vault({"password": _PASSWORD}), pytest.raises(FleetLockerVendorError) as caught:
            await connector.locker_password_add(_OPERATOR, _Target(), _ADD)
    assert _PASSWORD not in caught.value.remediation
    assert _PASSWORD not in str(caught.value.upstream_message)
    assert "MEHO hid this text" in caught.value.remediation


@pytest.mark.parametrize("ref", [None, "", "no-field", "a#b#c", "#field", "path#", "pa\nth#f"])
async def test_bad_password_ref_stops_before_any_call(
    connector: VcfFleetConnector, ref: Any
) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        route = mock.post("/lcm/locker/api/v2/passwords").respond(201, json={})
        with _vault({"password": _PASSWORD}) as vault, pytest.raises(ValueError):
            await connector.locker_password_add(_OPERATOR, _Target(), {**_ADD, "password_ref": ref})
    assert not route.called and not vault.called


async def test_empty_vault_field_stops_before_any_call(connector: VcfFleetConnector) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        route = mock.post("/lcm/locker/api/v2/passwords").respond(201, json={})
        with _vault({"password": None}), pytest.raises(ValueError, match="has no text"):
            await connector.locker_password_add(_OPERATOR, _Target(), _ADD)
    assert not route.called


# ---------------------------------------------------------------------------
# Locker certificates
# ---------------------------------------------------------------------------


async def test_certificate_list_returns_flat_rows(connector: VcfFleetConnector) -> None:
    entry = {
        "vmid": "cert-0001",
        "alias": "cert-alias",
        "subject": "CN=<fqdn>",
        "issuer": "CN=<ca>",
        "algorithm": "SHA256withRSA",
        "sha1": "AA:BB",
        "healthy": True,
        "referenced": True,
        "transactionId": "tx",
        "validity": {"expiresOn": "2027-01-01"},
        "key": _KEY,
    }
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get("/lcm/locker/api/v2/certificates").respond(
            200, json={"page": 0, "total": 1, "certificates": [entry]}
        )
        result = await connector.locker_certificate_list(_OPERATOR, _Target(), {})
    row = result["certificates"][0]
    assert row["alias"] == "cert-alias" and row["expiresOn"] == "2027-01-01"
    assert "key" not in row and "PRIVATE KEY" not in json.dumps(result)


_CERT_ANSWER = {
    "vmid": "cert-0002",
    "alias": "cert-new",
    "certChain": "-----BEGIN CERTIFICATE-----\nchain\n-----END CERTIFICATE-----",
    "leafCert": "-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----",
    "key": _KEY,
    "certInfo": {"subject": "CN=<fqdn>", "issuer": "CN=<ca>", "sha256": "CC"},
    "validity": {"issuedOn": "2026-01-01", "expiresOn": "2028-01-01", "healthy": True},
    "validations": [],
}


async def test_certificate_import_reads_the_key_from_vault(connector: VcfFleetConnector) -> None:
    params = {
        "mode": "import",
        "alias": "cert-new",
        "certificate_chain": "<pem-chain>",
        "private_key_ref": "kv/fleet/cert#key",
        "passphrase_ref": "kv/fleet/cert#passphrase",
    }
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        route = mock.post("/lcm/locker/api/v2/certificates/import").respond(200, json=_CERT_ANSWER)
        with _vault({"key": _KEY, "passphrase": "phrase-123"}):
            result = await connector.locker_certificate_import(_OPERATOR, _Target(), params)
    sent = json.loads(route.calls[0].request.content)
    assert sent == {
        "alias": "cert-new",
        "certificateChain": "<pem-chain>",
        "privateKey": _KEY,
        "passphrase": "phrase-123",
    }
    assert result["status"] == "imported" and result["vmid"] == "cert-0002"
    assert result["validity"]["expiresOn"] == "2028-01-01"
    text = json.dumps(result)
    assert "PRIVATE KEY" not in text and "BEGIN CERTIFICATE" not in text


async def test_certificate_generate_maps_the_fields_and_drops_the_key(
    connector: VcfFleetConnector,
) -> None:
    params = {
        "mode": "generate",
        "alias": "cert-new",
        "common_name": "<fqdn>",
        "hostnames": ["<fqdn>"],
        "ip_addresses": ["<ip>"],
        "organization": "<org>",
        "country": "<cc>",
        "key_size": 2048,
    }
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        route = mock.post("/lcm/locker/api/v2/certificates").respond(200, json=_CERT_ANSWER)
        with _vault({}) as vault:
            result = await connector.locker_certificate_import(_OPERATOR, _Target(), params)
    assert json.loads(route.calls[0].request.content) == {
        "alias": "cert-new",
        "cN": "<fqdn>",
        "host": ["<fqdn>"],
        "ip": ["<ip>"],
        "o": "<org>",
        "c": "<cc>",
        "size": 2048,
    }
    assert not vault.called
    assert result["status"] == "generated" and "PRIVATE KEY" not in json.dumps(result)


async def test_certificate_import_vendor_error_hides_a_key_line(
    connector: VcfFleetConnector,
) -> None:
    key_line = _KEY.splitlines()[1]
    params = {
        "mode": "import",
        "alias": "cert-new",
        "certificate_chain": "<pem-chain>",
        "private_key_ref": "kv/fleet/cert#key",
    }
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.post("/lcm/locker/api/v2/certificates/import").respond(
            400, json={"message": f"cannot parse key {key_line}"}
        )
        with _vault({"key": _KEY}), pytest.raises(FleetLockerVendorError) as caught:
            await connector.locker_certificate_import(_OPERATOR, _Target(), params)
    assert key_line not in caught.value.remediation
    assert caught.value.http_status == 400


# ---------------------------------------------------------------------------
# Product certificate replace (a vendor job)
# ---------------------------------------------------------------------------

_REPLACE = {"environment_id": _ENV, "product_id": _PRODUCT, "certificate_id": _CERT}
_REPORT_OK = {"requestId": "pre-0001", "requestState": "COMPLETED", "overallStatus": True}


@pytest.fixture
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replace_module, "PRE_VALIDATION_POLL_SECONDS", 0.0)


async def test_replace_is_accepted_with_the_request_id(
    connector: VcfFleetConnector, _fast_poll: None
) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get(_REQUESTS).respond(200, json=[])
        pre = mock.post(_PRE).respond(200, json={"requestId": "pre-0001"})
        mock.get(_REPORT).mock(
            side_effect=[
                httpx.Response(404, json={"message": "not yet"}),
                httpx.Response(200, json={**_REPORT_OK, "requestState": "INPROGRESS"}),
                httpx.Response(200, json=_REPORT_OK),
            ]
        )
        put = mock.put(_PUT).respond(200, json={"requestId": "req-0001"})
        result = await connector.product_certificate_replace(_OPERATOR, _Target(), _REPLACE)
    assert result["status"] == "accepted"
    assert result["request_id"] == "req-0001"
    assert result["pre_validation_request_id"] == "pre-0001"
    assert result["status_read"] == "fleet.request.get"
    assert [s["result"] for s in result["steps"]] == ["done", "done", "done", "done"]
    assert pre.called and put.called


async def test_replace_returns_a_running_request_and_starts_nothing(
    connector: VcfFleetConnector,
) -> None:
    running = {
        "vmid": "req-running",
        "state": "INPROGRESS",
        "requestType": "PRODUCT_CERTIFICATE_UPDATE",
        "inputMap": {"environmentId": _ENV, "productId": _PRODUCT},
    }
    other = {**running, "vmid": "req-other", "inputMap": {"environmentId": "env-9"}}
    done = {**running, "vmid": "req-done", "state": "COMPLETED"}
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get(_REQUESTS).respond(200, json=[other, done, running])
        pre = mock.post(_PRE).respond(200, json={"requestId": "pre-0001"})
        put = mock.put(_PUT).respond(200, json={"requestId": "req-0001"})
        result = await connector.product_certificate_replace(_OPERATOR, _Target(), _REPLACE)
    assert result["status"] == "already_running"
    assert result["request_id"] == "req-running"
    assert result["status_read"] == "fleet.request.get"
    assert not pre.called and not put.called


async def test_replace_stops_on_a_failed_pre_validation(
    connector: VcfFleetConnector, _fast_poll: None
) -> None:
    report = {
        "requestId": "pre-0001",
        "requestState": "COMPLETED",
        "overallStatus": False,
        "rootValidations": [
            {
                "checkName": "Certificate",
                "status": "SUCCESS",
                "childElements": [
                    {
                        "checkName": "Host names",
                        "status": "ERROR",
                        "resultDescription": "The certificate does not cover <fqdn>",
                        "recommendations": ["Add <fqdn> to the certificate"],
                    }
                ],
            }
        ],
    }
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get(_REQUESTS).respond(200, json=[])
        mock.post(_PRE).respond(200, json={"requestId": "pre-0001"})
        mock.get(_REPORT).respond(200, json=report)
        put = mock.put(_PUT).respond(200, json={"requestId": "req-0001"})
        result = await connector.product_certificate_replace(_OPERATOR, _Target(), _REPLACE)
    assert result["status"] == "failed"
    assert result["failed_step"] == "wait_for_pre_validation"
    checks = result["pre_validation"]["failing_checks"]
    assert checks == [
        {
            "check": "Host names",
            "status": "ERROR",
            "result": "The certificate does not cover <fqdn>",
            "recommendations": ["Add <fqdn> to the certificate"],
        }
    ]
    assert [s["result"] for s in result["steps"]] == ["done", "done", "failed", "not_run"]
    assert not put.called


async def test_replace_vendor_error_at_step_four_lists_every_step(
    connector: VcfFleetConnector, _fast_poll: None
) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get(_REQUESTS).respond(200, json=[])
        mock.post(_PRE).respond(200, json={"requestId": "pre-0001"})
        mock.get(_REPORT).respond(200, json=_REPORT_OK)
        mock.put(_PUT).respond(
            500, json={"errorCode": "LCMVROPS1001", "message": "Product is busy"}
        )
        result = await connector.product_certificate_replace(_OPERATOR, _Target(), _REPLACE)
    assert result["status"] == "failed" and result["failed_step"] == "replace"
    assert "Product is busy" in result["error"] and "HTTP 500" in result["error"]
    assert [s["result"] for s in result["steps"]] == ["done", "done", "done", "failed"]


async def test_replace_vendor_error_at_pre_validate_lists_what_did_not_run(
    connector: VcfFleetConnector,
) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get(_REQUESTS).respond(200, json=[])
        mock.post(_PRE).respond(400, json={"message": "Certificate not found"})
        result = await connector.product_certificate_replace(_OPERATOR, _Target(), _REPLACE)
    assert result["failed_step"] == "pre_validate"
    assert "Certificate not found" in result["error"]
    assert [s["result"] for s in result["steps"]] == ["done", "failed", "not_run", "not_run"]


async def test_replace_lost_reply_says_unknown(
    connector: VcfFleetConnector, _fast_poll: None
) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get(_REQUESTS).respond(200, json=[])
        mock.post(_PRE).respond(200, json={"requestId": "pre-0001"})
        mock.get(_REPORT).respond(200, json=_REPORT_OK)
        mock.put(_PUT).mock(side_effect=httpx.ReadTimeout("read timed out"))
        result = await connector.product_certificate_replace(_OPERATOR, _Target(), _REPLACE)
    assert result["status"] == "unknown"
    assert result["steps"][3]["result"] == "unknown"
    assert result["status_read"] == "fleet.request.get"


async def test_replace_on_an_appliance_that_does_not_answer(
    connector: VcfFleetConnector,
) -> None:
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get(_REQUESTS).mock(side_effect=httpx.ConnectError("connection refused"))
        pre = mock.post(_PRE).respond(200, json={"requestId": "pre-0001"})
        result = await connector.product_certificate_replace(_OPERATOR, _Target(), _REPLACE)
    assert result["status"] == "failed"
    assert result["failed_step"] == "check_running_requests"
    assert result["error"].startswith("not reachable at ")
    assert not pre.called


async def test_replace_stops_when_the_pre_validation_does_not_end(
    connector: VcfFleetConnector, monkeypatch: pytest.MonkeyPatch, _fast_poll: None
) -> None:
    monkeypatch.setattr(replace_module, "PRE_VALIDATION_WAIT_SECONDS", 0.0)
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get(_REQUESTS).respond(200, json=[])
        mock.post(_PRE).respond(200, json={"requestId": "pre-0001"})
        mock.get(_REPORT).respond(200, json={**_REPORT_OK, "requestState": "INPROGRESS"})
        put = mock.put(_PUT).respond(200, json={"requestId": "req-0001"})
        result = await connector.product_certificate_replace(_OPERATOR, _Target(), _REPLACE)
    assert result["status"] == "failed"
    assert result["failed_step"] == "wait_for_pre_validation"
    assert result["pre_validation_request_id"] == "pre-0001"
    assert not put.called


@pytest.mark.parametrize("bad", ["", "a/b", "a\tb"])
async def test_replace_refuses_a_bad_id(connector: VcfFleetConnector, bad: str) -> None:
    with pytest.raises(ValueError):
        await connector.product_certificate_replace(
            _OPERATOR, _Target(), {**_REPLACE, "environment_id": bad}
        )


# ---------------------------------------------------------------------------
# Each change waits for approval (full dispatch, seeded target)
# ---------------------------------------------------------------------------


@pytest.fixture
async def _registered(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    from meho_backplane.connectors.registry import clear_registry, register_connector_v2
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import Target as TargetORM
    from meho_backplane.operations import reset_dispatcher_caches
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product="fleet", version="9.0", impl_id="fleet-rest", cls=VcfFleetConnector
    )
    await VcfFleetConnector.register_operations()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_OPERATOR.tenant_id,
                name="fleet-locker",
                aliases=[],
                product="fleet",
                host=_HOST,
                port=443,
                fqdn=None,
                secret_ref="targets/fleet/fleet-locker",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "8.18"},
                preferred_impl_id="fleet-rest",
                notes="seeded by test_connectors_vcf_fleet_locker",
            )
        )
        await session.commit()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("fleet.locker.password.add", _ADD),
        (
            "fleet.locker.certificate.import",
            {"mode": "generate", "alias": "cert-new", "common_name": "<fqdn>"},
        ),
        ("fleet.product.certificate.replace", _REPLACE),
    ],
)
async def test_each_change_waits_for_approval(
    _registered: None, op_id: str, params: dict[str, Any]
) -> None:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations import dispatch
    from meho_backplane.targets.resolver import resolve_target

    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _OPERATOR.tenant_id, "fleet-locker")
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        sent = mock.route().respond(200, json={})
        result = await dispatch(
            operator=_OPERATOR,
            connector_id="fleet-rest-9.0",
            op_id=op_id,
            target=target,
            params=params,
            _approved=False,
        )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "awaiting_approval", dumped
    assert dumped["extras"].get("approval_request_id")
    assert not sent.called


# ---------------------------------------------------------------------------
# Approval previews of the two credential writes
# ---------------------------------------------------------------------------


def _preview_ctx(params: dict[str, Any]) -> Any:
    from unittest.mock import MagicMock

    from meho_backplane.operations._preview import PreviewContext

    ctx = MagicMock(spec=PreviewContext)
    ctx.params = params
    ctx.target = _Target()
    return ctx


async def test_password_add_preview_shows_the_reference_not_a_value() -> None:
    from meho_backplane.operations._preview import _PREVIEW_BUILDERS

    builder = _PREVIEW_BUILDERS["fleet.locker.password.add"]
    assert await builder(_preview_ctx(_ADD)) == {
        "target": "fleet-locker",
        "alias": "alias-new",
        "user_name": "admin",
        "description": None,
        "password_ref": "kv/fleet/new#password",
    }
    assert await builder(_preview_ctx({**_ADD, "password_ref": "no-field"})) is None


async def test_certificate_import_preview_never_shows_a_key() -> None:
    from meho_backplane.operations._preview import _PREVIEW_BUILDERS

    builder = _PREVIEW_BUILDERS["fleet.locker.certificate.import"]
    imported = await builder(
        _preview_ctx(
            {
                "mode": "import",
                "alias": "cert-new",
                "certificate_chain": "<pem-chain>",
                "private_key_ref": "kv/fleet/cert#key",
            }
        )
    )
    assert imported == {
        "target": "fleet-locker",
        "mode": "import",
        "alias": "cert-new",
        "certificate_chain_bytes": len("<pem-chain>"),
        "private_key_ref": "kv/fleet/cert#key",
    }
    generated = await builder(
        _preview_ctx({"mode": "generate", "alias": "cert-new", "common_name": "<fqdn>"})
    )
    assert generated == {
        "target": "fleet-locker",
        "mode": "generate",
        "alias": "cert-new",
        "cN": "<fqdn>",
    }
    assert await builder(_preview_ctx({"mode": "other", "alias": "cert-new"})) is None
