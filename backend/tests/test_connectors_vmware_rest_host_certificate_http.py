# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host certificate and TPM operations over HTTP, on vCenter 8 and 9.

The real :class:`VmwareRestConnector` runs against ``respx``: the VI-JSON calls
go to ``/sdk/vim25/8.0.3.0/...`` on a vCenter 8.0 U3 and to
``/sdk/vim25/9.0.0.0/...`` on a vCenter 9.0. The vendor answers are written
by hand from the API reference, with made-up names and ids.

* the certificate read and the TPM read with its report;
* the renew: vCenter's certificate manager from the ServiceContent, the task
  polled to its end, the certificate read back;
* the signing request, and a vendor fault that reaches the error.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.composites import _host_certificate, _write
from tests._vmware_teardown_fake import GateRecorder

_BASE = "https://vc.test.invalid"
_HOST = "host-1001"
_MANAGER = "certificateManager-1001"
_RELEASES = {"8.0.3": "8.0.3.0", "9.0.0.0": "9.0.0.0"}
_MOREF = "ManagedObjectReference"


@dataclass
class _Target:
    name: str = "example-vcenter"
    host: str = "vc.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/example"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _operator() -> Operator:
    return Operator(
        sub="op-host-certificate-http",
        name="Host Certificate HTTP Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-000000004354"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "<service-account>", "password": "<service-password>"}


async def _close(connector: VmwareRestConnector) -> None:
    for client in connector._clients.values():
        await client.aclose()
    connector._clients.clear()


def _ref(mo_type: str, moid: str) -> dict[str, str]:
    return {"_typeName": _MOREF, "type": mo_type, "value": moid}


def _object(mo_type: str, moid: str, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "objects": [
            {
                "obj": _ref(mo_type, moid),
                "propSet": [{"name": name, "val": val} for name, val in props.items()],
            }
        ]
    }


def _info(not_after: str) -> dict[str, Any]:
    return {
        "_typeName": "HostCertificateManagerCertificateInfo",
        "issuer": "O=VMware, CN=CA",
        "subject": "CN=host.example.invalid",
        "notBefore": "2025-01-01T00:00:00Z",
        "notAfter": not_after,
        "status": "good",
    }


def _host() -> dict[str, Any]:
    return _object(
        "HostSystem",
        _HOST,
        {
            "name": "<esxi-host-name>",
            "runtime.connectionState": "connected",
            "configManager.certificateManager": _ref("HostCertificateManager", _MANAGER),
            "capability.tpmSupported": True,
            "capability.tpmVersion": "2.0",
            "capability.txtEnabled": False,
            "summary.tpmAttestation": {
                "_typeName": "HostTpmAttestationInfo",
                "time": "2026-10-30T08:00:00Z",
                "status": "accepted",
            },
        },
    )


def _retrieve_responder(answers: dict[str, Callable[[], dict[str, Any]]]) -> Any:
    """Answer each ``RetrievePropertiesEx`` by the type it reads."""

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        mo_type = body["specSet"][0]["propSet"][0]["type"]
        return httpx.Response(200, json=answers[mo_type]())

    return respond


def _mock_base(mock: respx.MockRouter, about: str) -> None:
    mock.get("/api/about").respond(200, json={"version": about})
    mock.post("/api/session").respond(200, json="tok")


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


@pytest.mark.parametrize(("about", "release"), sorted(_RELEASES.items()))
async def test_the_reads_run_on_the_vcenter_path_of_each_version(about: str, release: str) -> None:
    report = {
        "_typeName": "HostTpmAttestationReport",
        "tpmPcrValues": [{"_typeName": "HostTpmDigestInfo", "pcrNumber": 0}],
        "tpmEvents": [],
        "tpmLogReliable": True,
    }
    connector = VmwareRestConnector(session_loader=_loader)
    try:
        async with respx.mock(base_url=_BASE) as mock:
            _mock_base(mock, about)
            base = f"/sdk/vim25/{release}"
            mock.post(f"{base}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=_retrieve_responder(
                    {
                        "HostSystem": _host,
                        "HostCertificateManager": lambda: _object(
                            "HostCertificateManager",
                            _MANAGER,
                            {"certificateInfo": _info("2027-01-01T00:00:00Z")},
                        ),
                    }
                )
            )
            query = mock.post(f"{base}/HostSystem/{_HOST}/QueryTpmAttestationReport").respond(
                200, json=report
            )
            show = await connector.host_certificate_show(_operator(), _Target(), {"host": _HOST})
            tpm = await connector.host_tpm_attestation(
                _operator(), _Target(), {"host": _HOST, "include_report": True}
            )
        assert show["status"] == "ok"
        assert show["certificate"]["valid_to"] == "2027-01-01T00:00:00Z"
        assert query.called
        [row] = tpm["hosts"]
        assert row["attestation_status"] == "accepted"
        assert row["report_log_reliable"] is True
        assert row["report_pcr_count"] == 1
    finally:
        await _close(connector)


@pytest.mark.parametrize(("about", "release"), sorted(_RELEASES.items()))
async def test_renew_runs_on_the_vcenter_path_of_each_version(
    monkeypatch: pytest.MonkeyPatch, gate: GateRecorder, about: str, release: str
) -> None:
    monkeypatch.setattr(_host_certificate, "_TASK_TIMEOUT_SECONDS", 5.0)
    state = {"not_after": "2026-11-15T00:00:00Z"}

    def manager() -> dict[str, Any]:
        return _object(
            "HostCertificateManager", _MANAGER, {"certificateInfo": _info(state["not_after"])}
        )

    def task() -> dict[str, Any]:
        state["not_after"] = "2031-10-31T00:00:00Z"
        return _object("Task", "task-7", {"info": {"_typeName": "TaskInfo", "state": "success"}})

    connector = VmwareRestConnector(session_loader=_loader)
    try:
        async with respx.mock(base_url=_BASE) as mock:
            _mock_base(mock, about)
            base = f"/sdk/vim25/{release}"
            mock.post(f"{base}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=_retrieve_responder(
                    {"HostSystem": _host, "HostCertificateManager": manager, "Task": task}
                )
            )
            mock.post(f"{base}/ServiceInstance/ServiceInstance/RetrieveServiceContent").respond(
                200,
                json={
                    "_typeName": "ServiceContent",
                    "certificateManager": _ref("CertificateManager", "certificateManager"),
                },
            )
            renew = mock.post(
                f"{base}/CertificateManager/certificateManager/CertMgrRefreshCertificates_Task"
            ).respond(200, json=_ref("Task", "task-7"))
            out = await _host_certificate.host_certificate_renew_composite(
                operator=_operator(), target=_Target(), params={"host": _HOST}, connector=connector
            )
        assert renew.called
        assert json.loads(renew.calls.last.request.content) == {"host": [_ref("HostSystem", _HOST)]}
        assert out["status"] == "renewed"
        assert out["task"] == "task-7"
        assert out["certificate_after"]["valid_to"] == "2031-10-31T00:00:00Z"
    finally:
        await _close(connector)


@pytest.mark.parametrize(("about", "release"), sorted(_RELEASES.items()))
async def test_csr_runs_on_the_vcenter_path_of_each_version(
    gate: GateRecorder, about: str, release: str
) -> None:
    csr = "-----BEGIN CERTIFICATE REQUEST-----\n<request>\n-----END CERTIFICATE REQUEST-----\n"
    connector = VmwareRestConnector(session_loader=_loader)
    try:
        async with respx.mock(base_url=_BASE) as mock:
            _mock_base(mock, about)
            base = f"/sdk/vim25/{release}"
            mock.post(f"{base}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=_retrieve_responder(
                    {
                        "HostSystem": _host,
                        "HostCertificateManager": lambda: _object(
                            "HostCertificateManager",
                            _MANAGER,
                            {"certificateInfo": _info("2027-01-01T00:00:00Z")},
                        ),
                    }
                )
            )
            call = mock.post(
                f"{base}/HostCertificateManager/{_MANAGER}/GenerateCertificateSigningRequest"
            ).respond(200, json=csr)
            out = await _host_certificate.host_certificate_csr_composite(
                operator=_operator(), target=_Target(), params={"host": _HOST}, connector=connector
            )
        assert json.loads(call.calls.last.request.content) == {"useIpAddressAsCommonName": False}
        assert out["status"] == "generated"
        assert out["csr_pem"] == csr
    finally:
        await _close(connector)


async def test_csr_vendor_fault_reaches_the_error(gate: GateRecorder) -> None:
    fault = {
        "_typeName": "HostConfigFault",
        "faultstring": "Unable to generate the certificate signing request.",
    }
    connector = VmwareRestConnector(session_loader=_loader)
    try:
        async with respx.mock(base_url=_BASE) as mock:
            _mock_base(mock, "8.0.3")
            base = "/sdk/vim25/8.0.3.0"
            mock.post(f"{base}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=_retrieve_responder(
                    {
                        "HostSystem": _host,
                        "HostCertificateManager": lambda: _object(
                            "HostCertificateManager",
                            _MANAGER,
                            {"certificateInfo": _info("2027-01-01T00:00:00Z")},
                        ),
                    }
                )
            )
            mock.post(
                f"{base}/HostCertificateManager/{_MANAGER}/GenerateCertificateSigningRequest"
            ).respond(500, json=fault)
            with pytest.raises(RuntimeError) as err:
                await _host_certificate.host_certificate_csr_composite(
                    operator=_operator(),
                    target=_Target(),
                    params={"host": _HOST},
                    connector=connector,
                )
    finally:
        await _close(connector)
    assert "HostConfigFault" in str(err.value)
    assert "Unable to generate" in str(err.value)
