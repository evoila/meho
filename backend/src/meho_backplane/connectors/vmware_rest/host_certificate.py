# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared parts of the host certificate and TPM operations.

Five operations use them: the two typed reads ``vmware.host.certificate.show``
and ``vmware.host.tpm.attestation``
(:mod:`~meho_backplane.connectors.vmware_rest.typed_ops_host_certificate`) and
the three changes ``vmware.composite.host.certificate.{renew,csr,install}``
(:mod:`~meho_backplane.connectors.vmware_rest.composites._host_certificate`).
All five sit in one operation group, :data:`HOST_CERTIFICATES_GROUP_KEY`.

vCenter has no REST path for these, so every call is a VIM method or property
read, sent as VI-JSON. The host certificate is the ``certificateInfo``
property of the host's ``HostCertificateManager``
(``HostSystem.configManager.certificateManager``).

The VIM read helpers of ``composites/_teardown_reads.py`` are imported inside
the functions: the typed reads import this module while ``composites`` may
still be loading.

A private key never passes through MEHO: the host makes its own key when it
makes a signing request, and an install sends only the signed certificate and
public CA certificates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.host_target import (
    HOST_FLAVOR_ESXI,
    classify_host_target,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "CERTIFICATE_SCHEMA",
    "HOST_CERTIFICATES_GROUP_KEY",
    "HOST_CERTIFICATES_WHEN_TO_USE",
    "HOST_CERTIFICATE_MANAGER_MO_TYPE",
    "HOST_SYSTEM_MO_TYPE",
    "HostInfo",
    "map_certificate",
    "needs_vcenter",
    "parse_vim_time",
    "read_certificate",
    "read_host",
]

HOST_CERTIFICATES_GROUP_KEY: Final = "vmware-host-certificates"

#: The group text. It names every read and change of the group.
HOST_CERTIFICATES_WHEN_TO_USE: Final = (
    "Use for the TLS certificate and the TPM chip of ESXi hosts that vCenter manages. "
    "Reads: the host certificate with its subject, issuer, end date and days left "
    "(host.certificate.show); TPM presence, TPM version and the attestation state of "
    "the hosts of a cluster or of one host (host.tpm.attestation). Changes: renew the "
    "host certificate from the vCenter certificate authority (host.certificate.renew); "
    "make a certificate signing request on the host (host.certificate.csr); install a "
    "certificate signed by your certificate authority, and add its CA chain to the host "
    "(host.certificate.install)."
)

HOST_SYSTEM_MO_TYPE: Final = "HostSystem"
HOST_CERTIFICATE_MANAGER_MO_TYPE: Final = "HostCertificateManager"

_HOST_PROPS: Final = ["name", "runtime.connectionState", "configManager.certificateManager"]
_CERTIFICATE_INFO: Final = "certificateInfo"
#: ``certificateInfo.kind`` is unset for the machine certificate.
_MACHINE_KIND: Final = "Machine"

_NULL_TEXT: Final = ["string", "null"]

#: The certificate fields every answer of the group uses.
CERTIFICATE_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The host certificate as vCenter reads it; null when it was not read.",
    "properties": {
        "kind": {
            "type": "string",
            "description": "Machine (the host's TLS certificate) or VASAClient.",
        },
        "subject": {"type": _NULL_TEXT, "description": "Subject of the certificate."},
        "issuer": {"type": _NULL_TEXT, "description": "Issuer (the certificate authority)."},
        "valid_from": {
            "type": _NULL_TEXT,
            "description": "Start of validity, UTC, ISO 8601.",
        },
        "valid_to": {"type": _NULL_TEXT, "description": "End of validity, UTC, ISO 8601."},
        "days_left": {
            "type": ["integer", "null"],
            "description": "Whole days until valid_to; negative when it has expired.",
        },
        "vcenter_status": {
            "type": _NULL_TEXT,
            "description": (
                "vCenter's view: good, expiring, expiringShortly, expirationImminent, "
                "expired or unknown."
            ),
        },
    },
}


@dataclass(frozen=True)
class HostInfo:
    """One read of a host: name, connection state and its certificate manager."""

    host: str
    name: str | None
    connection_state: str | None
    certificate_manager: str | None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def needs_vcenter(target: Any) -> str | None:
    """Why the target cannot run a host certificate operation, or ``None``."""
    flavor, refusal = classify_host_target(target)
    if refusal is not None:
        return str(refusal.get("guidance") or "the target is not a vCenter")
    if flavor == HOST_FLAVOR_ESXI:
        return (
            "the target is a single ESXi host. Host certificate and TPM operations run on "
            "the vCenter that manages the host; call again with the vCenter target. "
            "Nothing was sent."
        )
    return None


def parse_vim_time(value: Any) -> datetime | None:
    """A VI-JSON ``dateTime`` as an aware UTC datetime, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value is not None else None


def map_certificate(info: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The answer fields of a ``HostCertificateManagerCertificateInfo``."""
    if not isinstance(info, dict):
        return None
    valid_from = parse_vim_time(info.get("notBefore"))
    valid_to = parse_vim_time(info.get("notAfter"))
    days_left = None
    if valid_to is not None:
        days_left = (valid_to - (now or datetime.now(UTC))).days
    return {
        "kind": _text(info.get("kind")) or _MACHINE_KIND,
        "subject": _text(info.get("subject")),
        "issuer": _text(info.get("issuer")),
        "valid_from": _iso(valid_from),
        "valid_to": _iso(valid_to),
        "days_left": days_left,
        "vcenter_status": _text(info.get("status")),
    }


async def read_host(
    connector: VmwareRestConnector, target: Any, operator: Operator, host: str
) -> HostInfo | None:
    """Read the host's name, connection state and certificate manager; ``None`` when absent."""
    from meho_backplane.connectors.vmware_rest.composites._teardown_reads import read_object

    props = await read_object(
        connector, target, operator, mo_type=HOST_SYSTEM_MO_TYPE, moid=host, props=_HOST_PROPS
    )
    if props is None:
        return None
    manager = props.get("configManager.certificateManager")
    return HostInfo(
        host=host,
        name=_text(props.get("name")),
        connection_state=_text(props.get("runtime.connectionState")),
        certificate_manager=_text(manager.get("value")) if isinstance(manager, dict) else None,
    )


async def read_certificate(
    connector: VmwareRestConnector, target: Any, operator: Operator, manager: str
) -> dict[str, Any] | None:
    """Read and map ``HostCertificateManager.certificateInfo``; ``None`` when it is unset.

    A fault, or a property vCenter could not read (missing permission
    ``Certificate.Manage``), raises.
    """
    from meho_backplane.connectors.vmware_rest.composites._teardown_reads import read_object

    props = await read_object(
        connector,
        target,
        operator,
        mo_type=HOST_CERTIFICATE_MANAGER_MO_TYPE,
        moid=manager,
        props=[_CERTIFICATE_INFO],
    )
    if props is None:
        raise RuntimeError(f"vCenter has no HostCertificateManager {manager!r}")
    return map_certificate(props.get(_CERTIFICATE_INFO))
