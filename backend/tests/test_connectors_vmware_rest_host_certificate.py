# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handler tests of the host certificate and TPM operations.

The handlers run against a recording fake of the connector's VI-JSON seam
(:class:`CertVim`, built on the shared teardown fake) with a stub of the
sub-op gate. Every answer is checked against the operation's response schema.

* the two reads: the certificate with its days left; the TPM rows of a
  cluster, with and without the report; refusals;
* each change: the success path (the vendor call, the read back), a vendor
  fault (the fault type and text reach the error), a parked gate, and the
  refusals that send nothing;
* the install: the CA chain is added to the CA certificates the host has,
  nothing is removed, a certificate with the dates the host already shows is
  still installed (the dates do not prove the identity), and the inputs
  refuse a private key.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.vmware_rest.composites import (
    _host_certificate,
    _host_certificate_install,
    _write,
)
from meho_backplane.connectors.vmware_rest.composites._host_certificate_schemas import (
    CERTIFICATE_CSR_RESPONSE_SCHEMA,
    CERTIFICATE_INSTALL_PARAMETER_SCHEMA,
    CERTIFICATE_INSTALL_RESPONSE_SCHEMA,
    CERTIFICATE_RENEW_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.host_certificate import map_certificate
from meho_backplane.connectors.vmware_rest.typed_ops_host_certificate import (
    VMWARE_HOST_CERTIFICATE_SHOW_OP,
    VMWARE_HOST_TPM_ATTESTATION_OP,
    host_certificate_show_impl,
    host_tpm_attestation_impl,
)
from meho_backplane.operations._validate import validate_params
from tests._vmware_host_certificate_fake import (
    CLUSTER,
    CSR_PEM,
    FAKE_KEY_BLOCK,
    HOST,
    MANAGER,
    NEW_INFO,
    OLD_INFO,
    OTHER_HOST,
    VC_MANAGER,
    CertVim,
    info_for,
    make_certificate,
    seed_cluster,
    seed_host,
)
from tests._vmware_teardown_fake import GateRecorder, moref, operator, parked, preview_ctx

_ESXI = SimpleNamespace(name="esxi", fingerprint={"product": "esxi", "reachable": True})
_VCENTER = SimpleNamespace(name="example-vcenter", fingerprint=None)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    monkeypatch.setattr(_host_certificate, "_TASK_TIMEOUT_SECONDS", 0.0)
    return recorder


def _check(schema: dict[str, Any] | None, out: Any) -> dict[str, Any]:
    assert isinstance(out, dict), out
    assert schema is not None
    Draft202012Validator(schema).validate(out)
    return out


# ===========================================================================
# Reads
# ===========================================================================


def test_map_certificate_counts_the_days_left() -> None:
    now = datetime(2026, 10, 31, tzinfo=UTC)
    out = map_certificate(OLD_INFO, now=now)
    assert out == {
        "kind": "Machine",
        "subject": "CN=host.example.invalid",
        "issuer": "O=VMware, CN=CA",
        "valid_from": "2025-01-01T00:00:00Z",
        "valid_to": "2026-11-15T00:00:00Z",
        "days_left": 15,
        "vcenter_status": "expiring",
    }
    assert map_certificate(None) is None


async def test_certificate_show_reads_the_certificate() -> None:
    conn = CertVim()
    seed_host(conn)
    out = await host_certificate_show_impl(conn, operator(), _VCENTER, {"host": HOST})  # type: ignore[arg-type]
    out = _check(VMWARE_HOST_CERTIFICATE_SHOW_OP.response_schema, out)
    assert out["status"] == "ok"
    assert out["host_name"] == f"<esxi-host-{HOST}>"
    assert out["certificate"]["issuer"] == "O=VMware, CN=CA"
    assert out["certificate"]["valid_to"] == "2026-11-15T00:00:00Z"
    assert isinstance(out["certificate"]["days_left"], int)
    assert conn.calls_to("GenerateCertificateSigningRequest") == []


async def test_certificate_show_answers_not_found_and_needs_vcenter() -> None:
    conn = CertVim()
    out = await host_certificate_show_impl(conn, operator(), _VCENTER, {"host": HOST})  # type: ignore[arg-type]
    assert _check(VMWARE_HOST_CERTIFICATE_SHOW_OP.response_schema, out)["status"] == "not_found"
    conn.calls.clear()
    out = await host_certificate_show_impl(conn, operator(), _ESXI, {"host": HOST})  # type: ignore[arg-type]
    assert _check(VMWARE_HOST_CERTIFICATE_SHOW_OP.response_schema, out)["status"] == (
        "needs_vcenter"
    )
    assert conn.calls == []


async def test_certificate_show_says_why_the_certificate_is_unreadable() -> None:
    conn = CertVim()
    seed_host(conn)
    conn.unreadable[("HostCertificateManager", MANAGER)] = {"certificateInfo"}
    out = await host_certificate_show_impl(conn, operator(), _VCENTER, {"host": HOST})  # type: ignore[arg-type]
    out = _check(VMWARE_HOST_CERTIFICATE_SHOW_OP.response_schema, out)
    assert out["status"] == "unreadable"
    assert out["certificate"] is None
    assert "certificateInfo" in out["read_note"]
    assert "Certificate.Manage" in out["guidance"]


async def test_tpm_attestation_gives_one_flat_row_per_host_of_the_cluster() -> None:
    conn = CertVim()
    seed_host(
        conn,
        attestation={
            "_typeName": "HostTpmAttestationInfo",
            "time": "2026-10-30T08:00:00Z",
            "status": "accepted",
        },
    )
    seed_host(
        conn,
        OTHER_HOST,
        attestation={
            "_typeName": "HostTpmAttestationInfo",
            "time": "2026-10-30T08:00:00Z",
            "status": "notAccepted",
            "message": {"_typeName": "LocalizableMessage", "key": "k", "message": "Bad boot."},
        },
    )
    seed_cluster(conn)
    out = await host_tpm_attestation_impl(conn, operator(), _VCENTER, {"cluster": CLUSTER})  # type: ignore[arg-type]
    out = _check(VMWARE_HOST_TPM_ATTESTATION_OP.response_schema, out)
    assert out["status"] == "ok"
    assert out["host_count"] == 2
    first, second = out["hosts"]
    assert first["host"] == HOST
    assert first["tpm_supported"] is True
    assert first["tpm_version"] == "2.0"
    assert first["attestation_status"] == "accepted"
    assert first["attestation_time"] == "2026-10-30T08:00:00Z"
    assert second["attestation_status"] == "notAccepted"
    assert second["attestation_message"] == "Bad boot."
    for row in out["hosts"]:
        assert all(not isinstance(value, dict | list) for value in row.values())
    assert conn.calls_to("QueryTpmAttestationReport") == []


async def test_tpm_attestation_adds_the_report_summary() -> None:
    conn = CertVim()
    seed_host(conn)
    conn.tpm_reports[HOST] = {
        "_typeName": "HostTpmAttestationReport",
        "tpmPcrValues": [
            {"pcrNumber": 0, "digestMethod": "SHA256", "digestValue": [1, 2]},
            {"pcrNumber": 1, "digestMethod": "SHA256", "digestValue": [3, 4]},
        ],
        "tpmEvents": [{"pcrIndex": 0}],
        "tpmLogReliable": True,
    }
    out = await host_tpm_attestation_impl(
        conn,
        operator(),
        _VCENTER,  # type: ignore[arg-type]
        {"host": HOST, "include_report": True},
    )
    [row] = _check(VMWARE_HOST_TPM_ATTESTATION_OP.response_schema, out)["hosts"]
    assert row["report_log_reliable"] is True
    assert row["report_pcr_count"] == 2
    assert row["report_event_count"] == 1
    [(path, _body)] = conn.calls_to("QueryTpmAttestationReport")
    assert path == f"/HostSystem/{HOST}/QueryTpmAttestationReport"


async def test_tpm_attestation_notes_a_host_without_a_report() -> None:
    conn = CertVim()
    seed_host(conn, tpm=False)
    out = await host_tpm_attestation_impl(
        conn,
        operator(),
        _VCENTER,  # type: ignore[arg-type]
        {"host": HOST, "include_report": True},
    )
    [row] = _check(VMWARE_HOST_TPM_ATTESTATION_OP.response_schema, out)["hosts"]
    assert row["tpm_supported"] is False
    assert row["report_pcr_count"] is None
    assert "no TPM attestation report" in row["read_note"]


async def test_tpm_attestation_answers_not_found_and_needs_vcenter() -> None:
    conn = CertVim()
    schema = VMWARE_HOST_TPM_ATTESTATION_OP.response_schema
    out = await host_tpm_attestation_impl(conn, operator(), _VCENTER, {"cluster": CLUSTER})  # type: ignore[arg-type]
    assert _check(schema, out)["status"] == "not_found"
    out = await host_tpm_attestation_impl(conn, operator(), _VCENTER, {"host": HOST})  # type: ignore[arg-type]
    assert _check(schema, out)["status"] == "not_found"
    out = await host_tpm_attestation_impl(conn, operator(), _ESXI, {"host": HOST})  # type: ignore[arg-type]
    assert _check(schema, out)["status"] == "needs_vcenter"


# ===========================================================================
# host.certificate.renew
# ===========================================================================


async def _renew(conn: CertVim, target: Any = None) -> Any:
    return await _host_certificate.host_certificate_renew_composite(
        operator=operator(), target=target, params={"host": HOST}, connector=conn
    )


async def test_renew_runs_the_vcenter_task_and_reads_the_new_certificate(
    gate: GateRecorder,
) -> None:
    conn = CertVim()
    seed_host(conn)
    out = _check(CERTIFICATE_RENEW_RESPONSE_SCHEMA, await _renew(conn))
    assert out["status"] == "renewed"
    assert out["certificate_before"]["valid_to"] == OLD_INFO["notAfter"]
    assert out["certificate_after"]["valid_to"] == NEW_INFO["notAfter"]
    assert out["task"] == "task-1"
    [(path, body)] = conn.calls_to("CertMgrRefreshCertificates_Task")
    assert path == f"/CertificateManager/{VC_MANAGER}/CertMgrRefreshCertificates_Task"
    assert body == {"host": [moref("HostSystem", HOST)]}
    assert gate.calls[0]["op_id"] == (
        "POST:/CertificateManager/{moId}/CertMgrRefreshCertificates_Task"
    )
    assert gate.calls[0]["params"] == {"host": HOST}


async def test_renew_task_fault_reaches_the_error(gate: GateRecorder) -> None:
    conn = CertVim()
    seed_host(conn)
    conn.task_faults["CertMgrRefreshCertificates_Task"] = (
        "CertificateManagementFault",
        "The host certificate management mode is not vmca.",
    )
    with pytest.raises(RuntimeError) as err:
        await _renew(conn)
    assert "CertificateManagementFault" in str(err.value)
    assert "not vmca" in str(err.value)


async def test_renew_says_not_verified_when_the_certificate_did_not_change(
    gate: GateRecorder,
) -> None:
    conn = CertVim()
    seed_host(conn)
    conn.apply_writes = False
    out = _check(CERTIFICATE_RENEW_RESPONSE_SCHEMA, await _renew(conn))
    assert out["status"] == "not_verified"


async def test_renew_timeout_returns_the_task_and_an_error_issue(gate: GateRecorder) -> None:
    conn = CertVim()
    seed_host(conn)
    conn.running.add("CertMgrRefreshCertificates_Task")
    out = _check(CERTIFICATE_RENEW_RESPONSE_SCHEMA, await _renew(conn))
    assert out["status"] == "timeout"
    assert out["issues"][0]["severity"] == "error"


async def test_renew_returns_a_parked_gate_and_sends_nothing() -> None:
    conn = CertVim()
    seed_host(conn)
    verdict = parked("vmware.composite.host.certificate.renew")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
        out = await _renew(conn)
    assert out is verdict
    assert conn.calls_to("CertMgrRefreshCertificates_Task") == []


@pytest.mark.parametrize(
    ("seed", "target", "status"),
    [
        (False, None, "not_found"),
        (True, _ESXI, "needs_vcenter"),
        ("disconnected", None, "not_connected"),
    ],
)
async def test_renew_refusals_send_nothing(
    gate: GateRecorder, seed: Any, target: Any, status: str
) -> None:
    conn = CertVim()
    if seed:
        seed_host(conn, state=seed if isinstance(seed, str) else "connected")
    out = _check(CERTIFICATE_RENEW_RESPONSE_SCHEMA, await _renew(conn, target))
    assert out["status"] == status
    assert gate.calls == []
    assert conn.calls_to("CertMgrRefreshCertificates_Task") == []


# ===========================================================================
# host.certificate.csr
# ===========================================================================


async def _csr(conn: CertVim, **params: Any) -> Any:
    return await _host_certificate.host_certificate_csr_composite(
        operator=operator(), target=None, params={"host": HOST, **params}, connector=conn
    )


async def test_csr_returns_the_signing_request_and_the_certificate_in_use(
    gate: GateRecorder,
) -> None:
    conn = CertVim()
    seed_host(conn)
    out = _check(
        CERTIFICATE_CSR_RESPONSE_SCHEMA, await _csr(conn, use_ip_address_as_common_name=True)
    )
    assert out["status"] == "generated"
    assert out["csr_pem"] == CSR_PEM
    assert out["common_name"] == "ip_address"
    assert out["certificate_after"] == out["certificate_before"]
    [(path, body)] = conn.calls_to("GenerateCertificateSigningRequest")
    assert path == f"/HostCertificateManager/{MANAGER}/GenerateCertificateSigningRequest"
    assert body == {"useIpAddressAsCommonName": True}
    assert gate.calls[0]["op_id"] == (
        "POST:/HostCertificateManager/{moId}/GenerateCertificateSigningRequest"
    )


async def test_csr_vendor_fault_reaches_the_error(gate: GateRecorder) -> None:
    conn = CertVim()
    seed_host(conn)
    conn.method_faults["GenerateCertificateSigningRequest"] = (
        "HostConfigFault",
        "Unable to generate the certificate signing request.",
    )
    with pytest.raises(RuntimeError) as err:
        await _csr(conn)
    assert "HostConfigFault" in str(err.value)
    assert "Unable to generate" in str(err.value)


async def test_csr_returns_a_parked_gate_and_sends_nothing() -> None:
    conn = CertVim()
    seed_host(conn)
    verdict = parked("vmware.composite.host.certificate.csr")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
        out = await _csr(conn)
    assert out is verdict
    assert conn.calls_to("GenerateCertificateSigningRequest") == []


# ===========================================================================
# host.certificate.install
# ===========================================================================


async def _install(conn: CertVim, **params: Any) -> Any:
    return await _host_certificate_install.host_certificate_install_composite(
        operator=operator(), target=None, params={"host": HOST, **params}, connector=conn
    )


async def test_install_adds_the_ca_chain_installs_and_reads_back(gate: GateRecorder) -> None:
    conn = CertVim()
    seed_host(conn)
    pem, start, end = make_certificate()
    ca_pem, _start, _end = make_certificate("Example CA")
    old_cas, old_crls = list(conn.ca_certs), list(conn.ca_crls)
    conn.installed_info = info_for(start, end)
    out = _check(
        CERTIFICATE_INSTALL_RESPONSE_SCHEMA,
        await _install(conn, certificate_pem=pem, ca_certificates_pem=[ca_pem]),
    )
    assert out["status"] == "installed"
    assert out["ca_certificates_added"] == 1
    assert out["services_notified"] is True
    assert out["certificate_wanted"]["valid_to"] == end.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert out["certificate_after"]["valid_to"] == end.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert [m for m in conn.methods() if "Certificate" in m or "Notify" in m] == [
        "ListCACertificates",
        "ListCACertificateRevocationLists",
        "ReplaceCACertificatesAndCRLs",
        "InstallServerCertificate",
        "NotifyAffectedServices",
    ]
    [(_path, replace)] = conn.calls_to("ReplaceCACertificatesAndCRLs")
    assert replace == {"caCert": [*old_cas, ca_pem], "caCrl": old_crls}
    [(_path, install)] = conn.calls_to("InstallServerCertificate")
    assert install == {"cert": pem}
    assert [call["op_id"].rsplit("/", 1)[-1] for call in gate.calls] == [
        "ReplaceCACertificatesAndCRLs",
        "InstallServerCertificate",
        "NotifyAffectedServices",
    ]


async def test_install_on_vcenter_8_0_u2_does_not_notify_the_services(
    gate: GateRecorder,
) -> None:
    conn = CertVim()
    seed_host(conn)
    pem, start, end = make_certificate()
    conn.installed_info = info_for(start, end)
    old_vcenter = SimpleNamespace(name="old-vcenter", fingerprint={"version": "8.0.2"})
    out = await _host_certificate_install.host_certificate_install_composite(
        operator=operator(),
        target=old_vcenter,
        params={"host": HOST, "certificate_pem": pem},
        connector=conn,
    )
    out = _check(CERTIFICATE_INSTALL_RESPONSE_SCHEMA, out)
    assert out["status"] == "installed"
    assert out["services_notified"] is False
    assert "8.0.3" in out["guidance"]
    assert "hostd" in out["guidance"]
    assert conn.calls_to("InstallServerCertificate")
    assert conn.calls_to("NotifyAffectedServices") == []


async def test_install_reports_a_failed_notify_after_the_install(gate: GateRecorder) -> None:
    conn = CertVim()
    seed_host(conn)
    pem, start, end = make_certificate()
    conn.installed_info = info_for(start, end)
    conn.method_faults["NotifyAffectedServices"] = (
        "NotSupported",
        "The operation is not supported.",
    )
    out = _check(CERTIFICATE_INSTALL_RESPONSE_SCHEMA, await _install(conn, certificate_pem=pem))
    assert out["status"] == "installed"
    assert out["services_notified"] is False
    assert "NotSupported" in out["guidance"]


async def test_install_does_not_add_a_ca_the_host_already_has(gate: GateRecorder) -> None:
    conn = CertVim()
    seed_host(conn)
    pem, start, end = make_certificate()
    conn.installed_info = info_for(start, end)
    known, _start, _end = make_certificate("Example CA")
    conn.ca_certs = [known]
    out = _check(
        CERTIFICATE_INSTALL_RESPONSE_SCHEMA,
        await _install(conn, certificate_pem=pem, ca_certificates_pem=[f"\n{known}\n"]),
    )
    assert out["status"] == "installed"
    assert out["ca_certificates_added"] == 0
    assert conn.calls_to("ReplaceCACertificatesAndCRLs") == []


async def test_install_sends_a_certificate_with_the_dates_the_host_already_shows(
    gate: GateRecorder,
) -> None:
    """Matching dates do not prove the host has this certificate (another key or subject)."""
    conn = CertVim()
    pem, start, end = make_certificate("other.example.invalid")
    seed_host(conn, info=info_for(start, end))
    conn.installed_info = info_for(start, end)
    out = _check(CERTIFICATE_INSTALL_RESPONSE_SCHEMA, await _install(conn, certificate_pem=pem))
    assert out["status"] == "installed"
    [(_path, install)] = conn.calls_to("InstallServerCertificate")
    assert install == {"cert": pem}
    assert conn.calls_to("NotifyAffectedServices")


async def test_install_adds_the_ca_chain_when_the_dates_already_match(
    gate: GateRecorder,
) -> None:
    conn = CertVim()
    pem, start, end = make_certificate()
    ca_pem, _start, _end = make_certificate("Example CA")
    seed_host(conn, info=info_for(start, end))
    conn.installed_info = info_for(start, end)
    old_cas, old_crls = list(conn.ca_certs), list(conn.ca_crls)
    out = _check(
        CERTIFICATE_INSTALL_RESPONSE_SCHEMA,
        await _install(conn, certificate_pem=pem, ca_certificates_pem=[ca_pem]),
    )
    assert out["status"] == "installed"
    assert out["ca_certificates_added"] == 1
    [(_path, replace)] = conn.calls_to("ReplaceCACertificatesAndCRLs")
    assert replace == {"caCert": [*old_cas, ca_pem], "caCrl": old_crls}
    assert conn.calls_to("InstallServerCertificate")


async def test_install_fault_reaches_the_error(gate: GateRecorder) -> None:
    conn = CertVim()
    seed_host(conn)
    pem, _start, _end = make_certificate()
    conn.method_faults["InstallServerCertificate"] = (
        "HostConfigFault",
        "The certificate does not match the private key.",
    )
    with pytest.raises(RuntimeError) as err:
        await _install(conn, certificate_pem=pem)
    assert "HostConfigFault" in str(err.value)
    assert "does not match" in str(err.value)
    assert conn.calls_to("NotifyAffectedServices") == []


async def test_install_says_not_verified_when_the_dates_do_not_read_back(
    gate: GateRecorder,
) -> None:
    conn = CertVim()
    seed_host(conn)
    pem, _start, _end = make_certificate()
    out = _check(CERTIFICATE_INSTALL_RESPONSE_SCHEMA, await _install(conn, certificate_pem=pem))
    assert out["status"] == "not_verified"


async def test_install_refuses_text_that_is_not_a_certificate(gate: GateRecorder) -> None:
    conn = CertVim()
    seed_host(conn)
    bad = "-----BEGIN CERTIFICATE-----\nnot-base64\n-----END CERTIFICATE-----"
    out = _check(CERTIFICATE_INSTALL_RESPONSE_SCHEMA, await _install(conn, certificate_pem=bad))
    assert out["status"] == "invalid_certificate"
    assert conn.calls == []


async def test_install_returns_a_parked_gate_and_installs_nothing() -> None:
    conn = CertVim()
    seed_host(conn)
    pem, _start, _end = make_certificate()
    verdict = parked("vmware.composite.host.certificate.install")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_write, "enforce_subop_policy", GateRecorder(verdict))
        out = await _install(conn, certificate_pem=pem)
    assert out is verdict
    assert conn.calls_to("InstallServerCertificate") == []


def test_install_inputs_refuse_a_private_key() -> None:
    pem, _start, _end = make_certificate()
    key = FAKE_KEY_BLOCK
    assert (
        validate_params(
            CERTIFICATE_INSTALL_PARAMETER_SCHEMA, {"host": HOST, "certificate_pem": pem}
        )
        == []
    )
    assert validate_params(
        CERTIFICATE_INSTALL_PARAMETER_SCHEMA, {"host": HOST, "certificate_pem": f"{pem}\n{key}"}
    )
    assert validate_params(
        CERTIFICATE_INSTALL_PARAMETER_SCHEMA,
        {"host": HOST, "certificate_pem": pem, "ca_certificates_pem": [f"{pem}{key}"]},
    )


# ===========================================================================
# Previews
# ===========================================================================


@pytest.mark.parametrize(
    "op_id",
    [
        "vmware.composite.host.certificate.renew",
        "vmware.composite.host.certificate.csr",
        "vmware.composite.host.certificate.install",
    ],
)
async def test_preview_shows_the_host_and_its_certificate(op_id: str) -> None:
    conn = CertVim()
    seed_host(conn)
    pem, _start, end = make_certificate()
    ctx = preview_ctx({"host": HOST, "certificate_pem": pem}, conn)
    ctx = dataclasses.replace(ctx, descriptor=SimpleNamespace(op_id=op_id))
    preview = await _host_certificate._certificate_preview(ctx)
    assert preview is not None
    assert preview["host_name"] == f"<esxi-host-{HOST}>"
    assert preview["certificate_now"]["valid_to"] == OLD_INFO["notAfter"]
    assert preview["what_happens_next"]
    if op_id.endswith("install"):
        assert preview["certificate_wanted"]["valid_to"] == end.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert conn.calls_to("InstallServerCertificate") == []


async def test_preview_lists_a_missing_host_and_never_raises() -> None:
    conn = CertVim()
    preview = await _host_certificate._certificate_preview(preview_ctx({"host": HOST}, conn))
    assert preview is not None
    assert preview["unavailable"] == ["the host: vCenter has no host with this id"]
