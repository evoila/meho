# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared vim fake for the host certificate and TPM tests.

:class:`CertVim` is the teardown fake (:class:`tests._vmware_teardown_fake.VimFake`)
plus the certificate methods of vCenter and the host, and the TPM report.
The writes change the fake store, so the handlers' reads after a change see
the result. All names and ids are made up; the certificates are made in the
test with ``cryptography``.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from meho_backplane.auth.operator import Operator
from tests._vmware_teardown_fake import VimFake, http_status_error, moref

HOST = "host-1001"
OTHER_HOST = "host-1002"
CLUSTER = "domain-c7"
MANAGER = "certificateManager-1001"
VC_MANAGER = "certificateManager"
CSR_PEM = (
    "-----BEGIN CERTIFICATE REQUEST-----\n<signing-request>\n-----END CERTIFICATE REQUEST-----\n"
)
OLD_INFO: dict[str, Any] = {
    "_typeName": "HostCertificateManagerCertificateInfo",
    "issuer": "O=VMware, CN=CA",
    "subject": "CN=host.example.invalid",
    "notBefore": "2025-01-01T00:00:00Z",
    "notAfter": "2026-11-15T00:00:00Z",
    "status": "expiring",
}
NEW_INFO: dict[str, Any] = {**OLD_INFO, "notBefore": "2026-10-31T00:00:00Z", "status": "good"}
NEW_INFO["notAfter"] = "2031-10-31T00:00:00Z"
#: A text that looks like a pasted private key block (no real key). The marker
#: is joined at run time, so secret scanners do not read it as a key.
_KEY_MARKER = "PRIVATE" + " KEY"
FAKE_KEY_BLOCK = f"-----BEGIN {_KEY_MARKER}-----\n<key>\n-----END {_KEY_MARKER}-----"


def make_certificate(
    common_name: str = "host.example.invalid", *, days: int = 365, issuer: str = "Example CA"
) -> tuple[str, datetime, datetime]:
    """A self-signed PEM certificate with made-up names: ``(pem, not before, not after)``."""
    key = ec.generate_private_key(ec.SECP256R1())
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = start + timedelta(days=days)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode(), start, end


def info_for(not_before: datetime, not_after: datetime) -> dict[str, Any]:
    """The ``certificateInfo`` a host shows for a certificate with these dates."""
    return {
        **OLD_INFO,
        "notBefore": not_before.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "notAfter": not_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "good",
    }


class CertVim(VimFake):
    """The shared vim fake plus the certificate and TPM methods."""

    def __init__(self) -> None:
        super().__init__()
        self.ca_certs: list[str] = [
            "-----BEGIN CERTIFICATE-----\n<vmca-root>\n-----END CERTIFICATE-----"
        ]
        self.ca_crls: list[str] = ["-----BEGIN X509 CRL-----\n<vmca-crl>\n-----END X509 CRL-----"]
        #: method -> (fault type, text) a synchronous call fails with (HTTP 500).
        self.method_faults: dict[str, tuple[str, str]] = {}
        #: The certificate info the host shows after an install.
        self.installed_info: dict[str, Any] | None = None
        self.tpm_reports: dict[str, Any] = {}

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
        session_used: Any = None,
        session_token: str | None = None,
    ) -> Any:
        mo_type, moid, method = path.strip("/").split("/")
        handled = {
            "RetrieveServiceContent",
            "CertMgrRefreshCertificates_Task",
            "GenerateCertificateSigningRequest",
            "ListCACertificates",
            "ListCACertificateRevocationLists",
            "ReplaceCACertificatesAndCRLs",
            "InstallServerCertificate",
            "NotifyAffectedServices",
            "QueryTpmAttestationReport",
        }
        if method not in handled:
            return await super()._post_vmomi_json(
                target,
                path,
                operator=operator,
                json=json,
                promote_managed_object_not_found=promote_managed_object_not_found,
                session_used=session_used,
                session_token=session_token,
            )
        self.calls.append((path, copy.deepcopy(json)))
        if method in self.method_faults:
            fault_type, text = self.method_faults[method]
            raise http_status_error(
                500, f'{{"_typeName": "{fault_type}", "faultstring": "{text}"}}'
            )
        if method == "RetrieveServiceContent":
            return {"_typeName": "ServiceContent", "certificateManager": moref(mo_type, VC_MANAGER)}
        if method == "CertMgrRefreshCertificates_Task":
            hosts = [ref["value"] for ref in json["host"]]
            return self._task(method, apply=lambda: self._renew(hosts))
        if method == "GenerateCertificateSigningRequest":
            return CSR_PEM
        if method == "ListCACertificates":
            return list(self.ca_certs)
        if method == "ListCACertificateRevocationLists":
            return list(self.ca_crls)
        if method == "ReplaceCACertificatesAndCRLs":
            self.ca_certs = list(json["caCert"])
            self.ca_crls = list(json.get("caCrl") or [])
            return {}
        if method == "InstallServerCertificate":
            if self.installed_info is not None and self.apply_writes:
                self.objects[("HostCertificateManager", moid)]["certificateInfo"] = (
                    self.installed_info
                )
            return {}
        if method == "NotifyAffectedServices":
            return {}
        return copy.deepcopy(self.tpm_reports.get(moid))

    def _renew(self, hosts: list[str]) -> None:
        for host in hosts:
            manager = self.objects[("HostSystem", host)]["configManager.certificateManager"]
            self.objects[("HostCertificateManager", manager["value"])]["certificateInfo"] = (
                copy.deepcopy(NEW_INFO)
            )


def seed_host(
    conn: CertVim,
    host: str = HOST,
    *,
    state: str = "connected",
    info: dict[str, Any] | None = None,
    tpm: bool = True,
    attestation: dict[str, Any] | None = None,
) -> None:
    manager = f"certificateManager-{host.split('-')[1]}"
    conn.add(
        "HostSystem",
        host,
        name=f"<esxi-host-{host}>",
        **{
            "runtime.connectionState": state,
            "configManager.certificateManager": moref("HostCertificateManager", manager),
            "capability.tpmSupported": tpm,
            "capability.tpmVersion": "2.0" if tpm else None,
            "capability.txtEnabled": False,
            "summary.tpmAttestation": attestation,
        },
    )
    conn.add("HostCertificateManager", manager, certificateInfo=copy.deepcopy(info or OLD_INFO))


def seed_cluster(conn: CertVim, hosts: tuple[str, ...] = (HOST, OTHER_HOST)) -> None:
    conn.add(
        "ClusterComputeResource",
        CLUSTER,
        name="<cluster-name>",
        host={
            "_typeName": "ArrayOfManagedObjectReference",
            "_value": [moref("HostSystem", host) for host in hosts],
        },
    )
