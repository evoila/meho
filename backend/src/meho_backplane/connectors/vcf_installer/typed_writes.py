# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) write implementations for :class:`InstallerConnector`.

The governed bring-up *submit* primitives (#3078, retry #3123). The vendor
bring-up API is natively asynchronous — each POST/PATCH returns its tracking
object in seconds and progress is polled via the short status GETs in
:mod:`.typed_reads` — so every call here is short, the standard
park → approve → resume machinery governs the write as-is, and no
long-blocking loop or async dispatch machinery exists in this module. The
multi-hour orchestration (submit → durable poll) belongs to the
deploy-automation add-on, not this connector.

* ``installer.sddc.spec.validate`` — ``POST /v1/sddcs/validations`` with the
  caller's ``SddcSpec``, returning the vendor ``Validation`` object (poll
  ``installer.sddc.validation.status`` to a terminal ``executionStatus``).
  A dry-run on the appliance that mutates no estate —
  ``safety_level="caution"``, no approval.
* ``installer.sddc.bringup.start`` — ``POST /v1/sddcs`` with the
  ``SddcSpec``, returning the ``SddcTask`` the moment the deploy is accepted
  (poll ``installer.sddc.bringup.status``). The estate mutation —
  ``safety_level="dangerous"`` + ``requires_approval=True``: the dispatcher
  parks the call for approval *before* this handler runs, and the park-time
  preview (registered in :mod:`.bringup`, shared with the composite) echoes
  SDDC identity + network blast-radius only, never a password.
* ``installer.sddc.bringup.retry`` — ``PATCH /v1/sddcs/{id}`` (the vendor
  ``retrySddc`` operation), resuming a ``COMPLETED_WITH_FAILURE`` bring-up
  from its failed sub-task and returning the same ``SddcTask`` to poll. The
  ``SddcSpec`` body is optional: omitted, the appliance retries the stored
  spec as-is; provided, it is the vendor's documented edit-and-retry flow.
  Same posture as the start — ``dangerous`` + ``requires_approval=True``,
  with its own park-time preview (task id + optional spec identity) in
  :mod:`.bringup`.
* The **air-gapped depot lifecycle writes** (#3121) — the three steps that
  used to force a governed bring-up out-of-band:
  ``installer.system.depot.set`` (``PUT /v1/system/settings/depot``;
  synchronous connection check, vendor rejection mapped to a structured
  ``depot_error`` with inline remediation for the TLS-trust case),
  ``installer.system.trusted-certificates.add``
  (``POST /v1/sddc-manager/trusted-certificates``), and
  ``installer.bundles.download`` (``PATCH /v1/bundles/{id}`` per id in one
  approved batch, per-bundle failures collected not fatal). All three are
  ``caution`` + ``requires_approval=True`` — appliance-scoped configuration,
  not estate mutation, but each gates what a bring-up may install.

Each write is issued directly on the connector's own authenticated token
session via :meth:`HttpConnector._post_json`. A raw ``401`` (the Installer's
expired-token signal) propagates as :class:`httpx.HTTPStatusError` to the
dispatcher's #2067 recovery arm, which evicts the cached session token via
the connector's public :meth:`InstallerConnector.invalidate_session` hook and
re-dispatches once. That retry is safe even for these non-idempotent
POSTs/PATCHes: a ``401`` means the request was rejected at auth *before* the
server processed it, so the first attempt had no effect — the same argument
:meth:`InstallerConnector._post_json_with_session_retry` documents for the
composite's internal sub-calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_installer.typed_reads import scrub_password_keys

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_installer.connector import InstallerConnector
    from meho_backplane.connectors.vcf_installer.session import InstallerTargetLike

__all__ = [
    "INSTALLER_BRINGUP_RETRY_OP_ID",
    "INSTALLER_BRINGUP_START_OP_ID",
    "INSTALLER_BUNDLES_DOWNLOAD_OP_ID",
    "INSTALLER_DEPOT_SET_OP_ID",
    "INSTALLER_SPEC_VALIDATE_OP_ID",
    "INSTALLER_TRUSTED_CERTIFICATE_ADD_OP_ID",
    "TYPED_WRITE_DECLARED_OP_IDS",
    "installer_bundles_download_impl",
    "installer_depot_settings_set_impl",
    "installer_sddc_bringup_retry_impl",
    "installer_sddc_bringup_start_impl",
    "installer_sddc_spec_validate_impl",
    "installer_trusted_certificate_add_impl",
]

#: ``installer.sddc.spec.validate`` — submit an ``SddcSpec`` validation dry-run.
INSTALLER_SPEC_VALIDATE_OP_ID: Final[str] = "installer.sddc.spec.validate"
#: ``installer.sddc.bringup.start`` — start the management-domain bring-up.
INSTALLER_BRINGUP_START_OP_ID: Final[str] = "installer.sddc.bringup.start"
#: ``installer.sddc.bringup.retry`` — resume a failed bring-up from its failed sub-task.
INSTALLER_BRINGUP_RETRY_OP_ID: Final[str] = "installer.sddc.bringup.retry"
#: ``installer.system.depot.set`` — configure the release depot (#3121).
INSTALLER_DEPOT_SET_OP_ID: Final[str] = "installer.system.depot.set"
#: ``installer.system.trusted-certificates.add`` — trust an outbound TLS cert (#3121).
INSTALLER_TRUSTED_CERTIFICATE_ADD_OP_ID: Final[str] = "installer.system.trusted-certificates.add"
#: ``installer.bundles.download`` — trigger GA bundle downloads (#3121).
INSTALLER_BUNDLES_DOWNLOAD_OP_ID: Final[str] = "installer.bundles.download"

# --- Hand-coded wire paths — the spec-reconcile lane's introspection source
# (module constants introspected by value, the #2944 pattern). ---
#: ``POST`` — submit an ``SddcSpec`` for a non-mutating validation dry-run.
_SPEC_VALIDATE_PATH = "/v1/sddcs/validations"
#: ``POST`` — start the bring-up (the estate mutation).
_BRINGUP_START_PATH = "/v1/sddcs"
#: ``PATCH`` — retry a failed bring-up (the vendor ``retrySddc`` operation).
_BRINGUP_RETRY_PATH = "/v1/sddcs/{id}"
#: ``PUT`` — configure the release depot (the vendor ``updateDepotSettings``).
_DEPOT_SET_PATH = "/v1/system/settings/depot"
#: ``POST`` — add an outbound trusted certificate (``addTrustedCertificate``).
_TRUSTED_CERTIFICATE_ADD_PATH = "/v1/sddc-manager/trusted-certificates"
#: ``PATCH`` — trigger one bundle download (``startBundleDownloadByID``).
_BUNDLE_DOWNLOAD_PATH = "/v1/bundles/{id}"

#: The exact ``METHOD:/path`` set these write primitives hand-code. The
#: reconcile lane (:mod:`tests.test_connectors_vcf_installer_spec_reconcile`)
#: asserts each is served by the pinned 9.1 OpenAPI, and pins this set so it
#: can't go vacuous.
TYPED_WRITE_DECLARED_OP_IDS: frozenset[str] = frozenset(
    {
        f"POST:{_SPEC_VALIDATE_PATH}",
        f"POST:{_BRINGUP_START_PATH}",
        f"PATCH:{_BRINGUP_RETRY_PATH}",
        f"PUT:{_DEPOT_SET_PATH}",
        f"POST:{_TRUSTED_CERTIFICATE_ADD_PATH}",
        f"PATCH:{_BUNDLE_DOWNLOAD_PATH}",
    }
)


async def installer_sddc_spec_validate_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.sddc.spec.validate`` — ``POST /v1/sddcs/validations``.

    Submits ``params["spec"]`` (validated as a JSON object by the op's
    parameter schema before dispatch) verbatim and returns the vendor
    ``Validation`` — typically ``202`` with ``executionStatus="IN_PROGRESS"``
    and the ``id`` to poll via ``installer.sddc.validation.status``. Mutates
    nothing on the estate.
    """
    return await connector._post_json(
        target, _SPEC_VALIDATE_PATH, operator=operator, json=params["spec"]
    )


async def installer_sddc_bringup_start_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.sddc.bringup.start`` — ``POST /v1/sddcs``.

    Submits ``params["spec"]`` verbatim and returns the vendor ``SddcTask``
    the moment the deploy is accepted (``202``) — the bring-up itself runs
    for hours on the appliance; poll ``installer.sddc.bringup.status`` with
    the returned ``id`` to a terminal state. The op is registered
    ``dangerous`` + ``requires_approval=True``, so the dispatcher has already
    parked and an approver has already resumed this call by the time the
    handler runs.
    """
    return await connector._post_json(
        target, _BRINGUP_START_PATH, operator=operator, json=params["spec"]
    )


async def installer_sddc_bringup_retry_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.sddc.bringup.retry`` — ``PATCH /v1/sddcs/{id}`` (``retrySddc``).

    Resumes a ``COMPLETED_WITH_FAILURE`` bring-up from its failed sub-task and
    returns the vendor ``SddcTask`` the moment the retry is accepted; poll
    ``installer.sddc.bringup.status`` with the same ``id`` to a terminal state.
    ``params["spec"]`` is optional: omitted, no request body is sent and the
    appliance retries the stored spec as-is (the common transient-failure
    case); provided, the ``SddcSpec`` rides the PATCH verbatim — the vendor's
    documented edit-and-retry flow. The op is registered ``dangerous`` +
    ``requires_approval=True``, so the dispatcher has already parked and an
    approver has already resumed this call by the time the handler runs.
    """
    return await connector._post_json(
        target,
        _BRINGUP_RETRY_PATH.replace("{id}", params["id"]),
        operator=operator,
        verb="PATCH",
        json=params.get("spec"),
    )


def _vendor_error_body(exc: httpx.HTTPStatusError) -> dict[str, Any]:
    """The vendor error envelope of *exc*'s response, or ``{}`` when unparseable."""
    try:
        body = exc.response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


#: Remediation surfaced on the depot TLS-trust failure — the common first-run
#: outcome for HTTPS offline depots (#3121: cert import fixes the settings
#: check; note the 9.1 bundle *downloader* has been observed TLS-handshaking
#: against plain-HTTP depots, so plain HTTP + the LCM ``httpsEnabled``
#: property off remains the reliable combination for HTTP-only depots).
_DEPOT_CONNECT_REMEDIATION = (
    "The depot's TLS certificate is not trusted by the appliance. Import it "
    "first via installer.system.trusted-certificates.add "
    "(certificateUsageType TRUSTED_FOR_OUTBOUND), then re-run depot.set."
)


async def installer_depot_settings_set_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.system.depot.set`` — ``PUT /v1/system/settings/depot``.

    Submits ``params["settings"]`` (the vendor ``DepotSettings`` object —
    ``depotConfiguration`` {isOfflineDepot, hostname, port} plus
    ``offlineAccount`` / ``vmwareAccount`` credentials) verbatim. The
    appliance runs its depot connection check synchronously, so the answer is
    the verdict: ``{"status": "ok", "settings": <scrubbed echo>}`` on
    ``DEPOT_CONNECTION_SUCCESSFUL``, or the structured
    ``{"status": "depot_error", http_status, error_code, message[, remediation]}``
    when the vendor rejects the settings (#1627/#1649/#1804 pattern) — the
    common ``VMWARE_DEPOT_CONNECT_FAILURE`` TLS-trust case carries its
    remediation inline. A raw ``401`` still propagates to the dispatcher's
    #2067 session-recovery arm.
    """
    try:
        payload = await connector._post_json(
            target, _DEPOT_SET_PATH, operator=operator, verb="PUT", json=params["settings"]
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise
        body = _vendor_error_body(exc)
        result: dict[str, Any] = {
            "status": "depot_error",
            "http_status": exc.response.status_code,
            "error_code": body.get("errorCode"),
            "message": body.get("message"),
        }
        if body.get("errorCode") == "VMWARE_DEPOT_CONNECT_FAILURE":
            result["remediation"] = _DEPOT_CONNECT_REMEDIATION
        return result
    return {"status": "ok", "settings": scrub_password_keys(payload)}


async def installer_trusted_certificate_add_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.system.trusted-certificates.add`` —
    ``POST /v1/sddc-manager/trusted-certificates``.

    Adds ``params["certificate"]`` (PEM) to the appliance outbound trust
    store with ``certificateUsageType`` from
    ``params["certificate_usage_type"]`` (default ``TRUSTED_FOR_OUTBOUND`` —
    the depot-trust case). Certificates are public material; the PEM rides
    the params verbatim and the preview echoes only a fingerprint.
    """
    body = {
        "certificate": params["certificate"],
        "certificateUsageType": params.get("certificate_usage_type", "TRUSTED_FOR_OUTBOUND"),
    }
    payload = await connector._post_json(
        target, _TRUSTED_CERTIFICATE_ADD_PATH, operator=operator, json=body
    )
    return {"status": "ok", "result": payload}


async def installer_bundles_download_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.bundles.download`` — ``PATCH /v1/bundles/{id}`` per bundle.

    Triggers ``{"bundleDownloadSpec": {"downloadNow": true}}`` for every id in
    ``params["bundle_ids"]`` (one approved batch — the mandatory pre-download
    step of an air-gapped bring-up, #3121). Per-bundle vendor rejections are
    collected, not fatal: each element of ``results`` is
    ``{"id", "status": "accepted"|"failed", ...}`` and the envelope reports
    ``accepted`` / ``failed`` counts with ``status`` ``"ok"`` or
    ``"partial"``. Re-triggering an in-flight/downloaded bundle is harmless —
    the vendor either no-ops or rejects that bundle individually. A raw
    ``401`` propagates (session recovery); the dispatcher's single re-dispatch
    re-PATCHes the already-accepted ids, which is safe for the same reason.
    Poll ``installer.bundles.list`` for ``downloadStatus`` afterwards.
    """
    results: list[dict[str, Any]] = []
    accepted = failed = 0
    for bundle_id in params["bundle_ids"]:
        try:
            task = await connector._post_json(
                target,
                _BUNDLE_DOWNLOAD_PATH.replace("{id}", str(bundle_id)),
                operator=operator,
                verb="PATCH",
                json={"bundleDownloadSpec": {"downloadNow": True}},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise
            body = _vendor_error_body(exc)
            failed += 1
            results.append(
                {
                    "id": bundle_id,
                    "status": "failed",
                    "http_status": exc.response.status_code,
                    "error_code": body.get("errorCode"),
                    "message": body.get("message"),
                }
            )
            continue
        accepted += 1
        results.append(
            {
                "id": bundle_id,
                "status": "accepted",
                "task_id": task.get("id"),
                "task_name": task.get("name"),
            }
        )
    return {
        "status": "ok" if failed == 0 else "partial",
        "accepted": accepted,
        "failed": failed,
        "results": results,
    }
