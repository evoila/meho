# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed VCF management-domain bring-up composite for :class:`InstallerConnector`.

``installer.composite.sddc.bringup`` is the highest-blast-radius write in the
product — ``safety_level="dangerous"`` + ``requires_approval=True``. It
orchestrates the two-step Installer bring-up as one governed, approved unit:

1. **validate** — ``POST /v1/sddcs/validations`` with the ``SddcSpec`` body, then
   poll ``GET /v1/sddcs/validations/{id}`` until the ``Validation``'s
   ``executionStatus`` is terminal. Validation is a **non-mutating dry-run**, so
   it is *ungated*. The deploy is gated on the ``resultStatus``: ``SUCCEEDED`` and
   ``WARNING`` proceed (warnings surfaced in the return), anything else aborts
   **before any mutation**.
2. **deploy** — ``POST /v1/sddcs`` with the same ``SddcSpec`` body, through
   :func:`~meho_backplane.operations.composite.enforce_subop_policy`. This is the
   single state mutation and the single sub-op gate. Returns an ``SddcTask``.
3. **hand off** — a real bring-up runs for *hours* on the appliance, so the
   composite returns the ``SddcTask`` id the moment the deploy is accepted
   (``status="deploying"``). The caller (an operator or the deploy-automation
   add-on's durable workflow) polls ``installer.sddc.bringup.status`` to a terminal
   ``COMPLETED_WITH_SUCCESS`` / ``ROLLBACK_SUCCESS`` / ``COMPLETED_WITH_FAILURE``.
   Blocking a single dispatch call to a multi-hour terminal state would be wrong.

**Approval gate.** The top-level composite carries ``requires_approval=True`` —
the dispatcher parks it for approval *before* the handler runs, showing the
approver the secret-hygienic preview. The deploy sub-op passes
``requires_approval=False`` to :func:`enforce_subop_policy` so that for the
intended **non-agent operator / automation service-account** caller (the add-on
runs no LLM in its execute path) the top-level approval is the single gate and
the resumed dispatch AUTO_EXECUTEs the deploy. Caveat, not a claim of
double-gate-immunity for *every* principal: a run-bound **agent** calling this
op directly is subject to the backplane's unconditional ``dangerous``
safety-ceiling on the sub-op too, so it would re-park a second approval — a
fleet-wide property of every ``dangerous`` composite (the vmware-rest #2301
sub-op pattern), not specific to this op; agent callers of the bring-up are not
a supported path today.

**Direct-session dispatch (Goal #2247).** Every sub-call goes through the
injected connector's own session helpers
(:meth:`~InstallerConnector._post_json_with_session_retry` /
:meth:`~InstallerConnector._get_json_with_session_retry`), never
``dispatch_child`` into an ingested ``METHOD:/path`` primitive — so correctness
never depends on mutable per-deploy catalog state.

**Secret hygiene (#1503).** :func:`_blast_radius` reads only a whitelist of SDDC
identity + network keys and *never* reads any ``*Password`` / ``credentials``
field of the ``SddcSpec``. The park-time approval **preview** and the **sub-op
policy params** — the two reviewer-facing surfaces — are built from it, redaction
by construction. This does **not** cover ``ApprovalRequest.params``: when the
top-level op parks, the dispatcher persists the raw dispatch ``params`` (i.e. the
full ``SddcSpec`` including plaintext passwords) verbatim on that row for the
approval TTL (#1503's store-verbatim). That column is never surfaced by any API /
audit / broadcast read path (those carry only ``params_hash`` / the scrubbed
preview), so exposure is at-rest-in-the-governance-table, gated by DB/row access
control — the same posture as every ``requires_approval`` op that takes inline
secrets (e.g. the GOSC composites). It is *not* redacted.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vcf_installer.typed_writes import (
    INSTALLER_BRINGUP_RETRY_OP_ID,
    INSTALLER_BRINGUP_START_OP_ID,
    INSTALLER_BUNDLES_DOWNLOAD_OP_ID,
    INSTALLER_DEPOT_SET_OP_ID,
    INSTALLER_TRUSTED_CERTIFICATE_ADD_OP_ID,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder
from meho_backplane.operations.composite import enforce_subop_policy

if TYPE_CHECKING:
    from meho_backplane.connectors.vcf_installer.connector import InstallerConnector
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "BRINGUP_DECLARED_OP_IDS",
    "INSTALLER_BRINGUP_OP_ID",
    "installer_sddc_bringup_composite",
    "register_installer_composite_operations",
]

_log = structlog.get_logger(__name__)

#: The governed bring-up composite op_id (``<product>.composite.<noun>.<verb>``).
INSTALLER_BRINGUP_OP_ID = "installer.composite.sddc.bringup"

#: Governance key for the deploy sub-op fed to :func:`enforce_subop_policy` — the
#: logical mutation identifier, not a wire path.
_DEPLOY_SUBOP_OP_ID = "installer.sddc.deploy"

#: Same operation group as the ``installer.sddc.bringup.status`` poll, so discovery
#: co-locates "run a bring-up" with "track a bring-up".
_GROUP_BRINGUP = "installer-bringup"

# --- Hand-coded wire paths — the spec-reconcile lane's introspection source. ---
#: ``POST`` — submit an ``SddcSpec`` for a non-mutating validation dry-run.
_VALIDATE_PATH = "/v1/sddcs/validations"
#: ``POST`` — start the bring-up (the single state mutation).
_DEPLOY_PATH = "/v1/sddcs"
#: ``GET`` — poll one validation's ``executionStatus`` to a terminal state.
_VALIDATION_STATUS_PATH = "/v1/sddcs/validations/{id}"

#: The exact ``METHOD:/path`` set this composite hand-codes. The reconcile lane
#: (:mod:`tests.test_connectors_vcf_installer_spec_reconcile`) asserts each is
#: served by the pinned 9.1 OpenAPI, and pins this set so it can't go vacuous.
BRINGUP_DECLARED_OP_IDS: frozenset[str] = frozenset(
    {
        f"POST:{_VALIDATE_PATH}",
        f"POST:{_DEPLOY_PATH}",
        f"GET:{_VALIDATION_STATUS_PATH}",
    }
)

# --- Validation poll bounds (module globals so tests monkeypatch to 0). ---
#: Wall-clock ceiling for the validation poll. Validation is minutes, not hours.
_VALIDATION_TIMEOUT_SECONDS = 1200.0
#: Delay between validation-status polls.
_VALIDATION_POLL_INTERVAL = 10.0

# --- Validation status vocabulary (from the vendored 9.1 ``Validation`` schema). ---
#: ``executionStatus`` values that mean "still running; keep polling".
_VALIDATION_NONTERMINAL: frozenset[str] = frozenset({"IN_PROGRESS", "CANCELLATION_IN_PROGRESS"})
#: ``resultStatus`` values that permit the deploy (``WARNING`` is non-fatal in
#: VCF; its checks are surfaced in the return so the approver sees them).
_VALIDATION_PASS_RESULTS: frozenset[str] = frozenset({"SUCCEEDED", "WARNING"})

#: Cap on echoed validation-check summaries, so a large check list can't bloat
#: the return envelope.
_MAX_CHECK_SUMMARIES = 40


@dataclass(frozen=True)
class _ValidationVerdict:
    """Outcome of classifying a terminal (or timed-out) ``Validation``."""

    #: ``"pass"`` | ``"validation_failed"`` | ``"validation_timeout"``.
    status: str
    #: Non-fatal ``WARNING`` check summaries surfaced on a passing verdict.
    warnings: list[dict[str, Any]]


def _execution_status(validation: dict[str, Any]) -> str:
    """Upper-cased ``executionStatus`` of a ``Validation`` (``""`` if absent)."""
    return str(validation.get("executionStatus") or "").upper()


def _result_status(validation: dict[str, Any]) -> str:
    """Upper-cased ``resultStatus`` of a ``Validation`` (``""`` if absent)."""
    return str(validation.get("resultStatus") or "").upper()


def _collect_checks(validation: dict[str, Any], *, want: str) -> list[dict[str, Any]]:
    """Flatten ``validationChecks`` (recursing ``nestedValidationChecks``) to the
    leaf checks whose ``resultStatus`` equals *want*, as secret-free summaries.

    Echoes only ``description`` / ``severity`` / ``resultStatus`` — never
    ``errorResponse`` (which can be large and is not needed to triage), and never
    any spec value. Capped at :data:`_MAX_CHECK_SUMMARIES`.
    """
    out: list[dict[str, Any]] = []

    def _walk(checks: Any) -> None:
        if not isinstance(checks, list):
            return
        for check in checks:
            if not isinstance(check, dict):
                continue
            if (
                len(out) < _MAX_CHECK_SUMMARIES
                and str(check.get("resultStatus") or "").upper() == want
            ):
                out.append(
                    {
                        "description": check.get("description"),
                        "severity": check.get("severity"),
                        "resultStatus": check.get("resultStatus"),
                    }
                )
            _walk(check.get("nestedValidationChecks"))

    _walk(validation.get("validationChecks"))
    return out


def _classify_validation(validation: dict[str, Any]) -> _ValidationVerdict:
    """Map a terminal (or poll-exhausted) ``Validation`` to a deploy verdict."""
    if _execution_status(validation) in _VALIDATION_NONTERMINAL:
        # The poll loop returns a still-running validation only when it ran out
        # of wall-clock budget.
        return _ValidationVerdict(status="validation_timeout", warnings=[])
    if (
        _execution_status(validation) == "COMPLETED"
        and _result_status(validation) in _VALIDATION_PASS_RESULTS
    ):
        warnings = (
            _collect_checks(validation, want="WARNING")
            if _result_status(validation) == "WARNING"
            else []
        )
        return _ValidationVerdict(status="pass", warnings=warnings)
    return _ValidationVerdict(status="validation_failed", warnings=[])


async def _await_validation(
    connector: InstallerConnector,
    target: Any,
    operator: Operator,
    initial: dict[str, Any],
) -> dict[str, Any]:
    """Poll ``GET /v1/sddcs/validations/{id}`` until ``executionStatus`` is
    terminal or the wall-clock budget is spent; return the last ``Validation``.

    *initial* is the ``Validation`` the ``POST /v1/sddcs/validations`` returned;
    if it is already terminal (a synchronous ``200``) no GET is issued. A missing
    ``id`` mid-flight short-circuits (the caller classifies the still-running body
    as a timeout) rather than looping forever against an unpollable validation.
    """
    validation = initial
    deadline = time.monotonic() + _VALIDATION_TIMEOUT_SECONDS
    while _execution_status(validation) in _VALIDATION_NONTERMINAL:
        if time.monotonic() >= deadline:
            return validation
        validation_id = validation.get("id")
        if not isinstance(validation_id, str) or not validation_id:
            return validation
        await asyncio.sleep(_VALIDATION_POLL_INTERVAL)
        validation = await connector._get_json_with_session_retry(
            target,
            _VALIDATION_STATUS_PATH.replace("{id}", validation_id),
            operator=operator,
        )
    return validation


def _as_dict(value: Any) -> dict[str, Any]:
    """*value* if it is a dict, else an empty dict (typed narrowing helper)."""
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """*value* if it is a list, else an empty list (typed narrowing helper)."""
    return value if isinstance(value, list) else []


def _network_summary(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Secret-free per-network summary of ``networkSpecs`` (identity fields only)."""
    networks = _as_list(spec.get("networkSpecs"))
    return [
        {
            "networkType": net.get("networkType"),
            "subnet": net.get("subnet"),
            "gateway": net.get("gateway"),
            "vlanId": net.get("vlanId"),
            "mtu": net.get("mtu"),
        }
        for net in networks
        if isinstance(net, dict)
    ]


def _blast_radius(spec: dict[str, Any]) -> dict[str, Any]:
    """SDDC identity + network blast-radius, with **zero** secret exposure.

    Reads only a fixed whitelist of identity / network keys. It never reads any
    ``*Password`` (``rootVcenterPassword``, ``rootNsxtManagerPassword``,
    ``nsxtAdminPassword``, ``sddcManagerSpec.rootPassword``, …) or ``credentials``
    field of the ``SddcSpec`` — redaction by construction. Shared by the sub-op
    policy params and the park-time preview so both echo exactly this and nothing
    else.
    """
    vcenter = _as_dict(spec.get("vcenterSpec"))
    nsxt = _as_dict(spec.get("nsxtSpec"))
    dns = _as_dict(spec.get("dnsSpec"))
    sddc_manager = _as_dict(spec.get("sddcManagerSpec"))
    host_specs = _as_list(spec.get("hostSpecs"))
    nsxt_managers = _as_list(nsxt.get("nsxtManagers"))
    return {
        "sddc_id": spec.get("sddcId"),
        "vcf_instance_name": spec.get("vcfInstanceName") or spec.get("sddcId"),
        "workflow_type": spec.get("workflowType"),
        "vcenter_hostname": vcenter.get("vcenterHostname"),
        "sso_domain": vcenter.get("ssoDomain"),
        "nsxt_vip_fqdn": nsxt.get("vipFqdn"),
        "nsxt_manager_count": len(nsxt_managers),
        "nsxt_transport_vlan_id": nsxt.get("transportVlanId"),
        "sddc_manager_hostname": sddc_manager.get("hostname"),
        "dns_subdomain": dns.get("subdomain"),
        "dns_nameservers": dns.get("nameservers"),
        "ntp_servers": spec.get("ntpServers"),
        "host_count": len(host_specs),
        "host_names": [
            h.get("hostname") for h in host_specs if isinstance(h, dict) and h.get("hostname")
        ],
        "networks": _network_summary(spec),
    }


def _require_spec(params: dict[str, Any]) -> dict[str, Any]:
    """Return ``params['spec']`` as a dict or raise a clear :exc:`ValueError`."""
    spec = params.get("spec")
    if not isinstance(spec, dict):
        raise ValueError(
            f"{INSTALLER_BRINGUP_OP_ID} requires params['spec'] to be the SddcSpec "
            f"object (a JSON object); got {type(spec).__name__}"
        )
    return spec


def _connector_id(connector: InstallerConnector) -> str:
    """The dispatch-canonical ``impl_id-version`` id, e.g. ``installer-rest-9.1``."""
    return f"{connector.impl_id}-{connector.version}"


async def installer_sddc_bringup_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: InstallerConnector,
) -> dict[str, Any] | OperationResult:
    """Handler for ``installer.composite.sddc.bringup`` — validate then deploy.

    Returns a structured ``{"status": ...}`` envelope, or an ``OperationResult``
    verbatim when the deploy sub-op gate parks/denies the write.
    """
    spec = _require_spec(params)

    # 1. VALIDATE — non-mutating dry-run, ungated. Poll to terminal, then gate
    #    the deploy on the result. Never deploy an invalid spec.
    initial = await connector._post_json_with_session_retry(
        target, _VALIDATE_PATH, operator=operator, json=spec
    )
    validation = await _await_validation(connector, target, operator, initial)
    verdict = _classify_validation(validation)
    if verdict.status != "pass":
        _log.info(
            "installer_bringup_validation_blocked",
            target=getattr(target, "name", None),
            sddc_id=spec.get("sddcId"),
            status=verdict.status,
            execution_status=validation.get("executionStatus"),
            result_status=validation.get("resultStatus"),
        )
        return {
            "status": verdict.status,
            "validation_id": validation.get("id"),
            "execution_status": validation.get("executionStatus"),
            "result_status": validation.get("resultStatus"),
            "failed_checks": _collect_checks(validation, want="FAILED"),
        }

    # 2. DEPLOY — the single mutation, through the sub-op policy gate. The
    #    top-level composite is the approval gate (requires_approval=True on the
    #    op); the sub-op passes requires_approval=False so the approved+resumed
    #    dispatch AUTO_EXECUTEs for the non-agent operator/automation caller. (An
    #    agent principal still hits the dangerous safety-ceiling here — see the
    #    module docstring's "Approval gate"; agent bring-up is unsupported today.)
    gate = await enforce_subop_policy(
        operator=operator,
        connector_id=_connector_id(connector),
        op_id=_DEPLOY_SUBOP_OP_ID,
        safety_level="dangerous",
        requires_approval=False,
        target=target,
        params=_blast_radius(spec),
    )
    if gate is not None:
        return gate

    task = await connector._post_json_with_session_retry(
        target, _DEPLOY_PATH, operator=operator, json=spec
    )

    # 3. HAND OFF — the bring-up runs for hours; return the task id for
    #    installer.sddc.bringup.status polling rather than block on a terminal state.
    _log.info(
        "installer_bringup_deploy_accepted",
        target=getattr(target, "name", None),
        sddc_id=spec.get("sddcId"),
        sddc_task_id=task.get("id"),
        task_status=task.get("status"),
    )
    result: dict[str, Any] = {
        "status": "deploying",
        "sddc_task": {
            "id": task.get("id"),
            "status": task.get("status"),
            "name": task.get("name"),
            "vcfInstanceName": task.get("vcfInstanceName"),
        },
        "poll_with": "installer.sddc.bringup.status",
        "validation_id": validation.get("id"),
        # Always echo the validation verdict so a WARNING can never be silent:
        # `validation_warnings` only carries leaf WARNING checks, which may be
        # empty even when the overall resultStatus is WARNING (parent-encoded or
        # non-literal leaf status). The approver/caller sees the verdict either way.
        "validation_result_status": validation.get("resultStatus"),
    }
    if verdict.warnings:
        result["validation_warnings"] = verdict.warnings
    return result


async def _sddc_bringup_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time approval preview for ``installer.composite.sddc.bringup``.

    Echoes SDDC identity + network blast-radius only (via :func:`_blast_radius`,
    which never reads a secret field). Returns ``None`` if the spec is missing so
    the dispatcher falls back to its identifier-only default.
    """
    spec = ctx.params.get("spec")
    if not isinstance(spec, dict):
        return None
    br = _blast_radius(spec)
    return {
        "operation": "VCF management-domain bring-up",
        "sddc_id": br["sddc_id"],
        "vcf_instance_name": br["vcf_instance_name"],
        "vcenter_hostname": br["vcenter_hostname"],
        "sddc_manager_hostname": br["sddc_manager_hostname"],
        "nsxt_vip_fqdn": br["nsxt_vip_fqdn"],
        "host_count": br["host_count"],
        "host_names": br["host_names"],
        "management_networks": br["networks"],
        "dns_nameservers": br["dns_nameservers"],
    }


# --- Registration metadata --------------------------------------------------

_BRINGUP_PARAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "spec": {
            "type": "object",
            "description": (
                "The full VCF Installer SddcSpec bring-up body (as generated by the "
                "deploy-automation factory from a resolved EnvSpec). POSTed verbatim "
                "to /v1/sddcs/validations (dry-run) then, if valid, /v1/sddcs."
            ),
            "additionalProperties": True,
        },
    },
    "required": ["spec"],
    "additionalProperties": False,
}

_BRINGUP_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Bring-up outcome envelope. status='deploying' with sddc_task.id when the "
        "deploy is accepted (poll installer.sddc.bringup.status to terminal); "
        "status='validation_failed'/'validation_timeout' with failed_checks when "
        "validation blocked the deploy."
    ),
    "additionalProperties": True,
}

_BRINGUP_WHEN_TO_USE = (
    "Use to DEPLOY a new VCF management domain through the Installer: it validates "
    "the SddcSpec (POST /v1/sddcs/validations, poll to terminal) and, only if "
    "validation passes, starts the bring-up (POST /v1/sddcs) and returns the "
    "SddcTask id. dangerous + requires_approval — the reviewer sees an SDDC "
    "identity/network preview, never passwords. The deploy runs for hours; poll "
    "installer.sddc.bringup.status with the returned id for terminal state. This is the "
    "governed replacement for a Cloud Builder / Installer bring-up run by hand."
)


async def register_installer_composite_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the bring-up composite into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``. Mirrors
    :func:`~meho_backplane.connectors.vmware_rest.composites._register.register_vmware_composite_operations`.
    """
    from meho_backplane.connectors.vcf_installer.connector import InstallerConnector
    from meho_backplane.operations.typed_register import register_composite_operation

    await register_composite_operation(
        product=InstallerConnector.product,
        version=InstallerConnector.version,
        impl_id=InstallerConnector.impl_id,
        op_id=INSTALLER_BRINGUP_OP_ID,
        handler=installer_sddc_bringup_composite,
        summary="Govern a VCF management-domain bring-up (validate then deploy).",
        description=(
            "Orchestrates the two-step VCF Installer bring-up as one approved unit: "
            "POST /v1/sddcs/validations (non-mutating dry-run, polled to terminal) "
            "gates POST /v1/sddcs (the deploy). Returns the SddcTask id the moment "
            "the deploy is accepted — the bring-up runs for hours, so poll "
            "installer.sddc.bringup.status for terminal state. dangerous + requires_approval; "
            "the approval preview echoes SDDC identity + network blast-radius only, "
            "never passwords. Direct-session dispatch (Goal #2247)."
        ),
        parameter_schema=_BRINGUP_PARAM_SCHEMA,
        response_schema=_BRINGUP_RESPONSE_SCHEMA,
        group_key=_GROUP_BRINGUP,
        when_to_use=_BRINGUP_WHEN_TO_USE,
        tags=["vcf", "installer", "bringup", "deploy", "dangerous"],
        safety_level="dangerous",
        requires_approval=True,
        embedding_service=embedding_service,
    )
    _log.info(
        "installer_composite_operations_registered",
        count=1,
        product=InstallerConnector.product,
        version=InstallerConnector.version,
        impl_id=InstallerConnector.impl_id,
    )


async def _sddc_bringup_retry_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time approval preview for ``installer.sddc.bringup.retry`` (#3123).

    The retry's normal params carry only the failed task's ``id`` — the preview
    names the operation and echoes that id so the approver knows exactly which
    bring-up resumes. For the edit-and-retry flow (an ``SddcSpec`` in params)
    the composite's secret-hygienic identity/network blast-radius is appended —
    via :func:`_blast_radius`, which never reads a secret field. Returns
    ``None`` if the id is missing so the dispatcher falls back to its
    identifier-only default.
    """
    task_id = ctx.params.get("id")
    if not isinstance(task_id, str) or not task_id:
        return None
    preview: dict[str, Any] = {
        "operation": "VCF bring-up retry (resume a failed installation task)",
        "sddc_task_id": task_id,
    }
    spec = ctx.params.get("spec")
    if isinstance(spec, dict):
        br = _blast_radius(spec)
        preview["edit_and_retry"] = True
        preview["sddc_id"] = br["sddc_id"]
        preview["vcf_instance_name"] = br["vcf_instance_name"]
        preview["vcenter_hostname"] = br["vcenter_hostname"]
        preview["sddc_manager_hostname"] = br["sddc_manager_hostname"]
        preview["host_count"] = br["host_count"]
        preview["management_networks"] = br["networks"]
    return preview


async def _depot_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time preview for ``installer.system.depot.set`` (#3121).

    Echoes depot identity only — hostname / port / mode and which account
    kind carries credentials (plus its username) — never a ``password``
    value or key.
    """
    settings = ctx.params.get("settings")
    if not isinstance(settings, dict):
        return None
    config = settings.get("depotConfiguration")
    config = config if isinstance(config, dict) else {}
    preview: dict[str, Any] = {
        "operation": "Configure Installer release depot",
        "is_offline_depot": config.get("isOfflineDepot"),
        "hostname": config.get("hostname"),
        "port": config.get("port"),
    }
    for account_key in ("offlineAccount", "vmwareAccount"):
        account = settings.get(account_key)
        if isinstance(account, dict):
            preview["account"] = account_key
            preview["account_username"] = account.get("username")
            break
    return preview


async def _trusted_certificate_add_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time preview for ``installer.system.trusted-certificates.add`` (#3121).

    Certificates are public material, but a preview should be reviewable at a
    glance — echo the usage type and a SHA-256 fingerprint of the PEM, not
    the PEM body.
    """
    certificate = ctx.params.get("certificate")
    if not isinstance(certificate, str) or not certificate:
        return None
    fingerprint = hashlib.sha256(certificate.encode()).hexdigest()
    return {
        "operation": "Trust outbound TLS certificate on Installer",
        "certificate_usage_type": ctx.params.get("certificate_usage_type")
        or "TRUSTED_FOR_OUTBOUND",
        "certificate_sha256": fingerprint,
        "certificate_length": len(certificate),
    }


async def _bundles_download_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time preview for ``installer.bundles.download`` (#3121).

    One approval covers the batch — the approver sees how many and which
    bundle ids will download (ids truncated past 20 to keep the preview
    reviewable).
    """
    bundle_ids = ctx.params.get("bundle_ids")
    if not isinstance(bundle_ids, list) or not bundle_ids:
        return None
    preview: dict[str, Any] = {
        "operation": "Download release bundles on Installer",
        "bundle_count": len(bundle_ids),
        "bundle_ids": bundle_ids[:20],
    }
    if len(bundle_ids) > 20:
        preview["bundle_ids_truncated"] = len(bundle_ids) - 20
    return preview


# Side-effect: register the park-time preview builders at import time (#1608),
# mirroring how ``connectors/vmware_rest/composites/_write_preview`` wires its
# builders. This module is imported by the package ``__init__`` (which pulls in
# ``register_installer_composite_operations``), so the builders are registered
# before any dispatch can park. The ``installer.sddc.bringup.start`` primitive
# (#3078) parks the same ``{"spec": <SddcSpec>}`` params shape the composite
# does, so it shares the composite's secret-hygienic identity/network preview.
# The ``installer.sddc.bringup.retry`` primitive (#3123) parks ``{"id", "spec"?}``
# and gets its own task-id-first preview.
register_preview_builder(INSTALLER_BRINGUP_OP_ID, _sddc_bringup_preview)
register_preview_builder(INSTALLER_BRINGUP_START_OP_ID, _sddc_bringup_preview)
register_preview_builder(INSTALLER_BRINGUP_RETRY_OP_ID, _sddc_bringup_retry_preview)
# The #3121 depot-lifecycle writes park too; their previews live here as well
# (this module is the connector's single preview home).
register_preview_builder(INSTALLER_DEPOT_SET_OP_ID, _depot_set_preview)
register_preview_builder(INSTALLER_TRUSTED_CERTIFICATE_ADD_OP_ID, _trusted_certificate_add_preview)
register_preview_builder(INSTALLER_BUNDLES_DOWNLOAD_OP_ID, _bundles_download_preview)
