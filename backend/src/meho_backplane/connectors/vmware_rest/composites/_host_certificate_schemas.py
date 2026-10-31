# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and AI guidance of the three host certificate changes.

``vmware.composite.host.certificate.{renew,csr,install}``; the handlers are in
:mod:`._host_certificate` and :mod:`._host_certificate_install`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites.schemas import HOST_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.host_certificate import CERTIFICATE_SCHEMA

__all__ = [
    "CERTIFICATE_CSR_LLM_INSTRUCTIONS",
    "CERTIFICATE_CSR_PARAMETER_SCHEMA",
    "CERTIFICATE_CSR_RESPONSE_SCHEMA",
    "CERTIFICATE_INSTALL_LLM_INSTRUCTIONS",
    "CERTIFICATE_INSTALL_PARAMETER_SCHEMA",
    "CERTIFICATE_INSTALL_RESPONSE_SCHEMA",
    "CERTIFICATE_RENEW_LLM_INSTRUCTIONS",
    "CERTIFICATE_RENEW_PARAMETER_SCHEMA",
    "CERTIFICATE_RENEW_RESPONSE_SCHEMA",
]

_NULL_TEXT: Final = ["string", "null"]

_HOST_FIELD: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": HOST_MOID_PATTERN,
    "description": "Host id (moid), e.g. host-1001. Find it with GET:/api/vcenter/host.",
}

#: A public PEM certificate. Text with ``PRIVATE KEY`` is refused before the
#: call waits for approval, so a key pasted by mistake is never stored.
_PEM_CERTIFICATE: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 60,
    "maxLength": 16384,
    "pattern": r"^\s*-----BEGIN CERTIFICATE-----",
    "not": {
        "pattern": "PRIVATE KEY",
        "description": "Never a private key: the host keeps its own key.",
    },
}

_EXAMPLE_PEM: Final = "-----BEGIN CERTIFICATE-----\n<signed-certificate>\n-----END CERTIFICATE-----"
_EXAMPLE_CA_PEM: Final = (
    "-----BEGIN CERTIFICATE-----\n<issuing-ca-certificate>\n-----END CERTIFICATE-----"
)

_ISSUES_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": "Present when the change is not confirmed (a timeout).",
    "items": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Where the problem is."},
            "severity": {"type": "string", "description": "error, warning or info."},
            "message": {"type": "string", "description": "What happened."},
        },
    },
}

_COMMON_STATUS_TEXT: Final = (
    " not_found: no host with this id. not_connected: the host is not connected to "
    "vCenter. needs_vcenter: the target is a single ESXi host or not a vCenter. Nothing "
    "is sent on these three."
)


def _response(statuses: list[str], status_text: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": statuses, "description": status_text},
            "host": {"type": "string", "description": "The host id the call named."},
            "host_name": {"type": _NULL_TEXT, "description": "The host's name in vCenter."},
            "connection_state": {
                "type": _NULL_TEXT,
                "description": "connected, disconnected or notResponding.",
            },
            "certificate_before": {
                **CERTIFICATE_SCHEMA,
                "description": "The host certificate before the change.",
            },
            "certificate_after": {
                **CERTIFICATE_SCHEMA,
                "description": "The host certificate read back after the change.",
            },
            **extra,
            "task": {
                "type": _NULL_TEXT,
                "description": "The vCenter task id (renew only); null otherwise.",
            },
            "guidance": {"type": _NULL_TEXT, "description": "What to do next, in plain words."},
        },
        "required": ["status", "host"],
    }


# ===========================================================================
# host.certificate.renew
# ===========================================================================

CERTIFICATE_RENEW_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"host": _HOST_FIELD},
    "required": ["host"],
    "additionalProperties": False,
    "examples": [{"host": "host-1001"}],
}

CERTIFICATE_RENEW_RESPONSE_SCHEMA: dict[str, Any] = _response(
    ["renewed", "not_verified", "timeout", "not_found", "not_connected", "needs_vcenter"],
    "renewed: the host has a new certificate from VMCA. not_verified: the task ended, but "
    "the certificate reads back unchanged. timeout: MEHO stopped waiting; the task may still "
    "run." + _COMMON_STATUS_TEXT,
    issues=_ISSUES_SCHEMA,
)

CERTIFICATE_RENEW_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to renew a host certificate that is expiring or expired, when vCenter manages "
        "host certificates with its own certificate authority (VMCA, the default). A second "
        "person must approve it."
    ),
    "preconditions": (
        "The host is connected to vCenter. vCenter is in VMCA mode for host certificates; "
        "for a certificate from your own certificate authority, use host.certificate.csr and "
        "host.certificate.install instead. Works only on a vCenter target."
    ),
    "parameter_hints": {"host": "Host id (moid), e.g. 'host-21'."},
    "output_shape": (
        "{status: renewed|not_verified|timeout|not_found|not_connected|needs_vcenter, host, "
        "host_name, connection_state, certificate_before, certificate_after: {kind, subject, "
        "issuer, valid_from, valid_to, days_left, vcenter_status}, task, guidance, issues}."
    ),
    "next_step": (
        "On renewed, check certificate_after.valid_to. A vendor fault fails the call with the "
        "fault type and text. On timeout, read the certificate later with "
        "vmware.host.certificate.show."
    ),
}

# ===========================================================================
# host.certificate.csr
# ===========================================================================

CERTIFICATE_CSR_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": _HOST_FIELD,
        "use_ip_address_as_common_name": {
            "type": "boolean",
            "default": False,
            "description": (
                "True: the common name (CN) of the request is the host's management IP "
                "address. False (default): it is the host's FQDN."
            ),
        },
    },
    "required": ["host"],
    "additionalProperties": False,
    "examples": [{"host": "host-1001"}],
}

CERTIFICATE_CSR_RESPONSE_SCHEMA: dict[str, Any] = _response(
    ["generated", "not_found", "not_connected", "needs_vcenter"],
    "generated: the host made a new key and a signing request (csr_pem)." + _COMMON_STATUS_TEXT,
    csr_pem={
        "type": "string",
        "description": "The signing request, PEM. Public data; the key stays on the host.",
    },
    common_name={
        "type": "string",
        "enum": ["fqdn", "ip_address"],
        "description": "What the request uses as its common name.",
    },
)

CERTIFICATE_CSR_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call as the first step to give a host a certificate from your own certificate "
        "authority: the host makes a new private key and a signing request (CSR). A second "
        "person must approve it."
    ),
    "preconditions": (
        "The host is connected to vCenter. The current certificate stays in use. A new "
        "request replaces a key that still waits for its signed certificate, so do not call "
        "it twice before the install."
    ),
    "parameter_hints": {
        "use_ip_address_as_common_name": "Leave false unless your CA needs the IP address.",
    },
    "output_shape": (
        "{status: generated|not_found|not_connected|needs_vcenter, host, host_name, "
        "connection_state, csr_pem, common_name, certificate_before, certificate_after, "
        "guidance}. certificate_after is the certificate still in use."
    ),
    "next_step": (
        "Give csr_pem to your certificate authority. Then call "
        "vmware.composite.host.certificate.install with the signed certificate and the CA "
        "chain. A vendor fault fails the call with the fault type and text."
    ),
}

# ===========================================================================
# host.certificate.install
# ===========================================================================

CERTIFICATE_INSTALL_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": _HOST_FIELD,
        "certificate_pem": {
            **_PEM_CERTIFICATE,
            "description": (
                "The signed host certificate, PEM (-----BEGIN CERTIFICATE-----). It must "
                "match the key of the host's last signing request. Never a private key."
            ),
        },
        "ca_certificates_pem": {
            "type": "array",
            "maxItems": 10,
            "description": (
                "The CA chain of the certificate, one PEM certificate per item. They are "
                "added to the CA certificates the host trusts; none is removed."
            ),
            "items": {
                **_PEM_CERTIFICATE,
                "description": "One CA certificate, PEM. Never a private key.",
            },
        },
    },
    "required": ["host", "certificate_pem"],
    "additionalProperties": False,
    "examples": [
        {
            "host": "host-1001",
            "certificate_pem": _EXAMPLE_PEM,
            "ca_certificates_pem": [_EXAMPLE_CA_PEM],
        }
    ],
}

_WANTED_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The certificate the call sends, as MEHO reads it from certificate_pem.",
    "properties": {
        "subject": {"type": "string", "description": "Subject of the certificate."},
        "issuer": {"type": "string", "description": "Issuer of the certificate."},
        "valid_from": {"type": "string", "description": "Start of validity, UTC."},
        "valid_to": {"type": "string", "description": "End of validity, UTC."},
    },
}

CERTIFICATE_INSTALL_RESPONSE_SCHEMA: dict[str, Any] = _response(
    [
        "installed",
        "unchanged",
        "not_verified",
        "invalid_certificate",
        "not_found",
        "not_connected",
        "needs_vcenter",
    ],
    "installed: the host uses the new certificate. unchanged: it already had a certificate "
    "with these dates; nothing sent. not_verified: the host took it, but it does not read "
    "back yet. invalid_certificate: an input is not a PEM certificate; nothing sent."
    + _COMMON_STATUS_TEXT,
    certificate_wanted=_WANTED_SCHEMA,
    ca_certificates_added={
        "type": "integer",
        "description": "How many CA certificates were new to the host and were added.",
    },
    services_notified={
        "type": "boolean",
        "description": (
            "True when the host services were told to use the new certificate now. False "
            "on vCenter 8.0 U1 and U2 (no such call); guidance then says what to restart."
        ),
    },
)

CERTIFICATE_INSTALL_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call as the second step, after host.certificate.csr: install the certificate your "
        "certificate authority signed, and add its CA chain to the host. A second person "
        "must approve it."
    ),
    "preconditions": (
        "The certificate was signed for the host's last signing request "
        "(host.certificate.csr); else the host refuses it. vCenter trusts the same CA and "
        "is set to custom certificate mode, or vCenter can lose the connection to the host. "
        "The host is connected. Never pass a private key."
    ),
    "parameter_hints": {
        "certificate_pem": "The signed certificate only, not the chain and not a key.",
        "ca_certificates_pem": "The issuing CA, then the root CA. Omit when the host has them.",
    },
    "output_shape": (
        "{status: installed|unchanged|not_verified|invalid_certificate|not_found|"
        "not_connected|needs_vcenter, host, host_name, connection_state, certificate_wanted, "
        "ca_certificates_added, services_notified, certificate_before, certificate_after, "
        "guidance}."
    ),
    "next_step": (
        "On installed, check that the host stays connected in vCenter "
        "(vmware.host.certificate.show). When services_notified is false (vCenter 8.0 U1 "
        "or U2), restart the host's management agents as guidance says. A vendor fault "
        "fails the call with the fault type and text, for example when the certificate does "
        "not match the host's key."
    ),
}
