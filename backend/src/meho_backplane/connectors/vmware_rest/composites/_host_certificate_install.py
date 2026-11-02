# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``host.certificate.install`` -- install a CA-signed certificate on a host.

The steps, each through the governed write seam:

1. When ``ca_certificates_pem`` is given: read the CA certificates and CRLs
   the host trusts (``ListCACertificates``,
   ``ListCACertificateRevocationLists``) and send them again with the new CA
   certificates (``ReplaceCACertificatesAndCRLs`` replaces the whole list).
   A CA certificate the host already has is not added twice; none is removed.
2. ``InstallServerCertificate`` with the signed certificate. The host checks
   that it matches the key of its last signing request
   (``host.certificate.csr``).
3. ``NotifyAffectedServices``, so the host's services use the new certificate
   now, without a restart. This method is in the vSphere API from 8.0 U3 on.
   On vCenter 8.0 U1 and U2 it is not sent: the answer says
   ``services_notified: false`` and how to make the services use the new
   certificate.

Then the certificate is read back. MEHO compares its start and end dates with
the certificate it sent. The dates alone do not prove that the host has this
exact certificate (``certificateInfo`` has no fingerprint), so the install
always sends: a certificate the host already has is installed again, and the
CA chain is still added. The certificate and the CA chain are public data; the
input schema refuses any text with ``PRIVATE KEY``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx
from cryptography import x509

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest._version_gate import version_problem
from meho_backplane.connectors.vmware_rest.composites._host_certificate import (
    OP_ID_HOST_CERTIFICATE_INSTALL,
    OP_INSTALL_SERVER_CERTIFICATE,
    OP_NOTIFY_AFFECTED_SERVICES,
    OP_REPLACE_CA_CERTIFICATES,
    _answer,
    _connected_host,
    _send,
    _vendor_fault,
    wanted_certificate,
)
from meho_backplane.connectors.vmware_rest.composites._write import _unwrap_value
from meho_backplane.connectors.vmware_rest.host_certificate import (
    parse_vim_time,
    read_certificate,
)
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = ["host_certificate_install_composite"]

#: ``NotifyAffectedServices`` is in the vSphere API from 8.0 U3 on (the preview
#: in ``_host_certificate.py`` uses the same version).
_NOTIFY_MINIMUM: Final = "8.0.3"
_RESTART_HINT: Final = (
    "The host services use the new certificate after a restart of the management agents "
    "(hostd and vpxa) or a reboot of the host."
)


def _pem_key(pem: str) -> str:
    """A PEM block without white space, to compare two copies of one certificate."""
    return "".join(pem.split())


def _parse_certificates(params: dict[str, Any]) -> tuple[x509.Certificate | None, str | None]:
    """The parsed server certificate, or ``(None, reason)`` when an input is not a certificate."""
    try:
        server = x509.load_pem_x509_certificate(params["certificate_pem"].encode())
    except ValueError as exc:
        return None, f"certificate_pem is not a PEM certificate: {exc}"
    for index, pem in enumerate(params.get("ca_certificates_pem") or []):
        try:
            x509.load_pem_x509_certificate(pem.encode())
        except ValueError as exc:
            return None, f"ca_certificates_pem[{index}] is not a PEM certificate: {exc}"
    return server, None


def _same_dates(certificate: dict[str, Any] | None, server: x509.Certificate) -> bool:
    """Whether the host certificate has the validity dates of *server*."""
    if certificate is None:
        return False
    valid_from = parse_vim_time(certificate.get("valid_from"))
    valid_to = parse_vim_time(certificate.get("valid_to"))
    return valid_from == server.not_valid_before_utc and valid_to == server.not_valid_after_utc


async def _pem_list(
    connector: VmwareRestConnector, target: Any, operator: Operator, path: str
) -> list[str]:
    payload = await connector._post_vmomi_json(target, path, operator=operator, json={})
    items = unwrap_vim_value(_unwrap_value(payload))
    return [item for item in items if isinstance(item, str)] if isinstance(items, list) else []


async def _add_ca_chain(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    host: str,
    manager: str,
    chain: list[str],
) -> tuple[OperationResult | None, int]:
    """Add *chain* to the host's trusted CA certificates; ``(gate, number added)``.

    ``ReplaceCACertificatesAndCRLs`` replaces the whole list, so the current
    CA certificates and CRLs are read first and sent again with the new ones.
    """
    base = f"/HostCertificateManager/{manager}"
    try:
        current = await _pem_list(connector, target, operator, f"{base}/ListCACertificates")
        crls = await _pem_list(
            connector, target, operator, f"{base}/ListCACertificateRevocationLists"
        )
    except httpx.HTTPStatusError as exc:
        raise _vendor_fault("host.certificate.install", "ListCACertificates", exc) from exc
    known = {_pem_key(pem) for pem in current}
    new = []
    for pem in chain:
        if _pem_key(pem) not in known:
            known.add(_pem_key(pem))
            new.append(pem)
    if not new:
        return None, 0
    gate, _payload = await _send(
        connector,
        target,
        operator,
        op_id=OP_REPLACE_CA_CERTIFICATES,
        vmomi_path=f"{base}/ReplaceCACertificatesAndCRLs",
        body={"caCert": [*current, *new], "caCrl": crls},
        params={"host": host, "ca_certificates_added": len(new)},
        label="host.certificate.install",
    )
    return gate, len(new)


async def _install_and_notify(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    manager: str,
    params: dict[str, Any],
    certificate_pem: str,
) -> tuple[OperationResult | None, str | None]:
    """Install the certificate, then tell the host services; ``(gate, services note)``.

    The note is ``None`` when the services were told. ``NotifyAffectedServices``
    is in the vSphere API from 8.0 U3 on: on an older vCenter it is not sent,
    and the note says to restart the host's management agents. The
    certificate is installed at that point, so a failed notify is a note, not
    an error.
    """
    base = f"/HostCertificateManager/{manager}"
    gate, _payload = await _send(
        connector,
        target,
        operator,
        op_id=OP_INSTALL_SERVER_CERTIFICATE,
        vmomi_path=f"{base}/InstallServerCertificate",
        body={"cert": certificate_pem},
        params=params,
        label="host.certificate.install",
    )
    if gate is not None:
        return gate, None
    problem = version_problem(
        target,
        op_id=OP_ID_HOST_CERTIFICATE_INSTALL,
        minimum=_NOTIFY_MINIMUM,
        feature="telling the host services to use the new certificate (NotifyAffectedServices)",
    )
    if problem is not None:
        return None, f"{problem} {_RESTART_HINT}"
    try:
        gate, _payload = await _send(
            connector,
            target,
            operator,
            op_id=OP_NOTIFY_AFFECTED_SERVICES,
            vmomi_path=f"{base}/NotifyAffectedServices",
            body={},
            params=params,
            label="host.certificate.install",
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        return None, f"NotifyAffectedServices failed: {exc}. {_RESTART_HINT}"
    return gate, None


async def host_certificate_install_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Install a CA-signed certificate on the host, after adding its CA chain."""
    host = params["host"]
    server, problem = _parse_certificates(params)
    if server is None:
        return _answer(
            "invalid_certificate",
            host,
            None,
            guidance=f"{problem}. Nothing was sent.",
        )
    wanted = wanted_certificate(server)
    info, refusal = await _connected_host(connector, target, operator, host)
    if refusal is not None:
        return {**refusal, "certificate_wanted": wanted}
    assert info is not None and info.certificate_manager is not None  # narrowed by refusal
    manager = info.certificate_manager
    before = await read_certificate(connector, target, operator, manager)
    gate, added = await _add_ca_chain(
        connector,
        target,
        operator,
        host=host,
        manager=manager,
        chain=list(params.get("ca_certificates_pem") or []),
    )
    if gate is not None:
        return gate
    gate, services_note = await _install_and_notify(
        connector,
        target,
        operator,
        manager=manager,
        params={"host": host, "certificate_valid_to": wanted["valid_to"]},
        certificate_pem=params["certificate_pem"],
    )
    if gate is not None:
        return gate
    after = await read_certificate(connector, target, operator, manager)
    if _same_dates(after, server):
        return _answer(
            "installed",
            host,
            info,
            before=before,
            after=after,
            certificate_wanted=wanted,
            ca_certificates_added=added,
            services_notified=services_note is None,
            guidance=services_note,
        )
    return _answer(
        "not_verified",
        host,
        info,
        before=before,
        after=after,
        certificate_wanted=wanted,
        ca_certificates_added=added,
        services_notified=services_note is None,
        guidance=(
            "The host took the certificate, but it does not read back with its dates yet. "
            "Read it again with vmware.host.certificate.show. " + (services_note or "")
        ).strip(),
    )
