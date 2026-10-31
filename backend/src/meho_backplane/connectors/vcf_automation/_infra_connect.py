# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connect a vCenter to VCF Automation (provider plane).

* ``vcfa.provider.vcenter.register`` runs the next steps a vCenter needs:
  trust its certificate (only the one the approver saw), add it (switched
  on), switch it on again after a certificate fix, or refresh it so its
  Supervisors appear. Each vendor step is a job: the call returns as soon as
  VCF Automation accepted the job, with the job id. Call it again with the
  same inputs to run the next step; it skips the steps that are done.
* ``vcfa.provider.nsx_manager.register`` lives in :mod:`._infra_nsx` and
  uses the shared helpers here (:func:`match_existing`, :func:`answer`).

Credentials come from a Vault reference (``password_secret_ref``) read
under the caller's identity. The password goes only into the request body;
it never reaches an answer, a preview, a log or the audit row.

Wire facts follow ``go-vcloud-director`` v3 (``govcd/vsphere_vcenter.go``,
``govcd/nsxt_manager_openapi.go``, ``govcd/trusted_certificates.go``) and
the VCF Automation 9.0, 9.1 and 9.1.1 provider API references.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

import httpx

from meho_backplane.connectors.vcf_automation._infra_common import (
    Call,
    StepLog,
    job_from_response,
    now_iso,
    uuid_of,
    vendor_error_text,
)
from meho_backplane.connectors.vcf_automation._infra_paths import (
    INFRA_TEST_CONNECTION_PATH,
    INFRA_TRUSTED_CERTIFICATES_PATH,
    INFRA_VCENTER_PATH,
    INFRA_VCENTER_REFRESH_PATH,
    INFRA_VCENTERS_PATH,
    safe_segment,
)
from meho_backplane.connectors.vcf_automation._infra_reads import list_vcf
from meho_backplane.connectors.vcf_automation._lookups import (
    password_missing_guidance,
    read_password,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    approved_preview,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "VCENTER_REGISTER_OP_ID",
    "StepFailedError",
    "answer",
    "certificate_fingerprint",
    "match_existing",
    "name_taken_guidance",
    "provider_vcenter_register",
    "run_step",
]

VCENTER_REGISTER_OP_ID: Final = "vcfa.provider.vcenter.register"
_STATUS_READ: Final = "vcfa.provider.infra.list"
_VC_STEPS: Final = ("trust_certificate", "add", "enable", "refresh")
#: vCenter listener states: VCF Automation is connecting now.
_VC_CONNECTING: Final = frozenset({"CONNECTING", "CONNECTED_SYNCING"})
#: vCenter listener states that need the vCenter switched on again.
_VC_NEEDS_ENABLE: Final = frozenset({"INITIAL", "INVALID_SETTINGS", "DISCONNECTED"})
_PEM_BLOCK: Final = re.compile(
    r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.DOTALL
)


class StepFailedError(Exception):
    """A step failed after an earlier step changed something (see :func:`run_step`)."""


async def run_step(
    log: StepLog,
    step: str,
    action: Callable[[], Awaitable[httpx.Response]],
    *,
    hide: tuple[str, ...] = (),
) -> httpx.Response:
    """Run one vendor call of *step*.

    When it fails and no step changed anything yet, the error goes to the
    dispatcher as it is (nothing to report part-way). When an earlier step
    already ran, the step is logged as failed with the vendor's text (each
    *hide* value removed) and :class:`StepFailedError` stops the change.
    """
    try:
        return await action()
    except (httpx.HTTPStatusError, RuntimeError) as exc:
        if not log.any_ran():
            raise
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        log.fail(step, vendor_error_text(exc, hide=hide), status)
        raise StepFailedError(step) from exc


def certificate_fingerprint(pem: str) -> str | None:
    """SHA-256 fingerprint (``AA:BB:...``) of the first certificate in *pem*."""
    match = _PEM_BLOCK.search(pem)
    if match is None:
        return None
    try:
        der = base64.b64decode("".join(match.group(1).split()), validate=True)
    except (binascii.Error, ValueError):
        return None
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _cert_to_trust(chain: str) -> tuple[str, int]:
    """The certificate to trust and the chain length (the vendor SDK's rule).

    One certificate: that one. A chain: the last one (the issuer at the top).
    """
    blocks = [m.group(0) for m in _PEM_BLOCK.finditer(chain)]
    if not blocks:
        return "", 0
    return blocks[-1], len(blocks)


def _endpoint(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    return (parts.hostname or "").lower(), parts.port or 443


def match_existing(
    rows: list[dict[str, Any]], url: str, name: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The existing object at *url* (host and port), or another one that has *name*.

    Returns ``(match, name_taken_by)``. An object counts as the one asked for
    only when its address matches. A different object with the same name is
    returned second, so the caller can refuse the call.
    """
    wanted = _endpoint(url)
    for row in rows:
        row_url = str(row.get("url") or "")
        if row_url and _endpoint(row_url) == wanted:
            return row, None
    taken = next(
        (row for row in rows if str(row.get("name") or "").casefold() == name.casefold()), None
    )
    return None, taken


def name_taken_guidance(kind: str, name: str, row: Mapping[str, Any]) -> str:
    return (
        f"A {kind} named {name!r} already exists with the URL {row.get('url')!r}, which is "
        "not the URL in this call; nothing was written. Use another name, or pass that URL "
        f"to work on the existing {kind}."
    )


def answer(
    status: str,
    log: StepLog,
    *,
    obj: dict[str, Any],
    guidance: str,
    job: dict[str, Any] | None = None,
    certificate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "object": obj,
        "job": job,
        "certificate": certificate,
        "steps": log.as_list(),
        "checked_at": now_iso(),
        "guidance": guidance,
    }


# ---------------------------------------------------------------------------
# vcfa.provider.vcenter.register
# ---------------------------------------------------------------------------


async def _certificate_check(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    url: str,
) -> dict[str, Any]:
    """Ask VCF Automation to connect to the vCenter and report its certificate."""
    host, port = _endpoint(url)
    probe = await connector._post_json(
        target,
        INFRA_TEST_CONNECTION_PATH,
        operator=operator,
        json={
            "host": host,
            "port": port,
            "secure": True,
            "timeout": 10,
            "hostnameVerificationAlgorithm": "HTTPS",
        },
    )
    result = probe.get("targetProbe") or {}
    pem, count = _cert_to_trust(str(result.get("certificateChain") or ""))
    ssl_result = result.get("sslResult")
    return {
        "connection_result": result.get("connectionResult"),
        "ssl_result": ssl_result,
        "fingerprint_sha256": certificate_fingerprint(pem) if pem else None,
        "certificate_count": count,
        "needs_trust": ssl_result == "ERROR_UNTRUSTED_CERTIFICATE" and bool(pem),
        "_pem": pem,
    }


def _public_cert(cert: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if cert is None:
        return None
    return {key: value for key, value in cert.items() if not key.startswith("_")}


async def _vcenter_plan(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """What the call would do now: ``would``, the vCenter as read, its certificate."""
    url, name = str(params["url"]), str(params["name"])
    rows = await list_vcf(connector, target, operator, INFRA_VCENTERS_PATH)
    existing, name_taken_by = match_existing(rows, url, name)
    if name_taken_by is not None:
        return {
            "would": "name_taken",
            "existing": None,
            "name_taken_by": name_taken_by,
            "certificate": None,
        }
    if existing is None:
        would = "add"
    else:
        state = str(existing.get("listenerState") or "")
        if state in _VC_CONNECTING:
            would = "already_running"
        elif not existing.get("isEnabled") or state in _VC_NEEDS_ENABLE:
            would = "enable"
        elif state == "CONNECTED":
            would = "refresh" if params.get("refresh", True) else "unchanged"
        else:
            would = "unchanged"
    cert = None
    if would in {"add", "enable"}:
        cert = await _certificate_check(connector, target, operator, url)
    return {"would": would, "existing": existing, "name_taken_by": None, "certificate": cert}


def _vc_object(params: Mapping[str, Any], existing: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "kind": "vcenter",
        "name": (existing or {}).get("name") or params["name"],
        "url": (existing or {}).get("url") or params["url"],
        "id": (existing or {}).get("vcId"),
        "state": (existing or {}).get("listenerState"),
    }


def _approval_problem(plan: Mapping[str, Any]) -> str | None:
    """Why the approved run must stop: the plan or certificate changed since the preview."""
    seen = approved_preview(VCENTER_REGISTER_OP_ID)
    if seen is None:
        return None
    if seen.get("would") != plan["would"]:
        return (
            f"the approver saw the next step {seen.get('would')!r}, but it is now "
            f"{plan['would']!r}; nothing was written. Preview the call again and ask "
            "for a new approval"
        )
    cert = plan.get("certificate") or {}
    if cert.get("needs_trust"):
        seen_fp = (seen.get("certificate") or {}).get("fingerprint_sha256")
        if seen_fp != cert.get("fingerprint_sha256"):
            return (
                "the vCenter now shows a certificate other than the one the approver saw; "
                "nothing was trusted or written. Preview the call again and compare the "
                "fingerprint with the certificate to trust (the issuer when the chain has "
                "more than one)"
            )
    return None


def _certificate_refusal(cert: Mapping[str, Any]) -> str | None:
    """Why the certificate check stops the call before any write, or ``None``."""
    if cert.get("connection_result") not in (None, "SUCCESS"):
        return (
            f"VCF Automation cannot connect to the vCenter "
            f"({cert.get('connection_result')}); nothing was written. Check the URL"
        )
    if cert.get("ssl_result") not in ("SUCCESS", "ERROR_UNTRUSTED_CERTIFICATE"):
        return (
            f"the certificate check failed ({cert.get('ssl_result')}); nothing was written. "
            "Check that the URL uses the vCenter's own host name"
        )
    return None


async def _trust_step(call: Call, log: StepLog, cert: Mapping[str, Any] | None, url: str) -> None:
    if not cert or not cert.get("needs_trust"):
        log.mark("trust_certificate", "skipped", "the certificate is already trusted")
        return
    host, _port = _endpoint(url)
    await run_step(
        log,
        "trust_certificate",
        lambda: call.connector._send_with_retry(
            call.target,
            "POST",
            INFRA_TRUSTED_CERTIFICATES_PATH,
            operator=call.operator,
            json={"alias": f"{host}_{now_iso()}", "certificate": cert["_pem"]},
        ),
    )
    log.mark("trust_certificate", "done", f"trusted {cert.get('fingerprint_sha256')}")


async def _start_vcenter_job(
    call: Call,
    params: Mapping[str, Any],
    plan: Mapping[str, Any],
    log: StepLog,
    password: str | None,
) -> dict[str, Any]:
    """Send the one vendor call that starts a job (add, enable or refresh)."""
    would, existing = plan["would"], plan["existing"] or {}
    hide = (password,) if password else ()
    if would == "add":
        body = {
            "name": params["name"],
            "description": params.get("description") or "",
            "url": params["url"],
            "username": params["username"],
            "password": password,
            "isEnabled": True,
        }
        verb, path, step = "POST", INFRA_VCENTERS_PATH, "add"
    elif would == "enable":
        body = {**existing, "isEnabled": True, "username": params["username"]}
        body["password"] = password
        vc_urn = safe_segment(existing.get("vcId"), "vCenter id")
        verb, path, step = "PUT", INFRA_VCENTER_PATH.format(vcUrn=vc_urn), "enable"
        log.mark("add", "skipped", "the vCenter is already added")
    else:
        body = None
        vc_uuid = safe_segment(uuid_of(str(existing.get("vcId") or "")), "vCenter id")
        verb, path, step = "POST", INFRA_VCENTER_REFRESH_PATH.format(id=vc_uuid), "refresh"
        log.mark("add", "skipped", "the vCenter is already added")
        log.mark("enable", "skipped", "the vCenter is on and connected")
    resp = await run_step(
        log,
        step,
        lambda: call.connector._send_with_retry(
            call.target, verb, path, operator=call.operator, json=body
        ),
        hide=hide,
    )
    job = job_from_response(resp, status_read=_STATUS_READ)
    log.mark(step, "started", "VCF Automation accepted the job", job_id=job["id"])
    if would == "add":
        log.mark("enable", "skipped", "the add switches the vCenter on")
    return job


_VC_NEXT: Final[dict[str, str]] = {
    "add": (
        "Read vcfa.provider.infra.list (with job_id) until the vCenter state is CONNECTED, "
        "then call this op again with the same inputs to refresh it so its Supervisors appear."
    ),
    "enable": (
        "Read vcfa.provider.infra.list (with job_id) until the vCenter state is CONNECTED, "
        "then call this op again with the same inputs to refresh it."
    ),
    "refresh": (
        "Read vcfa.provider.infra.list (with job_id) until the job ends; the Supervisors of "
        "the vCenter then show up (region: none means free)."
    ),
}


def _vcenter_early_answer(
    params: Mapping[str, Any], plan: Mapping[str, Any], obj: dict[str, Any], log: StepLog
) -> dict[str, Any] | None:
    """The answer when the plan says to write nothing, or ``None`` to go on."""
    if plan["name_taken_by"] is not None:
        for step in _VC_STEPS:
            log.mark(step, "skipped", "another vCenter has this name")
        return answer(
            "invalid_request",
            log,
            obj=obj,
            guidance=name_taken_guidance("vCenter", str(params["name"]), plan["name_taken_by"]),
        )
    problem = _approval_problem(plan)
    if problem is not None:
        return answer(
            "precondition_failed",
            log,
            obj=obj,
            certificate=_public_cert(plan["certificate"]),
            guidance=problem,
        )
    would = plan["would"]
    if would not in {"already_running", "unchanged"}:
        return None
    for step in _VC_STEPS:
        log.mark(step, "skipped", f"the vCenter state is {obj['state']}")
    guidance = (
        "VCF Automation is connecting to this vCenter now; read vcfa.provider.infra.list "
        "until the state is CONNECTED. Nothing was started."
        if would == "already_running"
        else "Nothing to do: the vCenter is added, on and connected."
    )
    return answer(would, log, obj=obj, guidance=guidance)


async def provider_vcenter_register(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.vcenter.register`` -- run the next step a vCenter needs."""
    log = StepLog(_VC_STEPS)
    plan = await _vcenter_plan(connector, target, operator, params)
    obj, cert = _vc_object(params, plan["existing"]), plan["certificate"]
    early = _vcenter_early_answer(params, plan, obj, log)
    if early is not None:
        return early
    would = plan["would"]
    if cert is not None:
        refusal = _certificate_refusal(cert)
        if refusal is not None:
            return answer(
                "invalid_request", log, obj=obj, certificate=_public_cert(cert), guidance=refusal
            )
        if cert.get("needs_trust") and approved_preview(VCENTER_REGISTER_OP_ID) is None:
            return answer(
                "certificate_not_trusted",
                log,
                obj=obj,
                certificate=_public_cert(cert),
                guidance=(
                    "VCF Automation does not trust the vCenter certificate, and MEHO trusts a "
                    "certificate only after a person approved its fingerprint. Run "
                    "preview_operation for this call, compare the fingerprint with the "
                    "certificate to trust (the vCenter's own, or the issuer at the top when "
                    "the chain has more than one), and ask for approval. Nothing was written."
                ),
            )
    password = None
    if would in {"add", "enable"}:
        password = await read_password(operator, target, params)
        if password is None:
            return answer(
                "invalid_request", log, obj=obj, guidance=password_missing_guidance(params)
            )
    try:
        call = Call(connector, target, operator)
        await _trust_step(call, log, cert, str(params["url"]))
        job = await _start_vcenter_job(call, params, plan, log, password)
    except StepFailedError:
        return answer(
            "failed_part_way",
            log,
            obj=obj,
            certificate=_public_cert(cert),
            guidance=(
                "A step failed after an earlier step ran. The steps that ran are not undone. "
                "Read vcfa.provider.infra.list, fix the cause, then call again; done steps "
                "are skipped."
            ),
        )
    return answer(
        "accepted", log, obj=obj, job=job, certificate=_public_cert(cert), guidance=_VC_NEXT[would]
    )


async def _vcenter_register_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time preview: the next step and the certificate the approver must check.

    The approved run trusts a certificate only when its fingerprint equals
    the one shown here, and stops when the next step changed.
    """
    params = ctx.params
    preview: dict[str, Any] = {
        "action": "register_vcenter",
        "name": params.get("name"),
        "url": params.get("url"),
        "username": params.get("username"),
        "would": "unknown",
        "current_name": None,
        "current_url": None,
        "current_state": None,
        "certificate": None,
    }
    connector: Any = ctx.connector_instance
    if connector is None or not params.get("url") or not params.get("name"):
        return preview
    plan = await _vcenter_plan(connector, ctx.target, ctx.operator, params)
    row = plan["existing"] or plan["name_taken_by"] or {}
    return {
        **preview,
        "would": plan["would"],
        "current_name": row.get("name"),
        "current_url": row.get("url"),
        "current_state": row.get("listenerState"),
        "certificate": _public_cert(plan["certificate"]),
    }


register_preview_builder(VCENTER_REGISTER_OP_ID, _vcenter_register_preview)
