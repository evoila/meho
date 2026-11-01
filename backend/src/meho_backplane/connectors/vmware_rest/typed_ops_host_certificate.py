# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two typed reads: the host certificate and the TPM attestation of hosts.

* ``vmware.host.certificate.show`` reads ``HostCertificateManager.certificateInfo``
  of one host: subject, issuer, validity, days left and vCenter's status.
* ``vmware.host.tpm.attestation`` reads, for the hosts of a cluster or one
  host, ``capability.tpmSupported`` / ``tpmVersion`` / ``txtEnabled`` and
  ``summary.tpmAttestation``. With ``include_report`` it also calls
  ``HostSystem.QueryTpmAttestationReport`` per host and gives a short summary
  of the report.

Both read on the connector session (VI-JSON), so they work with no spec
ingest. The read helpers of ``composites/_teardown_reads.py`` are imported
inside the functions, because ``typed_ops`` imports this module while
``composites`` may still be loading. Both need a vCenter target. The shared parts are in
:mod:`~meho_backplane.connectors.vmware_rest.host_certificate`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_MOID_PATTERN,
    HOST_MOID_PATTERN,
)
from meho_backplane.connectors.vmware_rest.host_certificate import (
    CERTIFICATE_SCHEMA,
    HOST_CERTIFICATES_GROUP_KEY,
    HOST_CERTIFICATES_WHEN_TO_USE,
    HOST_SYSTEM_MO_TYPE,
    needs_vcenter,
    parse_vim_time,
    read_certificate,
    read_host,
)
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.vim_body import fault_message, unwrap_vim_value

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "HOST_CERTIFICATES_GROUP_KEY",
    "HOST_CERTIFICATES_WHEN_TO_USE",
    "TYPED_OPS",
    "VMWARE_HOST_CERTIFICATE_SHOW_OP",
    "VMWARE_HOST_TPM_ATTESTATION_OP",
    "host_certificate_show_impl",
    "host_tpm_attestation_impl",
]

_log = structlog.get_logger(__name__)

_READ_ERRORS = (httpx.HTTPError, RuntimeError)

_CLUSTER_MO_TYPE: Final = "ClusterComputeResource"
_TPM_PROPS: Final = [
    "name",
    "runtime.connectionState",
    "capability.tpmSupported",
    "capability.tpmVersion",
    "capability.txtEnabled",
    "summary.tpmAttestation",
]
_QUERY_TPM_REPORT_PATH: Final = "/HostSystem/{moid}/QueryTpmAttestationReport"

#: The vendor calls of the two reads (``METHOD:/path`` of the pinned vi-json.yaml).
OP_RETRIEVE_PROPERTIES: Final = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
OP_CONTINUE_RETRIEVE_PROPERTIES: Final = (
    "POST:/PropertyCollector/{moId}/ContinueRetrievePropertiesEx"
)
OP_QUERY_TPM_ATTESTATION_REPORT: Final = "POST:/HostSystem/{moId}/QueryTpmAttestationReport"
#: Read-only vendor calls of each typed read, for the reconcile test.
READ_VENDOR_CALLS: Final[dict[str, tuple[str, ...]]] = {
    "vmware.host.certificate.show": (OP_RETRIEVE_PROPERTIES,),
    "vmware.host.tpm.attestation": (
        OP_RETRIEVE_PROPERTIES,
        OP_CONTINUE_RETRIEVE_PROPERTIES,
        OP_QUERY_TPM_ATTESTATION_REPORT,
    ),
}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _note(what: str, exc: Exception) -> str:
    return f"{what}: read failed with {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# vmware.host.certificate.show
# ---------------------------------------------------------------------------


async def host_certificate_show_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.host.certificate.show``."""
    host = params["host"]
    empty: dict[str, Any] = {
        "host": host,
        "host_name": None,
        "connection_state": None,
        "certificate": None,
        "read_note": None,
        "guidance": None,
    }
    refusal = needs_vcenter(target)
    if refusal is not None:
        return {**empty, "status": "needs_vcenter", "guidance": refusal}
    info = await read_host(connector, target, operator, host)
    if info is None:
        return {
            **empty,
            "status": "not_found",
            "guidance": f"vCenter has no host {host!r}. Check the host id.",
        }
    answer = {
        **empty,
        "host_name": info.name,
        "connection_state": info.connection_state,
    }
    if info.certificate_manager is None:
        return {
            **answer,
            "status": "unreadable",
            "read_note": "the host has no configManager.certificateManager",
            "guidance": "vCenter shows no certificate manager. Is the host connected?",
        }
    try:
        certificate = await read_certificate(connector, target, operator, info.certificate_manager)
    except _READ_ERRORS as exc:
        return {
            **answer,
            "status": "unreadable",
            "read_note": _note("certificateInfo", exc),
            "guidance": (
                "The certificate could not be read. Reading it needs the privilege "
                "Certificate.Manage, and the host must be connected."
            ),
        }
    _log.info("vmware_host_certificate_read", target=target.name, host=host)
    return {**answer, "status": "ok", "certificate": certificate}


# ---------------------------------------------------------------------------
# vmware.host.tpm.attestation
# ---------------------------------------------------------------------------


def _tpm_row(host: str, props: dict[str, Any] | None) -> dict[str, Any]:
    """One flat row of the TPM answer."""
    props = props or {}
    attestation = props.get("summary.tpmAttestation")
    attestation = attestation if isinstance(attestation, dict) else {}
    message = attestation.get("message")
    time = parse_vim_time(attestation.get("time"))
    return {
        "host": host,
        "name": _text(props.get("name")),
        "connection_state": _text(props.get("runtime.connectionState")),
        "tpm_supported": _bool(props.get("capability.tpmSupported")),
        "tpm_version": _text(props.get("capability.tpmVersion")),
        "txt_enabled": _bool(props.get("capability.txtEnabled")),
        "attestation_status": _text(attestation.get("status")),
        "attestation_time": time.strftime("%Y-%m-%dT%H:%M:%SZ") if time else None,
        "attestation_message": (
            _text(message.get("message")) if isinstance(message, dict) else None
        ),
        "report_log_reliable": None,
        "report_pcr_count": None,
        "report_event_count": None,
        "read_note": None if props else "vCenter did not return this host",
    }


async def _add_report(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    row: dict[str, Any],
) -> None:
    """Fill the report fields of *row* from ``QueryTpmAttestationReport``."""
    path = _QUERY_TPM_REPORT_PATH.format(moid=row["host"])
    try:
        payload = await connector._post_vmomi_json(target, path, operator=operator, json={})
    except httpx.HTTPStatusError as exc:
        text = None
        try:
            body = exc.response.json()
            text = fault_message(body) if isinstance(body, dict) else None
        except ValueError:
            text = None
        row["read_note"] = f"QueryTpmAttestationReport failed: {text or exc}"
        return
    except _READ_ERRORS as exc:
        row["read_note"] = _note("QueryTpmAttestationReport", exc)
        return
    report = unwrap_vim_value(_unwrap_value(payload))
    if not isinstance(report, dict) or not report:
        row["read_note"] = "the host gave no TPM attestation report (no TPM, or TPM 1.2)"
        return
    pcrs = report.get("tpmPcrValues")
    events = report.get("tpmEvents")
    row["report_log_reliable"] = _bool(report.get("tpmLogReliable"))
    row["report_pcr_count"] = len(pcrs) if isinstance(pcrs, list) else 0
    row["report_event_count"] = len(events) if isinstance(events, list) else 0


async def _read_tpm_rows(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]] | None:
    """``(rows, notes)`` for the cluster's hosts or the one host; ``None`` when it is absent."""
    from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
        moref_values,
        read_many,
        read_object,
    )

    if params.get("host"):
        host = str(params["host"])
        props = await read_object(
            connector, target, operator, mo_type=HOST_SYSTEM_MO_TYPE, moid=host, props=_TPM_PROPS
        )
        return None if props is None else ([_tpm_row(host, props)], [])
    cluster = await read_object(
        connector,
        target,
        operator,
        mo_type=_CLUSTER_MO_TYPE,
        moid=str(params["cluster"]),
        props=["host"],
    )
    if cluster is None:
        return None
    hosts = [moid for _type, moid in moref_values(cluster.get("host"))]
    by_host, complete = await read_many(
        connector,
        target,
        operator,
        mo_type=HOST_SYSTEM_MO_TYPE,
        moids=hosts,
        props=_TPM_PROPS,
        all_pages=True,
    )
    notes = [] if complete else ["vCenter did not answer every field of every host"]
    return [_tpm_row(host, by_host.get(host)) for host in hosts], notes


async def host_tpm_attestation_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.host.tpm.attestation``."""
    scope = {"cluster": params.get("cluster"), "host": params.get("host")}
    empty: dict[str, Any] = {**scope, "hosts": [], "host_count": 0, "read_notes": []}
    refusal = needs_vcenter(target)
    if refusal is not None:
        return {**empty, "status": "needs_vcenter", "guidance": refusal}
    found = await _read_tpm_rows(connector, operator, target, params)
    if found is None:
        what = (
            f"host {params['host']!r}" if params.get("host") else f"cluster {params['cluster']!r}"
        )
        return {**empty, "status": "not_found", "guidance": f"vCenter has no {what}. Check the id."}
    rows, notes = found
    if params.get("include_report"):
        for row in rows:
            if row["read_note"] is None and row["connection_state"] == "connected":
                await _add_report(connector, operator, target, row)
    _log.info(
        "vmware_host_tpm_attestation_read",
        target=target.name,
        host_count=len(rows),
        with_report=bool(params.get("include_report")),
    )
    return {**empty, "status": "ok", "hosts": rows, "host_count": len(rows), "read_notes": notes}


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_NULL_TEXT: Final = ["string", "null"]

_HOST_FIELD: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": HOST_MOID_PATTERN,
    "description": "Host id (moid), e.g. host-1001. Find it with GET:/api/vcenter/host.",
}

_SHOW_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"host": _HOST_FIELD},
    "required": ["host"],
    "additionalProperties": False,
    "examples": [{"host": "host-1001"}],
}

_REFUSAL_TEXT: Final = (
    "ok: read. not_found: no host with this id. needs_vcenter: the target is a single "
    "ESXi host or not a vCenter. unreadable: the host was found, but not its certificate "
    "(read_note says why)."
)

_SHOW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["ok", "not_found", "needs_vcenter", "unreadable"],
            "description": _REFUSAL_TEXT,
        },
        "host": {"type": "string", "description": "The host id the call named."},
        "host_name": {"type": _NULL_TEXT, "description": "The host's name in vCenter."},
        "connection_state": {
            "type": _NULL_TEXT,
            "description": "connected, disconnected or notResponding.",
        },
        "certificate": CERTIFICATE_SCHEMA,
        "read_note": {"type": _NULL_TEXT, "description": "Why the certificate was not read."},
        "guidance": {"type": _NULL_TEXT, "description": "What to do next, in plain words."},
    },
    "required": ["status", "host", "certificate"],
}

VMWARE_HOST_CERTIFICATE_SHOW_OP = VmwareTypedOp(
    op_id="vmware.host.certificate.show",
    handler_attr="host_certificate_show",
    summary="The TLS certificate of one ESXi host: subject, issuer, end date and days left.",
    description=(
        "Reads the host certificate that vCenter knows for one host: subject, issuer, "
        "start and end of validity, days left and vCenter's status (good, expiring, "
        "expired). Needs a vCenter target. Read-only."
    ),
    parameter_schema=_SHOW_PARAMETER_SCHEMA,
    response_schema=_SHOW_RESPONSE_SCHEMA,
    group_key=HOST_CERTIFICATES_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "host", "certificate", "vi-json"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to check a host certificate: who issued it, when it ends, how many days "
            "are left, and whether vCenter marks it as expiring. Call it before and after "
            "a renew or an install."
        ),
        "parameter_hints": {"host": "Host id (moid), e.g. 'host-21'."},
        "output_shape": (
            "{status: ok|not_found|needs_vcenter|unreadable, host, host_name, "
            "connection_state, certificate: {kind, subject, issuer, valid_from, valid_to, "
            "days_left, vcenter_status}, read_note, guidance}."
        ),
        "next_step": (
            "When days_left is low or vcenter_status is not good: renew the certificate from "
            "the vCenter certificate authority (vmware.composite.host.certificate.renew), or "
            "make a signing request (vmware.composite.host.certificate.csr) and install the "
            "signed certificate (vmware.composite.host.certificate.install)."
        ),
    },
)

_TPM_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "string",
            "pattern": CLUSTER_MOID_PATTERN,
            "description": "Read every host of this cluster (moid, e.g. domain-c7). Or give host.",
        },
        "host": {**_HOST_FIELD, "description": "Read only this host (moid). Or give cluster."},
        "include_report": {
            "type": "boolean",
            "default": False,
            "description": (
                "Also ask each connected host for its TPM attestation report "
                "(QueryTpmAttestationReport): one more call per host."
            ),
        },
    },
    "oneOf": [{"required": ["cluster"]}, {"required": ["host"]}],
    "additionalProperties": False,
    "examples": [{"cluster": "domain-c7"}],
}

_TPM_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {"type": "string", "description": "Host id (moid)."},
        "name": {"type": _NULL_TEXT, "description": "Host name in vCenter."},
        "connection_state": {
            "type": _NULL_TEXT,
            "description": "connected, disconnected or notResponding.",
        },
        "tpm_supported": {
            "type": ["boolean", "null"],
            "description": "True when the host can measure its boot with a TPM.",
        },
        "tpm_version": {"type": _NULL_TEXT, "description": "TPM version, e.g. 2.0."},
        "txt_enabled": {
            "type": ["boolean", "null"],
            "description": "True when Intel TXT is on.",
        },
        "attestation_status": {
            "type": _NULL_TEXT,
            "description": "accepted, notAccepted, or null when vCenter has no attestation.",
        },
        "attestation_time": {
            "type": _NULL_TEXT,
            "description": "When vCenter last checked the host, UTC, ISO 8601.",
        },
        "attestation_message": {
            "type": _NULL_TEXT,
            "description": "Why the attestation failed, when vCenter says.",
        },
        "report_log_reliable": {
            "type": ["boolean", "null"],
            "description": "From the report: true when the TPM event log is complete.",
        },
        "report_pcr_count": {
            "type": ["integer", "null"],
            "description": "From the report: number of PCR values.",
        },
        "report_event_count": {
            "type": ["integer", "null"],
            "description": "From the report: number of TPM events.",
        },
        "read_note": {"type": _NULL_TEXT, "description": "Why a field of this host is missing."},
    },
    "required": ["host"],
}

_TPM_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["ok", "not_found", "needs_vcenter"],
            "description": (
                "ok: read. not_found: no cluster or host with this id. needs_vcenter: the "
                "target is a single ESXi host or not a vCenter."
            ),
        },
        "cluster": {"type": _NULL_TEXT, "description": "The cluster id the call named."},
        "host": {"type": _NULL_TEXT, "description": "The host id the call named."},
        "hosts": {
            "type": "array",
            "items": _TPM_ROW_SCHEMA,
            "description": "One flat row per host. A long list comes back as a result handle.",
        },
        "host_count": {"type": "integer", "description": "Number of rows."},
        "read_notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Problems with the whole read.",
        },
        "guidance": {"type": _NULL_TEXT, "description": "What to do next, in plain words."},
    },
    "required": ["status", "hosts", "host_count"],
}

VMWARE_HOST_TPM_ATTESTATION_OP = VmwareTypedOp(
    op_id="vmware.host.tpm.attestation",
    handler_attr="host_tpm_attestation",
    summary="TPM presence, TPM version and attestation state of the hosts of a cluster.",
    description=(
        "One row per host of a cluster, or for one host: TPM supported, TPM version, Intel "
        "TXT, and vCenter's attestation state (accepted or notAccepted) with its time and "
        "message. include_report adds a summary of each host's attestation report. Read-only."
    ),
    parameter_schema=_TPM_PARAMETER_SCHEMA,
    response_schema=_TPM_RESPONSE_SCHEMA,
    group_key=HOST_CERTIFICATES_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "host", "tpm", "attestation", "vi-json"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call for a security audit of hosts: which hosts have a TPM 2.0 chip, and "
            "whether vCenter accepted their boot measurements (attestation)."
        ),
        "parameter_hints": {
            "cluster": "Cluster id from vmware.cluster.list. Give cluster or host.",
            "include_report": "Set true only when you need the report details; it is slower.",
        },
        "output_shape": (
            "{status: ok|not_found|needs_vcenter, cluster, host, hosts: [{host, name, "
            "connection_state, tpm_supported, tpm_version, txt_enabled, attestation_status, "
            "attestation_time, attestation_message, report_log_reliable, report_pcr_count, "
            "report_event_count, read_note}], host_count, read_notes, guidance}. A long "
            "hosts list comes back as a result handle; read it with result_query."
        ),
        "next_step": (
            "attestation_status notAccepted: read attestation_message, then check the host's "
            "secure boot and software. attestation_status null with tpm_supported true: "
            "vCenter has not checked the host yet, or the TPM is not 2.0. Use include_report "
            "to see whether the TPM event log is complete."
        ),
    },
)

#: The two typed reads; ``typed_ops.py`` collects them.
TYPED_OPS: Final[tuple[VmwareTypedOp, ...]] = (
    VMWARE_HOST_CERTIFICATE_SHOW_OP,
    VMWARE_HOST_TPM_ATTESTATION_OP,
)
WHEN_TO_USE_BY_GROUP: Final[dict[str, str]] = {
    HOST_CERTIFICATES_GROUP_KEY: HOST_CERTIFICATES_WHEN_TO_USE
}
