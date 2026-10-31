# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host certificate changes: renew, signing request and install.

vCenter has no REST path for these, so each one sends VIM methods as VI-JSON
through the governed write seam (``_write_vmomi_sub_op``):

* ``host.certificate.renew`` -- ``CertificateManager.CertMgrRefreshCertificates_Task``
  on vCenter's certificate manager: vCenter gets a signing request from the
  host, signs it with its own certificate authority (VMCA) and installs the
  new certificate. The task is polled to its end.
* ``host.certificate.csr`` -- ``HostCertificateManager.GenerateCertificateSigningRequest``:
  the host makes a new key and a signing request (CSR). The key stays on the
  host; the current certificate stays in use until an install.
* ``host.certificate.install`` (in :mod:`._host_certificate_install`) -- optionally
  ``HostCertificateManager.ReplaceCACertificatesAndCRLs`` (the CA chain is
  added to the CA certificates the host already trusts; nothing is removed),
  then ``InstallServerCertificate`` and ``NotifyAffectedServices`` so the
  host's services use the new certificate now.

Each change reads the host and its certificate first, and reads the
certificate again at the end; the answer gives both. A vendor fault fails the
call with the fault type and text. No input or answer holds a private key:
the input schemas refuse any text with ``PRIVATE KEY``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx
from cryptography import x509

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest._version_gate import version_problem
from meho_backplane.connectors.vmware_rest.composites._write import (
    _task_timeout_issue,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.host_certificate import (
    HOST_SYSTEM_MO_TYPE,
    HostInfo,
    needs_vcenter,
    read_certificate,
    read_host,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    fault_message,
    unwrap_vim_value,
    vim_moref,
)
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task
from meho_backplane.operations._preview import preview_failure_reason, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "OP_ID_HOST_CERTIFICATE_CSR",
    "OP_ID_HOST_CERTIFICATE_INSTALL",
    "OP_ID_HOST_CERTIFICATE_RENEW",
    "host_certificate_csr_composite",
    "host_certificate_renew_composite",
    "wanted_certificate",
]

OP_ID_HOST_CERTIFICATE_RENEW: Final = "vmware.composite.host.certificate.renew"
OP_ID_HOST_CERTIFICATE_CSR: Final = "vmware.composite.host.certificate.csr"
OP_ID_HOST_CERTIFICATE_INSTALL: Final = "vmware.composite.host.certificate.install"

# The vendor calls (``METHOD:/path`` of the pinned vi-json.yaml).
OP_RETRIEVE_SERVICE_CONTENT: Final = "POST:/ServiceInstance/{moId}/RetrieveServiceContent"
OP_REFRESH_CERTIFICATES: Final = "POST:/CertificateManager/{moId}/CertMgrRefreshCertificates_Task"
OP_GENERATE_CSR: Final = "POST:/HostCertificateManager/{moId}/GenerateCertificateSigningRequest"
OP_LIST_CA_CERTIFICATES: Final = "POST:/HostCertificateManager/{moId}/ListCACertificates"
OP_LIST_CA_CRLS: Final = "POST:/HostCertificateManager/{moId}/ListCACertificateRevocationLists"
OP_REPLACE_CA_CERTIFICATES: Final = (
    "POST:/HostCertificateManager/{moId}/ReplaceCACertificatesAndCRLs"
)
OP_INSTALL_SERVER_CERTIFICATE: Final = (
    "POST:/HostCertificateManager/{moId}/InstallServerCertificate"
)
OP_NOTIFY_AFFECTED_SERVICES: Final = "POST:/HostCertificateManager/{moId}/NotifyAffectedServices"

_SERVICE_CONTENT_PATH: Final = "/ServiceInstance/ServiceInstance/RetrieveServiceContent"
_CONNECTED: Final = "connected"

#: Wall-clock bound of the renew task poll (the 600 s convention); tests zero it.
_TASK_TIMEOUT_SECONDS = 600.0


# ---------------------------------------------------------------------------
# Shared steps
# ---------------------------------------------------------------------------


def _answer(
    status: str,
    host: str,
    info: HostInfo | None,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    task: str | None = None,
    guidance: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "status": status,
        "host": host,
        "host_name": info.name if info else None,
        "connection_state": info.connection_state if info else None,
        "certificate_before": before,
        "certificate_after": after,
        **extra,
        "task": task,
        "guidance": guidance,
    }


async def _connected_host(
    connector: VmwareRestConnector, target: Any, operator: Operator, host: str
) -> tuple[HostInfo | None, dict[str, Any] | None]:
    """``(host info, None)`` for a connected host, else ``(info or None, refusal answer)``."""
    refusal = needs_vcenter(target)
    if refusal is not None:
        return None, _answer("needs_vcenter", host, None, guidance=refusal)
    info = await read_host(connector, target, operator, host)
    if info is None:
        return None, _answer(
            "not_found",
            host,
            None,
            guidance=f"vCenter has no host {host!r}. Nothing was sent. Check the host id.",
        )
    if info.connection_state != _CONNECTED or info.certificate_manager is None:
        return info, _answer(
            "not_connected",
            host,
            info,
            guidance=(
                "The host is not connected to vCenter. Nothing was sent. Connect it first "
                "(vmware.composite.host.connect)."
            ),
        )
    return info, None


def _vendor_fault(label: str, method: str, exc: httpx.HTTPStatusError) -> RuntimeError:
    """A ``RuntimeError`` with the fault type and text of a refused VIM method."""
    try:
        body = exc.response.json()
    except ValueError:
        body = None
    fault = unwrap_vim_value(body) if isinstance(body, dict) else None
    fault_type = fault.get(VIM_TYPE_NAME_KEY) if isinstance(fault, dict) else None
    text = fault_message(fault) if isinstance(fault, dict) else None
    return RuntimeError(
        f"{label}: {method} faulted ({fault_type or f'HTTP {exc.response.status_code}'}): "
        f"{text or '<no fault text reported>'}"
    )


async def _send(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    vmomi_path: str,
    body: dict[str, Any],
    params: dict[str, Any],
    label: str,
) -> tuple[OperationResult | None, Any]:
    """Gate and send one VIM method; a vendor fault raises with its type and text."""
    try:
        return await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=op_id,
            vmomi_path=vmomi_path,
            body=body,
            params=params,
        )
    except httpx.HTTPStatusError as exc:
        raise _vendor_fault(label, vmomi_path.rsplit("/", 1)[-1], exc) from exc


def _moref_value(value: Any) -> str | None:
    if isinstance(value, dict) and isinstance(value.get("value"), str) and value["value"]:
        return str(value["value"])
    return None


async def _vcenter_certificate_manager(
    connector: VmwareRestConnector, target: Any, operator: Operator
) -> str:
    """The moid of vCenter's ``CertificateManager`` (``ServiceContent.certificateManager``)."""
    raw = await connector._post_vmomi_json(
        target, _SERVICE_CONTENT_PATH, operator=operator, json={}
    )
    content = unwrap_vim_value(_unwrap_value(raw))
    manager = _moref_value(content.get("certificateManager")) if isinstance(content, dict) else None
    if manager is None:
        raise RuntimeError(
            "host.certificate.renew: vCenter has no certificate manager (ServiceContent."
            "certificateManager is unset); renew needs a vCenter, not an ESXi host"
        )
    return manager


# ---------------------------------------------------------------------------
# host.certificate.renew
# ---------------------------------------------------------------------------


def _changed(before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
    if before is None or after is None:
        return False
    return any(before.get(k) != after.get(k) for k in ("valid_from", "valid_to", "issuer"))


async def host_certificate_renew_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Renew a host certificate from the vCenter certificate authority (VMCA)."""
    host = params["host"]
    info, refusal = await _connected_host(connector, target, operator, host)
    if refusal is not None:
        return refusal
    assert info is not None and info.certificate_manager is not None  # narrowed by refusal
    before = await read_certificate(connector, target, operator, info.certificate_manager)
    manager = await _vcenter_certificate_manager(connector, target, operator)
    gate, payload = await _send(
        connector,
        target,
        operator,
        op_id=OP_REFRESH_CERTIFICATES,
        vmomi_path=f"/CertificateManager/{manager}/CertMgrRefreshCertificates_Task",
        body={"host": [vim_moref(HOST_SYSTEM_MO_TYPE, host)]},
        params={"host": host},
        label="host.certificate.renew",
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(payload),
        timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"host.certificate.renew: CertMgrRefreshCertificates_Task faulted "
            f"({outcome.fault_type or 'fault'}): "
            f"{outcome.error_message or '<no fault text reported>'}"
        )
    if outcome.timed_out:
        answer = _answer(
            "timeout",
            host,
            info,
            before=before,
            task=outcome.task,
            guidance=(
                f"MEHO stopped waiting for the renew task {outcome.task}; it may still run. "
                "Read the certificate again later (vmware.host.certificate.show)."
            ),
        )
        answer["issues"] = [_task_timeout_issue("CertMgrRefreshCertificates_Task", outcome.task)]
        return answer
    after = await read_certificate(connector, target, operator, info.certificate_manager)
    if _changed(before, after):
        return _answer("renewed", host, info, before=before, after=after, task=outcome.task)
    return _answer(
        "not_verified",
        host,
        info,
        before=before,
        after=after,
        task=outcome.task,
        guidance=(
            "The task ended, but the certificate reads back with the same dates and issuer. "
            "Read it again; check that vCenter manages host certificates with VMCA."
        ),
    )


# ---------------------------------------------------------------------------
# host.certificate.csr
# ---------------------------------------------------------------------------


async def host_certificate_csr_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Make a certificate signing request on the host; the key stays on the host."""
    host = params["host"]
    use_ip = bool(params.get("use_ip_address_as_common_name", False))
    info, refusal = await _connected_host(connector, target, operator, host)
    if refusal is not None:
        return refusal
    assert info is not None and info.certificate_manager is not None  # narrowed by refusal
    manager = info.certificate_manager
    before = await read_certificate(connector, target, operator, manager)
    gate, payload = await _send(
        connector,
        target,
        operator,
        op_id=OP_GENERATE_CSR,
        vmomi_path=f"/HostCertificateManager/{manager}/GenerateCertificateSigningRequest",
        body={"useIpAddressAsCommonName": use_ip},
        params={"host": host, "use_ip_address_as_common_name": use_ip},
        label="host.certificate.csr",
    )
    if gate is not None:
        return gate
    csr = unwrap_vim_value(_unwrap_value(payload))
    if not isinstance(csr, str) or "CERTIFICATE REQUEST" not in csr:
        raise RuntimeError(
            "host.certificate.csr: GenerateCertificateSigningRequest returned no PEM "
            f"signing request ({type(csr).__name__})"
        )
    after = await read_certificate(connector, target, operator, manager)
    return _answer(
        "generated",
        host,
        info,
        before=before,
        after=after,
        csr_pem=csr,
        common_name="ip_address" if use_ip else "fqdn",
        guidance=(
            "Give csr_pem to your certificate authority. Then install the signed certificate "
            "with vmware.composite.host.certificate.install. Do not make another request "
            "before that: a new request replaces the waiting key on the host."
        ),
    )


def wanted_certificate(server: x509.Certificate) -> dict[str, Any]:
    """The subject, issuer and dates of a certificate to install, for answers and previews."""
    return {
        "subject": server.subject.rfc4514_string(),
        "issuer": server.issuer.rfc4514_string(),
        "valid_from": server.not_valid_before_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valid_to": server.not_valid_after_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------------------
# Approval previews
# ---------------------------------------------------------------------------

_WHAT_HAPPENS: Final[dict[str, str]] = {
    OP_ID_HOST_CERTIFICATE_RENEW: (
        "vCenter gets a signing request from the host, signs it with its own certificate "
        "authority (VMCA) and installs the new certificate. Host services restart their "
        "TLS connections."
    ),
    OP_ID_HOST_CERTIFICATE_CSR: (
        "The host makes a new private key and a signing request. The key stays on the "
        "host; the current certificate stays in use until an install. A new request "
        "replaces a key that waits for its certificate."
    ),
    OP_ID_HOST_CERTIFICATE_INSTALL: (
        "The CA chain is added to the CA certificates the host trusts (none is removed). "
        "Then the host uses the new certificate. vCenter must trust its CA, or vCenter "
        "can lose the connection to the host."
    ),
}


def _install_wanted(params: dict[str, Any]) -> dict[str, Any] | None:
    pem = params.get("certificate_pem")
    if not isinstance(pem, str):
        return None
    try:
        return wanted_certificate(x509.load_pem_x509_certificate(pem.encode()))
    except ValueError:
        return None


async def _certificate_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The host and its certificate now, and what the change does. A failed read never blocks."""
    host = ctx.params.get("host")
    if not isinstance(host, str) or ctx.connector_instance is None:
        return None
    op_id = getattr(ctx.descriptor, "op_id", None)
    preview: dict[str, Any] = {"host": host}
    connector: Any = ctx.connector_instance
    try:
        info = await read_host(connector, ctx.target, ctx.operator, host)
        if info is None:
            preview["unavailable"] = ["the host: vCenter has no host with this id"]
        else:
            preview.update({"host_name": info.name, "connection_state": info.connection_state})
            if info.certificate_manager is not None:
                preview["certificate_now"] = await read_certificate(
                    connector, ctx.target, ctx.operator, info.certificate_manager
                )
    except Exception as exc:
        preview["unavailable"] = [f"the host certificate: {preview_failure_reason(exc)}"]
    if op_id == OP_ID_HOST_CERTIFICATE_INSTALL:
        preview["certificate_wanted"] = _install_wanted(ctx.params)
        preview["ca_certificates_given"] = len(ctx.params.get("ca_certificates_pem") or [])
        problem = version_problem(
            ctx.target,
            op_id=op_id,
            minimum="8.0.3",
            feature="telling the host services to use the new certificate (NotifyAffectedServices)",
        )
        if problem is not None:
            preview["version_problem"] = problem
    if op_id == OP_ID_HOST_CERTIFICATE_CSR:
        preview["use_ip_address_as_common_name"] = bool(
            ctx.params.get("use_ip_address_as_common_name", False)
        )
    if isinstance(op_id, str) and op_id in _WHAT_HAPPENS:
        preview["what_happens_next"] = _WHAT_HAPPENS[op_id]
    preview["checked_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return preview


register_preview_builder(OP_ID_HOST_CERTIFICATE_RENEW, _certificate_preview)
register_preview_builder(OP_ID_HOST_CERTIFICATE_CSR, _certificate_preview)
register_preview_builder(OP_ID_HOST_CERTIFICATE_INSTALL, _certificate_preview)
