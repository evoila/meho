# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the governed ``installer.composite.sddc.bringup`` composite.

Exercises the validate → gate → deploy flow of
:func:`~meho_backplane.connectors.vcf_installer.bringup.installer_sddc_bringup_composite`
end to end against a respx-mocked Installer (``/v1/tokens``,
``/v1/sddcs/validations``, ``/v1/sddcs/validations/{id}``, ``/v1/sddcs``), with
:func:`enforce_subop_policy` monkeypatched by a recorder so the sub-op gate is
asserted without a DB. Covers:

* happy path (sync validation SUCCEEDED → gated deploy → ``deploying`` handle);
* async validation polled to terminal;
* ``FAILED`` validation aborts **before** any deploy;
* ``WARNING`` validation proceeds and surfaces the warnings;
* validation poll exhausting its wall-clock budget → ``validation_timeout``;
* the deploy gate parking → the handler returns the ``OperationResult`` verbatim
  and never fires the deploy;
* the preview echoing identity/network only, never a password;
* a missing ``spec`` raising a clear error;
* the write-path ``_post_json_with_session_retry`` 401 → re-login → retry;
* the handler satisfying the composite-registration signature contract.

All respx-mocked — no network, no DB.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel, OperationResult
from meho_backplane.connectors.vcf_installer import InstallerConnector, InstallerTargetLike
from meho_backplane.connectors.vcf_installer import bringup as _bringup

_TOKEN_PATH = "/v1/tokens"
_VALIDATE_PATH = "/v1/sddcs/validations"
_DEPLOY_PATH = "/v1/sddcs"
_BASE_URL = "https://installer-a.test.invalid"


def _make_operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _StubTarget:
    name: str = "installer-a"
    host: str = "installer-a.test.invalid"
    port: int | None = 443
    secret_ref: str = "installer/installer-a"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    id: UUID = field(default_factory=lambda: UUID(int=1))
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget()


async def _stub_loader(_target: InstallerTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "admin@local", "password": "stub-password"}


def _make_connector() -> InstallerConnector:
    return InstallerConnector(credentials_loader=_stub_loader)


@pytest.fixture(autouse=True)
def _clean_installer_registry() -> Iterator[None]:
    clear_registry()
    register_connector_v2(
        product=InstallerConnector.product,
        version=InstallerConnector.version,
        impl_id=InstallerConnector.impl_id,
        cls=InstallerConnector,
    )
    yield
    clear_registry()


class _GateRecorder:
    """Stand-in for :func:`enforce_subop_policy`; records calls, returns a verdict.

    ``verdict=None`` models an ``AUTO_EXECUTE`` gate (the handler proceeds with
    the write); an ``OperationResult`` models a parked/denied gate (the handler
    must return it verbatim without writing).
    """

    def __init__(self, verdict: OperationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._verdict = verdict

    async def __call__(
        self,
        *,
        operator: Operator,
        connector_id: str,
        op_id: str,
        safety_level: str,
        requires_approval: bool,
        target: Any,
        params: dict[str, Any],
    ) -> OperationResult | None:
        self.calls.append(
            {
                "connector_id": connector_id,
                "op_id": op_id,
                "safety_level": safety_level,
                "requires_approval": requires_approval,
                "params": params,
            }
        )
        return self._verdict


def _spec_with_secrets() -> dict[str, Any]:
    """A representative SddcSpec carrying passwords in every credential slot."""
    return {
        "sddcId": "mgmt-domain",
        "vcfInstanceName": "vcf-mgmt-01",
        "workflowType": "VCF",
        "vcenterSpec": {
            "vcenterHostname": "vc01.evba.lab",
            "ssoDomain": "vsphere.local",
            "rootVcenterPassword": "SECRET-VC-ROOT",
            "adminUserSsoPassword": "SECRET-SSO",
        },
        "nsxtSpec": {
            "vipFqdn": "nsx.evba.lab",
            "nsxtManagers": [{"hostname": "nsx-a.evba.lab"}],
            "transportVlanId": 14,
            "rootNsxtManagerPassword": "SECRET-NSX-ROOT",
            "nsxtAdminPassword": "SECRET-NSX-ADMIN",
        },
        "sddcManagerSpec": {
            "hostname": "sddcm.evba.lab",
            "rootPassword": "SECRET-SDDCM-ROOT",
            "sshPassword": "SECRET-SDDCM-SSH",
        },
        "dnsSpec": {"subdomain": "evba.lab", "nameservers": ["10.5.1.209"]},
        "ntpServers": ["10.5.1.209"],
        "networkSpecs": [
            {"networkType": "MANAGEMENT", "subnet": "10.11.16.0/20", "vlanId": 4003, "mtu": 1400},
        ],
        "hostSpecs": [
            {"hostname": "esx-dc13.evba.lab", "credentials": {"password": "SECRET-HOST"}},
        ],
    }


# --------------------------------------------------------------- happy path


@pytest.mark.asyncio
async def test_bringup_happy_path_validates_then_deploys(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _GateRecorder(verdict=None)  # AUTO_EXECUTE
    monkeypatch.setattr(_bringup, "enforce_subop_policy", gate)
    connector = _make_connector()
    spec = _spec_with_secrets()

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        validate_route = mock.post(_VALIDATE_PATH).respond(
            200, json={"id": "val-1", "executionStatus": "COMPLETED", "resultStatus": "SUCCEEDED"}
        )
        deploy_route = mock.post(_DEPLOY_PATH).respond(
            202, json={"id": "sddc-1", "status": "IN_PROGRESS", "name": "bringup-mgmt"}
        )
        result = await _bringup.installer_sddc_bringup_composite(
            operator=_make_operator(), target=_TARGET, params={"spec": spec}, connector=connector
        )

    assert isinstance(result, dict)
    assert result["status"] == "deploying"
    assert result["sddc_task"]["id"] == "sddc-1"
    assert result["sddc_task"]["status"] == "IN_PROGRESS"
    assert result["poll_with"] == "installer.sddc.bringup.status"
    assert result["validation_id"] == "val-1"
    assert result["validation_result_status"] == "SUCCEEDED"
    assert "validation_warnings" not in result
    # Validation and deploy both fired, each carrying the minted Bearer token.
    assert validate_route.call_count == 1
    assert deploy_route.call_count == 1
    assert json.loads(validate_route.calls[0].request.content)["sddcId"] == "mgmt-domain"
    assert json.loads(deploy_route.calls[0].request.content)["sddcId"] == "mgmt-domain"
    assert deploy_route.calls[0].request.headers["authorization"] == "Bearer tok-abc"
    # The deploy sub-op was gated with the right governance facts + secret-free params.
    assert len(gate.calls) == 1
    call = gate.calls[0]
    assert call["op_id"] == "installer.sddc.deploy"
    assert call["connector_id"] == "installer-rest-9.1"
    assert call["safety_level"] == "dangerous"
    assert call["requires_approval"] is False
    assert "SECRET" not in json.dumps(call["params"])
    await connector.aclose()


@pytest.mark.asyncio
async def test_bringup_polls_async_validation_to_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_bringup, "enforce_subop_policy", _GateRecorder(verdict=None))
    monkeypatch.setattr(_bringup, "_VALIDATION_POLL_INTERVAL", 0.0)
    connector = _make_connector()

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        mock.post(_VALIDATE_PATH).respond(
            202, json={"id": "val-1", "executionStatus": "IN_PROGRESS"}
        )
        poll_route = mock.get("/v1/sddcs/validations/val-1").respond(
            200, json={"id": "val-1", "executionStatus": "COMPLETED", "resultStatus": "SUCCEEDED"}
        )
        deploy_route = mock.post(_DEPLOY_PATH).respond(
            202, json={"id": "sddc-9", "status": "IN_PROGRESS"}
        )
        result = await _bringup.installer_sddc_bringup_composite(
            operator=_make_operator(),
            target=_TARGET,
            params={"spec": _spec_with_secrets()},
            connector=connector,
        )

    assert isinstance(result, dict)
    assert result["status"] == "deploying"
    assert result["sddc_task"]["id"] == "sddc-9"
    assert poll_route.call_count == 1
    assert deploy_route.call_count == 1
    await connector.aclose()


# --------------------------------------------------------------- validation gates the deploy


@pytest.mark.asyncio
async def test_bringup_aborts_on_validation_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _GateRecorder(verdict=None)
    monkeypatch.setattr(_bringup, "enforce_subop_policy", gate)
    connector = _make_connector()

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        mock.post(_VALIDATE_PATH).respond(
            200,
            json={
                "id": "val-2",
                "executionStatus": "COMPLETED",
                "resultStatus": "FAILED",
                "validationChecks": [
                    {
                        "description": "DNS forward/reverse mismatch for esx-dc13",
                        "severity": "ERROR",
                        "resultStatus": "FAILED",
                    },
                    {
                        "description": "NTP reachable",
                        "severity": "INFO",
                        "resultStatus": "SUCCEEDED",
                    },
                ],
            },
        )
        deploy_route = mock.post(_DEPLOY_PATH).respond(202, json={"id": "should-not-happen"})
        result = await _bringup.installer_sddc_bringup_composite(
            operator=_make_operator(),
            target=_TARGET,
            params={"spec": _spec_with_secrets()},
            connector=connector,
        )

    assert isinstance(result, dict)
    assert result["status"] == "validation_failed"
    assert result["result_status"] == "FAILED"
    assert result["validation_id"] == "val-2"
    assert [c["description"] for c in result["failed_checks"]] == [
        "DNS forward/reverse mismatch for esx-dc13"
    ]
    # The deploy never fired, and the gate was never consulted.
    assert deploy_route.call_count == 0
    assert gate.calls == []
    await connector.aclose()


@pytest.mark.asyncio
async def test_bringup_proceeds_on_warning_surfacing_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_bringup, "enforce_subop_policy", _GateRecorder(verdict=None))
    connector = _make_connector()

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        mock.post(_VALIDATE_PATH).respond(
            200,
            json={
                "id": "val-3",
                "executionStatus": "COMPLETED",
                "resultStatus": "WARNING",
                "validationChecks": [
                    {
                        "description": "Management network gateway ping skipped",
                        "severity": "WARNING",
                        "resultStatus": "WARNING",
                    }
                ],
            },
        )
        deploy_route = mock.post(_DEPLOY_PATH).respond(
            202, json={"id": "sddc-3", "status": "IN_PROGRESS"}
        )
        result = await _bringup.installer_sddc_bringup_composite(
            operator=_make_operator(),
            target=_TARGET,
            params={"spec": _spec_with_secrets()},
            connector=connector,
        )

    assert isinstance(result, dict)
    assert result["status"] == "deploying"
    assert deploy_route.call_count == 1
    assert result["validation_warnings"][0]["resultStatus"] == "WARNING"
    # The verdict is always echoed so a WARNING can never be silent (Finding 3).
    assert result["validation_result_status"] == "WARNING"
    await connector.aclose()


@pytest.mark.asyncio
async def test_bringup_times_out_when_validation_never_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _GateRecorder(verdict=None)
    monkeypatch.setattr(_bringup, "enforce_subop_policy", gate)
    monkeypatch.setattr(_bringup, "_VALIDATION_TIMEOUT_SECONDS", 0.0)
    connector = _make_connector()

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        mock.post(_VALIDATE_PATH).respond(
            202, json={"id": "val-4", "executionStatus": "IN_PROGRESS"}
        )
        deploy_route = mock.post(_DEPLOY_PATH).respond(202, json={"id": "should-not-happen"})
        result = await _bringup.installer_sddc_bringup_composite(
            operator=_make_operator(),
            target=_TARGET,
            params={"spec": _spec_with_secrets()},
            connector=connector,
        )

    assert isinstance(result, dict)
    assert result["status"] == "validation_timeout"
    assert result["execution_status"] == "IN_PROGRESS"
    assert deploy_route.call_count == 0
    assert gate.calls == []
    await connector.aclose()


# --------------------------------------------------------------- gate parks the deploy


@pytest.mark.asyncio
async def test_bringup_returns_gate_result_verbatim_when_parked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parked = OperationResult(status="pending", op_id="installer.sddc.deploy", duration_ms=0.0)
    monkeypatch.setattr(_bringup, "enforce_subop_policy", _GateRecorder(verdict=parked))
    connector = _make_connector()

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        mock.post(_VALIDATE_PATH).respond(
            200, json={"id": "val-5", "executionStatus": "COMPLETED", "resultStatus": "SUCCEEDED"}
        )
        deploy_route = mock.post(_DEPLOY_PATH).respond(202, json={"id": "should-not-happen"})
        result = await _bringup.installer_sddc_bringup_composite(
            operator=_make_operator(),
            target=_TARGET,
            params={"spec": _spec_with_secrets()},
            connector=connector,
        )

    assert result is parked  # returned verbatim, not wrapped
    assert deploy_route.call_count == 0  # write never fired while awaiting approval
    await connector.aclose()


# --------------------------------------------------------------- preview secret hygiene


@pytest.mark.asyncio
async def test_bringup_preview_echoes_identity_never_secrets() -> None:
    spec = _spec_with_secrets()
    ctx = SimpleNamespace(params={"spec": spec})
    preview = await _bringup._sddc_bringup_preview(ctx)  # type: ignore[arg-type]

    assert preview is not None
    assert preview["operation"] == "VCF management-domain bring-up"
    assert preview["sddc_id"] == "mgmt-domain"
    assert preview["vcenter_hostname"] == "vc01.evba.lab"
    assert preview["nsxt_vip_fqdn"] == "nsx.evba.lab"
    assert preview["host_count"] == 1
    assert preview["management_networks"][0]["vlanId"] == 4003
    # Not one password value or key from any credential slot leaks into the preview.
    dumped = json.dumps(preview)
    assert "SECRET" not in dumped
    for secret_key in (
        "rootVcenterPassword",
        "adminUserSsoPassword",
        "rootNsxtManagerPassword",
        "nsxtAdminPassword",
        "rootPassword",
        "sshPassword",
        "credentials",
    ):
        assert secret_key not in dumped


@pytest.mark.asyncio
async def test_bringup_preview_returns_none_without_spec() -> None:
    ctx = SimpleNamespace(params={})
    assert await _bringup._sddc_bringup_preview(ctx) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_bringup_retry_preview_echoes_the_task_id() -> None:
    """The plain retry parks with only the failed task id — the preview names
    the operation and that id, nothing else (#3123)."""
    ctx = SimpleNamespace(params={"id": "sddc-task-1"})
    preview = await _bringup._sddc_bringup_retry_preview(ctx)  # type: ignore[arg-type]

    assert preview == {
        "operation": "VCF bring-up retry (resume a failed installation task)",
        "sddc_task_id": "sddc-task-1",
    }


@pytest.mark.asyncio
async def test_bringup_retry_preview_edit_and_retry_echoes_identity_never_secrets() -> None:
    """Edit-and-retry appends the composite's blast-radius identity — and
    inherits its no-password property."""
    ctx = SimpleNamespace(params={"id": "sddc-task-1", "spec": _spec_with_secrets()})
    preview = await _bringup._sddc_bringup_retry_preview(ctx)  # type: ignore[arg-type]

    assert preview is not None
    assert preview["edit_and_retry"] is True
    assert preview["sddc_task_id"] == "sddc-task-1"
    assert preview["sddc_id"] == "mgmt-domain"
    assert preview["vcenter_hostname"] == "vc01.evba.lab"
    dumped = json.dumps(preview)
    assert "SECRET" not in dumped
    for secret_key in ("rootVcenterPassword", "credentials", "sshPassword"):
        assert secret_key not in dumped


@pytest.mark.asyncio
async def test_bringup_retry_preview_returns_none_without_id() -> None:
    ctx = SimpleNamespace(params={})
    assert await _bringup._sddc_bringup_retry_preview(ctx) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_depot_set_preview_echoes_identity_never_secrets() -> None:
    ctx = SimpleNamespace(
        params={
            "settings": {
                "offlineAccount": {"username": "depot-svc", "password": "SECRET-depot"},
                "depotConfiguration": {
                    "isOfflineDepot": True,
                    "hostname": "depot.test.invalid",
                    "port": 80,
                },
            }
        }
    )
    preview = await _bringup._depot_set_preview(ctx)  # type: ignore[arg-type]

    assert preview == {
        "operation": "Configure Installer release depot",
        "is_offline_depot": True,
        "hostname": "depot.test.invalid",
        "port": 80,
        "account": "offlineAccount",
        "account_username": "depot-svc",
    }
    dumped = json.dumps(preview)
    assert "SECRET" not in dumped
    assert "password" not in dumped


@pytest.mark.asyncio
async def test_depot_set_preview_returns_none_without_settings() -> None:
    ctx = SimpleNamespace(params={})
    assert await _bringup._depot_set_preview(ctx) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_trusted_certificate_add_preview_echoes_fingerprint_not_pem() -> None:
    import hashlib

    pem = "-----BEGIN CERTIFICATE-----\nMIIBdummy\n-----END CERTIFICATE-----\n"
    ctx = SimpleNamespace(params={"certificate": pem})
    preview = await _bringup._trusted_certificate_add_preview(ctx)  # type: ignore[arg-type]

    assert preview is not None
    assert preview["certificate_usage_type"] == "TRUSTED_FOR_OUTBOUND"
    assert preview["certificate_sha256"] == hashlib.sha256(pem.encode()).hexdigest()
    assert "BEGIN CERTIFICATE" not in json.dumps(preview)


@pytest.mark.asyncio
async def test_bundles_download_preview_truncates_past_twenty_ids() -> None:
    ids = [f"bundle-{i}" for i in range(25)]
    ctx = SimpleNamespace(params={"bundle_ids": ids})
    preview = await _bringup._bundles_download_preview(ctx)  # type: ignore[arg-type]

    assert preview is not None
    assert preview["bundle_count"] == 25
    assert len(preview["bundle_ids"]) == 20
    assert preview["bundle_ids_truncated"] == 5


# --------------------------------------------------------------- guards / plumbing


@pytest.mark.asyncio
async def test_bringup_missing_spec_raises() -> None:
    connector = _make_connector()
    with pytest.raises(ValueError, match=r"params\['spec'\]"):
        await _bringup.installer_sddc_bringup_composite(
            operator=_make_operator(), target=_TARGET, params={}, connector=connector
        )
    await connector.aclose()


@pytest.mark.asyncio
async def test_post_json_with_session_retry_relogins_on_401() -> None:
    connector = _make_connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        token = mock.post(_TOKEN_PATH)
        token.side_effect = [
            respx.MockResponse(201, json={"accessToken": "tok-1"}),
            respx.MockResponse(201, json={"accessToken": "tok-2"}),
        ]
        deploy = mock.post(_DEPLOY_PATH)
        deploy.side_effect = [
            respx.MockResponse(401, json={"message": "token expired"}),
            respx.MockResponse(202, json={"id": "sddc-7", "status": "IN_PROGRESS"}),
        ]
        result = await connector._post_json_with_session_retry(
            _TARGET, _DEPLOY_PATH, operator=_make_operator(), json={"sddcId": "x"}
        )

    assert result["id"] == "sddc-7"
    assert deploy.call_count == 2  # first 401, retried after re-login
    assert token.call_count == 2  # re-minted the token between attempts
    # The retry carried the FRESH token, not the stale one that 401'd.
    assert deploy.calls[0].request.headers["authorization"] == "Bearer tok-1"
    assert deploy.calls[1].request.headers["authorization"] == "Bearer tok-2"
    await connector.aclose()


@pytest.mark.asyncio
async def test_bringup_deploy_recovers_from_mid_deploy_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: a 401 on the deploy POST re-logs in and retries once — the
    composite completes without a double-launch surfacing to the caller."""
    monkeypatch.setattr(_bringup, "enforce_subop_policy", _GateRecorder(verdict=None))
    connector = _make_connector()

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        token = mock.post(_TOKEN_PATH)
        token.side_effect = [
            respx.MockResponse(201, json={"accessToken": "tok-1"}),
            respx.MockResponse(201, json={"accessToken": "tok-2"}),
        ]
        mock.post(_VALIDATE_PATH).respond(
            200, json={"id": "val-8", "executionStatus": "COMPLETED", "resultStatus": "SUCCEEDED"}
        )
        deploy = mock.post(_DEPLOY_PATH)
        deploy.side_effect = [
            respx.MockResponse(401, json={"message": "token expired mid-deploy"}),
            respx.MockResponse(202, json={"id": "sddc-8", "status": "IN_PROGRESS"}),
        ]
        result = await _bringup.installer_sddc_bringup_composite(
            operator=_make_operator(),
            target=_TARGET,
            params={"spec": _spec_with_secrets()},
            connector=connector,
        )

    assert isinstance(result, dict)
    assert result["status"] == "deploying"
    assert result["sddc_task"]["id"] == "sddc-8"
    assert deploy.call_count == 2
    assert deploy.calls[1].request.headers["authorization"] == "Bearer tok-2"
    await connector.aclose()


# --------------------------------------------------------------- classifier (fail-closed)


@pytest.mark.parametrize(
    ("validation", "expected"),
    [
        # Deploys only on COMPLETED + {SUCCEEDED, WARNING}.
        ({"executionStatus": "COMPLETED", "resultStatus": "SUCCEEDED"}, "pass"),
        ({"executionStatus": "COMPLETED", "resultStatus": "WARNING"}, "pass"),
        # Every other combination must abort (fail-closed), NOT deploy.
        ({"executionStatus": "COMPLETED", "resultStatus": "FAILED"}, "validation_failed"),
        ({"executionStatus": "COMPLETED", "resultStatus": "UNKNOWN"}, "validation_failed"),
        ({"executionStatus": "COMPLETED"}, "validation_failed"),  # missing resultStatus
        ({"executionStatus": "FAILED", "resultStatus": "FAILED"}, "validation_failed"),
        # A terminal execution FAILED must abort even if resultStatus reads SUCCEEDED.
        ({"executionStatus": "FAILED", "resultStatus": "SUCCEEDED"}, "validation_failed"),
        ({"executionStatus": "SKIPPED", "resultStatus": "SUCCEEDED"}, "validation_failed"),
        ({"executionStatus": "CANCELLED", "resultStatus": "SUCCEEDED"}, "validation_failed"),
        ({"executionStatus": "UNKNOWN", "resultStatus": "SUCCEEDED"}, "validation_failed"),
        ({}, "validation_failed"),  # empty / malformed
        # Still-running statuses map to timeout (poll budget spent).
        ({"executionStatus": "IN_PROGRESS"}, "validation_timeout"),
        ({"executionStatus": "CANCELLATION_IN_PROGRESS"}, "validation_timeout"),
    ],
)
def test_classify_validation_is_fail_closed(validation: dict[str, Any], expected: str) -> None:
    """Deploy only on COMPLETED+SUCCEEDED/WARNING; every other verdict aborts."""
    assert _bringup._classify_validation(validation).status == expected


# --------------------------------------------------------------- poll short-circuit


class _RecordingReader:
    """Minimal connector double recording ``_get_json_with_session_retry`` calls."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.get_calls = 0
        self._response = response

    async def _get_json_with_session_retry(
        self, _target: Any, _path: str, *, operator: Any
    ) -> dict[str, Any]:
        self.get_calls += 1
        return self._response


@pytest.mark.asyncio
async def test_await_validation_short_circuits_when_initial_has_no_id() -> None:
    """An IN_PROGRESS initial with no ``id`` returns as-is — no unpollable GET loop."""
    reader = _RecordingReader({"executionStatus": "COMPLETED", "resultStatus": "SUCCEEDED"})
    result = await _bringup._await_validation(
        reader,  # type: ignore[arg-type]
        _TARGET,
        _make_operator(),
        {"executionStatus": "IN_PROGRESS"},  # no id
    )
    assert result == {"executionStatus": "IN_PROGRESS"}
    assert reader.get_calls == 0


@pytest.mark.asyncio
async def test_await_validation_polls_to_terminal_via_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """An IN_PROGRESS initial WITH an id is polled once to its terminal state."""
    monkeypatch.setattr(_bringup, "_VALIDATION_POLL_INTERVAL", 0.0)
    reader = _RecordingReader({"executionStatus": "COMPLETED", "resultStatus": "SUCCEEDED"})
    result = await _bringup._await_validation(
        reader,  # type: ignore[arg-type]
        _TARGET,
        _make_operator(),
        {"id": "val-x", "executionStatus": "IN_PROGRESS"},
    )
    assert result["executionStatus"] == "COMPLETED"
    assert reader.get_calls == 1


def test_bringup_handler_signature_is_valid_for_composite_registration() -> None:
    """The handler declares ``connector`` so composite registration accepts it."""
    from meho_backplane.operations.typed_register import validate_composite_handler_signature

    validate_composite_handler_signature(_bringup.installer_sddc_bringup_composite)
