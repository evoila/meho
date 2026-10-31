# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connect a vCenter or an NSX manager to VCF Automation (provider plane).

* ``vcfa.provider.vcenter.register`` runs the next steps a vCenter needs:
  trust its certificate (only the one the approver saw), add it (switched
  on), switch it on again after a certificate fix, or refresh it so its
  Supervisors appear. Each vendor step is a job: the call returns as soon as
  VCF Automation accepted the job, with the job id. Call it again with the
  same inputs to run the next step; it skips the steps that are done.
* ``vcfa.provider.nsx_manager.register`` adds an NSX manager. The vendor
  refuses a body without ``description``, so it is always sent.

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
    INFRA_NSX_MANAGERS_PATH,
    INFRA_TEST_CONNECTION_PATH,
    INFRA_TRUSTED_CERTIFICATES_PATH,
    INFRA_VCENTER_PATH,
    INFRA_VCENTER_REFRESH_PATH,
    INFRA_VCENTERS_PATH,
    provider_vcf_path,
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
    "NSX_REGISTER_OP_ID",
    "VCENTER_REGISTER_OP_ID",
    "StepFailedError",
    "certificate_fingerprint",
    "provider_nsx_manager_register",
    "provider_vcenter_register",
    "run_step",
]

VCENTER_REGISTER_OP_ID: Final = "vcfa.provider.vcenter.register"
NSX_REGISTER_OP_ID: Final = "vcfa.provider.nsx_manager.register"
_STATUS_READ: Final = "vcfa.provider.infra.list"
_VC_STEPS: Final = ("trust_certificate", "add", "enable", "refresh")
#: vCenter listener states: VCF Automation is connecting now.
_VC_CONNECTING: Final = frozenset({"CONNECTING", "CONNECTED_SYNCING"})
#: vCenter listener states that need the vCenter switched on again.
_VC_NEEDS_ENABLE: Final = frozenset({"INITIAL", "INVALID_SETTINGS", "DISCONNECTED"})
#: NSX manager states of a job that still runs.
_NSX_RUNNING: Final = frozenset({"PENDING", "CONFIGURING"})
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


def _same_endpoint(row: Mapping[str, Any], url: str, name: str) -> bool:
    row_url = str(row.get("url") or "")
    if row_url and _endpoint(row_url) == _endpoint(url):
        return True
    return str(row.get("name") or "").casefold() == name.casefold()


def _answer(
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
    existing = next((row for row in rows if _same_endpoint(row, url, name)), None)
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
    return {"would": would, "existing": existing, "certificate": cert}


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
                "fingerprint with the vCenter's own"
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
    problem = _approval_problem(plan)
    if problem is not None:
        return _answer(
            "precondition_failed", log, obj=obj, certificate=_public_cert(cert), guidance=problem
        )
    would = plan["would"]
    if would in {"already_running", "unchanged"}:
        for step in _VC_STEPS:
            log.mark(step, "skipped", f"the vCenter state is {obj['state']}")
        guidance = (
            "VCF Automation is connecting to this vCenter now; read vcfa.provider.infra.list "
            "until the state is CONNECTED. Nothing was started."
            if would == "already_running"
            else "Nothing to do: the vCenter is added, on and connected."
        )
        return _answer(would, log, obj=obj, guidance=guidance)
    if cert is not None:
        refusal = _certificate_refusal(cert)
        if refusal is not None:
            return _answer(
                "invalid_request", log, obj=obj, certificate=_public_cert(cert), guidance=refusal
            )
        if cert.get("needs_trust") and approved_preview(VCENTER_REGISTER_OP_ID) is None:
            return _answer(
                "certificate_not_trusted",
                log,
                obj=obj,
                certificate=_public_cert(cert),
                guidance=(
                    "VCF Automation does not trust the vCenter certificate, and MEHO trusts a "
                    "certificate only after a person approved its fingerprint. Run "
                    "preview_operation for this call, compare the fingerprint with the "
                    "vCenter's own, and ask for approval. Nothing was written."
                ),
            )
    password = None
    if would in {"add", "enable"}:
        password = await read_password(operator, target, params)
        if password is None:
            return _answer(
                "invalid_request", log, obj=obj, guidance=password_missing_guidance(params)
            )
    try:
        call = Call(connector, target, operator)
        await _trust_step(call, log, cert, str(params["url"]))
        job = await _start_vcenter_job(call, params, plan, log, password)
    except StepFailedError:
        return _answer(
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
    return _answer(
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
        "current_state": None,
        "certificate": None,
    }
    connector: Any = ctx.connector_instance
    if connector is None or not params.get("url") or not params.get("name"):
        return preview
    plan = await _vcenter_plan(connector, ctx.target, ctx.operator, params)
    return {
        **preview,
        "would": plan["would"],
        "current_state": (plan["existing"] or {}).get("listenerState"),
        "certificate": _public_cert(plan["certificate"]),
    }


# ---------------------------------------------------------------------------
# vcfa.provider.nsx_manager.register
# ---------------------------------------------------------------------------


def _nsx_object(params: Mapping[str, Any], existing: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "kind": "nsx_manager",
        "name": (existing or {}).get("name") or params["name"],
        "url": (existing or {}).get("url") or params["url"],
        "id": (existing or {}).get("id"),
        "state": (existing or {}).get("status"),
    }


async def provider_nsx_manager_register(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.nsx_manager.register`` -- add an NSX manager (one job)."""
    log = StepLog(("add",))
    url, name = str(params["url"]), str(params["name"])
    rows = await list_vcf(connector, target, operator, INFRA_NSX_MANAGERS_PATH)
    existing = next((row for row in rows if _same_endpoint(row, url, name)), None)
    if existing is not None:
        state = existing.get("status")
        running = state in _NSX_RUNNING
        log.mark("add", "skipped", f"an NSX manager with this URL or name exists ({state})")
        return _answer(
            "already_running" if running else "unchanged",
            log,
            obj=_nsx_object(params, existing),
            guidance=(
                "VCF Automation is still setting this NSX manager up; read "
                "vcfa.provider.infra.list until its state is REALIZED. Nothing was started."
                if running
                else "Nothing to do: this NSX manager is already added."
            ),
        )
    password = await read_password(operator, target, params)
    if password is None:
        return _answer(
            "invalid_request",
            log,
            obj=_nsx_object(params, None),
            guidance=password_missing_guidance(params),
        )
    body = {
        "name": name,
        # The vendor refuses a body without a description, even an empty one.
        "description": params.get("description") or "",
        "url": url,
        "username": params["username"],
        "password": password,
    }
    resp = await connector._send_with_retry(
        target,
        "POST",
        provider_vcf_path(target, INFRA_NSX_MANAGERS_PATH),
        operator=operator,
        json=body,
    )
    job = job_from_response(resp, status_read=_STATUS_READ)
    log.mark("add", "started", "VCF Automation accepted the job", job_id=job["id"])
    return _answer(
        "accepted",
        log,
        obj=_nsx_object(params, None),
        job=job,
        guidance=(
            "Read vcfa.provider.infra.list (with job_id) until the NSX manager state is "
            "REALIZED, then use it in vcfa.provider.region.set."
        ),
    )


async def _nsx_register_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time preview: the NSX manager to add, or the one that exists."""
    params = ctx.params
    preview: dict[str, Any] = {
        "action": "register_nsx_manager",
        "name": params.get("name"),
        "url": params.get("url"),
        "username": params.get("username"),
        "would": "unknown",
        "current_state": None,
    }
    connector: Any = ctx.connector_instance
    if connector is None or not params.get("url") or not params.get("name"):
        return preview
    rows = await list_vcf(connector, ctx.target, ctx.operator, INFRA_NSX_MANAGERS_PATH)
    url, name = str(params["url"]), str(params["name"])
    existing = next((row for row in rows if _same_endpoint(row, url, name)), None)
    if existing is None:
        return {**preview, "would": "add"}
    state = existing.get("status")
    return {
        **preview,
        "would": "already_running" if state in _NSX_RUNNING else "unchanged",
        "current_state": state,
    }


register_preview_builder(VCENTER_REGISTER_OP_ID, _vcenter_register_preview)
register_preview_builder(NSX_REGISTER_OP_ID, _nsx_register_preview)
