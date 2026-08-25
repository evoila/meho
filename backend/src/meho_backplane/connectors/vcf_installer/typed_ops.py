# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`InstallerConnector`.

The Installer's governed bring-up surface ships as five submit/poll typed
primitives (``source_kind="typed"``, #3078; retry #3123) so they dispatch on
a fresh boot with zero catalog ingest:

* ``installer.sddc.spec.validate`` — submit an ``SddcSpec`` validation
  dry-run (``POST /v1/sddcs/validations``; ``caution``).
* ``installer.sddc.validation.status`` — poll one validation
  (``GET /v1/sddcs/validations/{id}``; ``safe``).
* ``installer.sddc.bringup.start`` — start the bring-up (``POST /v1/sddcs``;
  ``dangerous`` + ``requires_approval``).
* ``installer.sddc.bringup.retry`` — resume a failed bring-up from its
  failed sub-task (``PATCH /v1/sddcs/{id}``, the vendor ``retrySddc``;
  ``dangerous`` + ``requires_approval``).
* ``installer.sddc.bringup.status`` — poll one bring-up task
  (``GET /v1/sddcs/{id}``; ``safe``).

Plus the **air-gapped depot lifecycle** family (#3121, group
``installer-depot``): ``installer.system.depot.get`` / ``.set`` (read /
configure the release depot, PUT running the vendor connection check
synchronously), ``installer.system.trusted-certificates.list`` / ``.add``
(outbound trust store — the depot TLS-trust fix), and
``installer.bundles.list`` / ``.download`` (bundle inventory + the mandatory
explicit GA pre-download). Reads are ``safe``; the three writes are
``caution`` + ``requires_approval``.

The vendor bring-up API is natively asynchronous — every submit returns its
tracking object in seconds and progress rides the status polls — so each
primitive is a short call and the standard park → approve → resume machinery
governs ``bringup.start`` unmodified. Op bodies live in
:mod:`meho_backplane.connectors.vcf_installer.typed_reads` /
:mod:`.typed_writes`; a thin bound-method shim on :class:`InstallerConnector`
exposes each under its ``handler_attr`` name so the dispatcher's
``import_handler`` walk recovers the callable.

The one-shot governed bring-up (validate → deploy in a single approved unit)
remains the separate ``installer.composite.sddc.bringup`` composite in
:mod:`.bringup`. The dataclass + tuple + registrar shape mirrors
:mod:`meho_backplane.connectors.sddc_manager.typed_ops`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

from meho_backplane.connectors.vcf_installer.typed_writes import (
    INSTALLER_BRINGUP_RETRY_OP_ID,
    INSTALLER_BRINGUP_START_OP_ID,
    INSTALLER_BUNDLES_DOWNLOAD_OP_ID,
    INSTALLER_DEPOT_SET_OP_ID,
    INSTALLER_SPEC_VALIDATE_OP_ID,
    INSTALLER_TRUSTED_CERTIFICATE_ADD_OP_ID,
)

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "INSTALLER_TYPED_OPS",
    "INSTALLER_TYPED_WHEN_TO_USE_BY_GROUP",
    "InstallerTypedOp",
    "register_installer_typed_operations",
]

_log = structlog.get_logger(__name__)

_GROUP_BRINGUP = "installer-bringup"
_GROUP_DEPOT = "installer-depot"


@dataclass(frozen=True)
class InstallerTypedOp:
    """Metadata for one Installer typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.vcf_installer.connector.InstallerConnector`
    exposing the async handler. Mirrors
    :class:`~meho_backplane.connectors.sddc_manager.typed_ops.SddcTypedOp`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Curated ``when_to_use`` blurb per typed-op group.
INSTALLER_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_BRINGUP: (
        "Use to run or track a VCF management-domain bring-up on the Installer "
        "appliance via short submit/poll primitives: installer.sddc.spec.validate "
        "submits an SddcSpec dry-run and installer.sddc.validation.status polls "
        "its verdict; installer.sddc.bringup.start (dangerous, approval-gated) "
        "starts the bring-up and installer.sddc.bringup.status polls the task's "
        "lifecycle state (IN_PROGRESS / COMPLETED_WITH_SUCCESS / "
        "COMPLETED_WITH_FAILURE) and per-stage sub-tasks; on "
        "COMPLETED_WITH_FAILURE, installer.sddc.bringup.retry (dangerous, "
        "approval-gated) resumes the task from its failed sub-task. The "
        "one-shot governed validate-then-deploy unit is "
        "installer.composite.sddc.bringup."
    ),
    _GROUP_DEPOT: (
        "Use to make an air-gapped/offline Installer ready to deploy — the "
        "mandatory pre-bring-up depot lifecycle, in order: "
        "installer.system.depot.get reads the current depot settings and "
        "connection status; installer.system.depot.set (approval-gated) "
        "configures the release depot and runs the vendor connection check "
        "synchronously — on a VMWARE_DEPOT_CONNECT_FAILURE for an HTTPS "
        "depot, first import the depot's TLS certificate via "
        "installer.system.trusted-certificates.add (approval-gated; "
        "installer.system.trusted-certificates.list reads the trust store) "
        "and retry; after a successful connect the catalog ingests and "
        "installer.bundles.list populates within about a minute; then "
        "installer.bundles.download (approval-gated, one batch) explicitly "
        "downloads the release bundles BEFORE validating a spec — a freshly "
        "connected installer pins components to the newest catalog versions "
        "and hard-fails Versions-and-Bundles when those binaries are absent "
        "from the depot, so the explicit GA download is a required step, not "
        "an optimization. Poll installer.bundles.list downloadStatus until "
        "the set is SUCCESSFUL."
    ),
}


def _instructions(
    *,
    when_to_use: str,
    output_shape: str,
    parameter_hints: dict[str, str],
    result_scalars: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    instructions: dict[str, Any] = {
        "when_to_use": when_to_use,
        "output_shape": output_shape,
        "parameter_hints": parameter_hints,
    }
    if result_scalars is not None:
        # #3084: the JSONFlux reduction hint — when the vendor object's
        # set-shaped field (validationChecks[] / sddcSubTasks[]) rides the
        # reduction, the listed scalar siblings survive on the inline
        # summary so the submit → poll loop stays drivable through the
        # governed surface (the reducer's ``_preserved_scalars`` contract).
        instructions["result_scalars"] = {"keys": list(result_scalars)}
    return instructions


#: ``Validation`` scalars that must survive a ``validationChecks[]``
#: reduction (#3084): ``id`` is the poll key for
#: ``installer.sddc.validation.status``; ``executionStatus`` /
#: ``resultStatus`` carry the lifecycle + verdict; ``description`` names the
#: run. Field names pinned by the vendored ``vcf-installer-9.1`` OpenAPI
#: (``components.schemas.Validation``).
_VALIDATION_RESULT_SCALARS: tuple[str, ...] = (
    "id",
    "description",
    "executionStatus",
    "resultStatus",
)

#: ``SddcTask`` scalars that must survive an ``sddcSubTasks[]`` /
#: ``milestones[]`` reduction (#3084): ``id`` is the poll key for
#: ``installer.sddc.bringup.status``; ``status`` carries the lifecycle
#: state; the rest identify the bring-up. Field names pinned by the
#: vendored ``vcf-installer-9.1`` OpenAPI (``components.schemas.SddcTask``).
_SDDC_TASK_RESULT_SCALARS: tuple[str, ...] = (
    "id",
    "name",
    "status",
    "deploymentType",
    "vcfInstanceName",
    "creationTimestamp",
)


#: The shared ``spec`` parameter hint for the submit primitives — carrying the
#: 9.1 SddcSpec shape rules that are undocumented upstream and were each paid
#: for with a live bring-up iteration (#3121 follow-up; c3-class program,
#: 2026-08-25). Field names pinned by the vendored ``vcf-installer-9.1``
#: OpenAPI; the FLEET_LCM consequence observed live in the SDDC Manager
#: inventory translation.
_SDDC_SPEC_HINT = (
    "The full SddcSpec object, POSTed verbatim. 9.1 shape rules proven live: "
    "(1) vspClusterSpec needs THREE unique FQDNs — platformFqdn, "
    "instanceFqdn AND fleetFqdn — each resolving (A+PTR) to its OWN address "
    "OUTSIDE vspClusterSpec.ipv4Pool; omitting fleetFqdn PASSES validation "
    "but fails bring-up later at 'Save VCF Management Components' "
    "(FLEET_LCM fqdn=null -> INVENTORY_INTERNAL_SERVER_ERROR 500), while "
    "reusing vcfOperationsFleetManagementSpec.hostname as fleetFqdn crashes "
    "the Network Configuration validation with IllegalStateException "
    "'Duplicate key'. (2) The five core passwords (sddcManagerSpec "
    "root/ssh/localUser, vcenterSpec rootVcenter/adminUserSso) are limited "
    "to 20 characters. (3) vidbSpec's FQDN defaults to "
    "vcf-identity.<dnsSpec.subdomain> — pre-create that DNS record or set "
    "it explicitly. (4) All-flash/nested labs pass the vSAN check via "
    "datastoreSpec.vsanSpec.esaConfig.enabled=false. (5) The ipv4Pool "
    "ipRange needs >=12 free addresses for a small deployment."
)


def _sddc_spec_parameter_schema(posted_to: str) -> dict[str, Any]:
    """The shared ``{"spec": <SddcSpec>}`` parameter schema of the two submits."""
    return {
        "type": "object",
        "properties": {
            "spec": {
                "type": "object",
                "description": (
                    "The full VCF Installer SddcSpec body (as generated by the "
                    f"deploy-automation factory from a resolved EnvSpec); POSTed "
                    f"verbatim to {posted_to}."
                ),
                "additionalProperties": True,
            },
        },
        "required": ["spec"],
        "additionalProperties": False,
    }


_SPEC_VALIDATE = InstallerTypedOp(
    op_id=INSTALLER_SPEC_VALIDATE_OP_ID,
    handler_attr="sddc_spec_validate",
    summary="Submit an SddcSpec for a non-mutating validation dry-run.",
    description=(
        "Submits the SddcSpec to POST /v1/sddcs/validations — the Installer's "
        "non-mutating dry-run — and returns the vendor Validation object the "
        "moment the submit is accepted (typically executionStatus=IN_PROGRESS "
        "with the id to poll). Poll installer.sddc.validation.status to the "
        "terminal executionStatus and read the resultStatus verdict "
        "(SUCCEEDED / WARNING / FAILED) plus per-check detail. Writes to the "
        "installer appliance but mutates no estate. safety_level=caution."
    ),
    parameter_schema=_sddc_spec_parameter_schema("/v1/sddcs/validations"),
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_BRINGUP,
    tags=("vcf", "installer", "bringup", "validation", "write"),
    safety_level="caution",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call with a full SddcSpec to dry-run-validate it on the Installer "
            "before any bring-up is started. Nothing is deployed."
        ),
        output_shape=(
            "{id, executionStatus, resultStatus, validationChecks: [...]}. "
            "Keep the id and poll installer.sddc.validation.status until "
            "executionStatus is terminal. When validationChecks reduces to "
            "a JSONFlux handle, id / executionStatus / resultStatus stay "
            "top-level on the result."
        ),
        parameter_hints={"spec": _SDDC_SPEC_HINT},
        result_scalars=_VALIDATION_RESULT_SCALARS,
    ),
)

_VALIDATION_STATUS = InstallerTypedOp(
    op_id="installer.sddc.validation.status",
    handler_attr="sddc_validation_status",
    summary="Status of one SddcSpec validation dry-run.",
    description=(
        "Reads one spec validation via GET /v1/sddcs/validations/{id} — the "
        "Validation object whose executionStatus carries the IN_PROGRESS / "
        "COMPLETED lifecycle, resultStatus the SUCCEEDED / WARNING / FAILED "
        "verdict, and validationChecks[] the per-check detail. Requires the "
        "validation id returned by installer.sddc.spec.validate. Works with "
        "zero catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "The validation id returned by installer.sddc.spec.validate.",
            },
        },
        "required": ["id"],
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_BRINGUP,
    tags=("read-only", "vcf", "installer", "bringup", "validation", "status"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call with a validation id to check whether an SddcSpec dry-run "
            "has finished and whether it passed."
        ),
        output_shape=(
            "{id, executionStatus, resultStatus, validationChecks: [...]}. "
            "Terminal when executionStatus leaves IN_PROGRESS; surface any "
            "FAILED check. When validationChecks reduces to a JSONFlux "
            "handle, id / executionStatus / resultStatus stay top-level on "
            "the result."
        ),
        parameter_hints={"id": "The validation id from installer.sddc.spec.validate."},
        result_scalars=_VALIDATION_RESULT_SCALARS,
    ),
)

_BRINGUP_START = InstallerTypedOp(
    op_id=INSTALLER_BRINGUP_START_OP_ID,
    handler_attr="sddc_bringup_start",
    summary="Start a VCF management-domain bring-up from an SddcSpec.",
    description=(
        "Submits the SddcSpec to POST /v1/sddcs — the estate mutation — and "
        "returns the vendor SddcTask the moment the deploy is accepted "
        "(typically within seconds). The bring-up itself runs for hours on "
        "the appliance; poll installer.sddc.bringup.status with the returned "
        "id to a terminal state. dangerous + requires_approval — the dispatch "
        "parks for approval first and the approver sees an SDDC "
        "identity/network preview, never passwords. Run "
        "installer.sddc.spec.validate to a SUCCEEDED/WARNING verdict first — "
        "this primitive submits the spec as-is."
    ),
    parameter_schema=_sddc_spec_parameter_schema("/v1/sddcs"),
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_BRINGUP,
    tags=("vcf", "installer", "bringup", "deploy", "dangerous"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_use=(
            "Call with a validated SddcSpec to start the management-domain "
            "bring-up. Parks for human approval before anything runs."
        ),
        output_shape=(
            "{id, name, status, vcfInstanceName, ...} (SddcTask). Keep the id "
            "and poll installer.sddc.bringup.status to terminal. When the "
            "task's sub-task list reduces to a JSONFlux handle, id / status "
            "stay top-level on the result."
        ),
        parameter_hints={"spec": _SDDC_SPEC_HINT},
        result_scalars=_SDDC_TASK_RESULT_SCALARS,
    ),
)

_BRINGUP_RETRY = InstallerTypedOp(
    op_id=INSTALLER_BRINGUP_RETRY_OP_ID,
    handler_attr="sddc_bringup_retry",
    summary="Retry a failed VCF management-domain bring-up from its failed sub-task.",
    description=(
        "Resumes a COMPLETED_WITH_FAILURE bring-up via PATCH /v1/sddcs/{id} — "
        "the vendor retrySddc operation — and returns the same SddcTask the "
        "moment the retry is accepted. The task re-runs from its failed "
        "sub-task, not from scratch; poll installer.sddc.bringup.status with "
        "the same id to a terminal state. The spec body is optional: omit it "
        "to retry the stored SddcSpec as-is (the common transient-failure "
        "case), or pass a corrected spec for the vendor's edit-and-retry "
        "flow. dangerous + requires_approval — the dispatch parks first and "
        "the approver sees the task id (plus, for edit-and-retry, the SDDC "
        "identity/network preview), never passwords. Triage the failure via "
        "installer.sddc.bringup.status (failed sub-tasks carry errors[]) "
        "before retrying."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The bring-up (SDDC) task id whose status is COMPLETED_WITH_FAILURE."
                ),
            },
            "spec": {
                "type": "object",
                "description": (
                    "Optional corrected SddcSpec for the vendor edit-and-retry "
                    "flow; omitted, the appliance retries the stored spec "
                    "as-is."
                ),
                "additionalProperties": True,
            },
        },
        "required": ["id"],
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_BRINGUP,
    tags=("vcf", "installer", "bringup", "retry", "dangerous"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_use=(
            "Call with a failed bring-up task id to resume the deployment "
            "from its failed sub-task. Parks for human approval before "
            "anything runs. Read installer.sddc.bringup.status first to see "
            "which sub-task failed and why."
        ),
        output_shape=(
            "{id, name, status, vcfInstanceName, ...} (SddcTask). The id is "
            "unchanged — keep polling installer.sddc.bringup.status with it "
            "to terminal. When the task's sub-task list reduces to a JSONFlux "
            "handle, id / status stay top-level on the result."
        ),
        parameter_hints={
            "id": "The failed bring-up (SDDC) task id from the deploy.",
            "spec": (
                "Optional corrected SddcSpec (edit-and-retry); usually omitted. "
                "Same 9.1 shape rules as installer.sddc.spec.validate's spec hint."
            ),
        },
        result_scalars=_SDDC_TASK_RESULT_SCALARS,
    ),
)

_BRINGUP_STATUS = InstallerTypedOp(
    op_id="installer.sddc.bringup.status",
    handler_attr="sddc_bringup_status",
    summary="Lifecycle status of one VCF bring-up task.",
    description=(
        "Reads one VCF management-domain bring-up task via GET /v1/sddcs/{id} — "
        "the SddcTask object whose top-level status carries the "
        "IN_PROGRESS / COMPLETED_WITH_SUCCESS / COMPLETED_WITH_FAILURE lifecycle "
        "state, plus sddcSubTasks[] and milestones[] for per-stage progress. "
        "Requires the bring-up id returned by installer.sddc.bringup.start (or "
        "the governed composite). The poll an operator (or the automation "
        "add-on) runs while a bring-up is in flight or to triage a failed one. "
        "Works with zero catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "The bring-up (SDDC) task id returned by the deploy.",
            },
        },
        "required": ["id"],
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_BRINGUP,
    tags=("read-only", "vcf", "installer", "bringup", "status"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call with a bring-up id to check whether a VCF management-domain "
            "deployment has completed, is still running, or failed."
        ),
        output_shape=(
            "{id, name, status, vcfInstanceName, sddcSubTasks: [...], "
            "milestones: [...]}. Surface the top-level status and any failed "
            "sub-task. When the task's sub-task list reduces to a JSONFlux "
            "handle, id / status stay top-level on the result."
        ),
        parameter_hints={"id": "The bring-up (SDDC) task id from the deploy."},
        result_scalars=_SDDC_TASK_RESULT_SCALARS,
    ),
)


_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_DEPOT_GET = InstallerTypedOp(
    op_id="installer.system.depot.get",
    handler_attr="system_depot_get",
    summary="Read the Installer's release-depot settings and connection status.",
    description=(
        "Reads GET /v1/system/settings/depot — the DepotSettings object "
        "(vmwareAccount / offlineAccount / depotConfiguration with "
        "isOfflineDepot, hostname, port). Every password key is scrubbed at "
        "every depth before the result crosses the governed surface; the "
        "account status/message fields (e.g. DEPOT_CONNECTION_SUCCESSFUL) "
        "survive, so this is the read that tells you whether a depot is "
        "connected. safety_level=safe, read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_DEPOT,
    tags=("read-only", "vcf", "installer", "depot", "system"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to check whether (and which) release depot the Installer "
            "is connected to before configuring one or triggering downloads."
        ),
        output_shape=(
            "{vmwareAccount?, offlineAccount?, depotConfiguration: "
            "{isOfflineDepot, hostname, port, url}}. Account status carries "
            "the connection verdict (e.g. DEPOT_CONNECTION_SUCCESSFUL); "
            "password keys are scrubbed and never present."
        ),
        parameter_hints={},
    ),
)

_DEPOT_SET = InstallerTypedOp(
    op_id=INSTALLER_DEPOT_SET_OP_ID,
    handler_attr="system_depot_set",
    summary="Configure the Installer's release depot (vendor connection check inline).",
    description=(
        "Submits the DepotSettings object to PUT /v1/system/settings/depot. "
        "The appliance runs its depot connection check synchronously, so the "
        "result is the verdict: status='ok' with the scrubbed settings echo "
        "on success, or the structured status='depot_error' (http_status + "
        "vendor error_code + message) on rejection — the common "
        "VMWARE_DEPOT_CONNECT_FAILURE TLS-trust case carries its remediation "
        "inline (import the depot certificate via "
        "installer.system.trusted-certificates.add first). caution + "
        "requires_approval — appliance-scoped configuration that gates what "
        "any later bring-up may install; the approval preview echoes "
        "hostname/port/mode and username only, never a password."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "settings": {
                "type": "object",
                "description": (
                    "The vendor DepotSettings object, PUT verbatim: "
                    "depotConfiguration {isOfflineDepot, hostname, port} plus "
                    "offlineAccount {username, password} for offline depots "
                    "(or vmwareAccount for the online Broadcom depot)."
                ),
                "additionalProperties": True,
            },
        },
        "required": ["settings"],
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_DEPOT,
    tags=("vcf", "installer", "depot", "system", "write"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to point the Installer at a release depot (offline/air-gapped "
            "or the online Broadcom depot). Parks for approval. On depot_error "
            "with VMWARE_DEPOT_CONNECT_FAILURE, follow the inline remediation."
        ),
        output_shape=(
            "{status: 'ok', settings: {...scrubbed echo...}} or {status: "
            "'depot_error', http_status, error_code, message, remediation?}."
        ),
        parameter_hints={
            "settings": (
                "DepotSettings verbatim. Offline example shape: "
                "{offlineAccount: {username, password}, depotConfiguration: "
                "{isOfflineDepot: true, hostname, port}}."
            ),
        },
    ),
)

_TRUSTED_CERTIFICATES_LIST = InstallerTypedOp(
    op_id="installer.system.trusted-certificates.list",
    handler_attr="system_trusted_certificates_list",
    summary="List the Installer's outbound trusted certificates.",
    description=(
        "Reads GET /v1/sddc-manager/trusted-certificates — the appliance "
        "outbound trust store (public certificate material). The read half "
        "of the depot TLS-trust flow. safety_level=safe, read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_DEPOT,
    tags=("read-only", "vcf", "installer", "certificates", "system"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to see which outbound TLS certificates the Installer "
            "already trusts (e.g. before/after adding a depot certificate)."
        ),
        output_shape="Vendor page of trusted certificates (public material).",
        parameter_hints={},
    ),
)

_TRUSTED_CERTIFICATE_ADD = InstallerTypedOp(
    op_id=INSTALLER_TRUSTED_CERTIFICATE_ADD_OP_ID,
    handler_attr="system_trusted_certificate_add",
    summary="Trust an outbound TLS certificate on the Installer (depot TLS trust).",
    description=(
        "POSTs {certificate, certificateUsageType} to "
        "/v1/sddc-manager/trusted-certificates. The fix for "
        "VMWARE_DEPOT_CONNECT_FAILURE against an HTTPS depot with a "
        "self-signed/private-CA certificate: import the depot's certificate "
        "(usage TRUSTED_FOR_OUTBOUND, the default), then re-run "
        "installer.system.depot.set. Certificates are public material — the "
        "PEM rides the params verbatim and the preview echoes a fingerprint. "
        "caution + requires_approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "certificate": {
                "type": "string",
                "minLength": 1,
                "description": "The PEM-encoded certificate to trust.",
            },
            "certificate_usage_type": {
                "type": "string",
                "description": (
                    "Vendor certificateUsageType; defaults to "
                    "TRUSTED_FOR_OUTBOUND (the depot-trust case)."
                ),
            },
        },
        "required": ["certificate"],
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_DEPOT,
    tags=("vcf", "installer", "certificates", "system", "write"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_use=(
            "Call when installer.system.depot.set returns depot_error with "
            "VMWARE_DEPOT_CONNECT_FAILURE for an HTTPS depot: trust the "
            "depot's certificate, then retry depot.set."
        ),
        output_shape="{status: 'ok', result: <vendor echo>}.",
        parameter_hints={
            "certificate": "PEM string of the depot/CA certificate.",
            "certificate_usage_type": "Omit for TRUSTED_FOR_OUTBOUND.",
        },
    ),
)

_BUNDLES_LIST = InstallerTypedOp(
    op_id="installer.bundles.list",
    handler_attr="bundles_list",
    summary="List the Installer's bundle inventory (download states).",
    description=(
        "Reads GET /v1/bundles — the PageOfBundle inventory the catalog "
        "ingest populates after a successful depot connect (typically within "
        "about a minute; empty before any connect). Each element carries "
        "id / version / downloadStatus / components — the poll target after "
        "installer.bundles.download. safety_level=safe, read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_DEPOT,
    tags=("read-only", "vcf", "installer", "bundles", "depot"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call after a depot connect to enumerate available bundles and "
            "their download states, and to poll downloads to SUCCESSFUL "
            "after installer.bundles.download."
        ),
        output_shape=(
            "{elements: [{id, version, downloadStatus, components, ...}], "
            "pageMetadata}. A 9.1 release depot advertises ~180 bundles — "
            "filter by downloadStatus/version instead of dumping elements."
        ),
        parameter_hints={},
    ),
)

_BUNDLES_DOWNLOAD = InstallerTypedOp(
    op_id=INSTALLER_BUNDLES_DOWNLOAD_OP_ID,
    handler_attr="bundles_download",
    summary="Trigger release-bundle downloads on the Installer (one approved batch).",
    description=(
        "PATCHes {bundleDownloadSpec: {downloadNow: true}} to "
        "/v1/bundles/{id} for every id in bundle_ids — the mandatory "
        "pre-download step of an air-gapped bring-up: a freshly connected "
        "installer pins components to the newest catalog versions and "
        "hard-fails the Versions-and-Bundles validation when those binaries "
        "are absent, so the release bundles must be explicitly downloaded "
        "BEFORE spec validation. Per-bundle vendor rejections are collected "
        "(status 'partial'), not fatal; re-triggering an in-flight bundle is "
        "harmless. Poll installer.bundles.list to SUCCESSFUL afterwards. "
        "caution + requires_approval — one approval covers the batch; the "
        "preview echoes the bundle count and ids."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "bundle_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "description": "Bundle ids (from installer.bundles.list) to download.",
            },
        },
        "required": ["bundle_ids"],
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_DEPOT,
    tags=("vcf", "installer", "bundles", "depot", "write"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_use=(
            "Call after the bundle inventory populates to download the "
            "release (GA) bundles before validating an SddcSpec. One "
            "approval covers the whole batch."
        ),
        output_shape=(
            "{status: 'ok'|'partial', accepted, failed, results: [{id, "
            "status: 'accepted'|'failed', task_id?, error_code?, "
            "message?}]}. Then poll installer.bundles.list."
        ),
        parameter_hints={
            "bundle_ids": (
                "Ids from installer.bundles.list (e.g. every bundle of the target release)."
            ),
        },
    ),
)


#: The typed ops :class:`InstallerConnector` registers at lifespan startup,
#: in submit → poll order per primitive pair (the bring-up trio adds the
#: failed-task retry between its submit and its poll; the depot family runs
#: in its air-gapped lifecycle order: settings read/write → trust store
#: read/write → bundle inventory read/download).
INSTALLER_TYPED_OPS: tuple[InstallerTypedOp, ...] = (
    _SPEC_VALIDATE,
    _VALIDATION_STATUS,
    _BRINGUP_START,
    _BRINGUP_RETRY,
    _BRINGUP_STATUS,
    _DEPOT_GET,
    _DEPOT_SET,
    _TRUSTED_CERTIFICATES_LIST,
    _TRUSTED_CERTIFICATE_ADD,
    _BUNDLES_LIST,
    _BUNDLES_DOWNLOAD,
)


async def register_installer_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`INSTALLER_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``. Mirrors
    :func:`~meho_backplane.connectors.sddc_manager.typed_ops.register_sddc_typed_operations`.
    """
    from meho_backplane.connectors.vcf_installer.connector import InstallerConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in INSTALLER_TYPED_OPS:
        handler = getattr(InstallerConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"InstallerConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else INSTALLER_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"InstallerConnector typed op {op.op_id!r} declares "
                f"group_key={op.group_key!r} but no curated when_to_use exists for that key."
            )
        await register_typed_operation(
            product=InstallerConnector.product,
            version=InstallerConnector.version,
            impl_id=InstallerConnector.impl_id,
            op_id=op.op_id,
            handler=handler,
            summary=op.summary,
            description=op.description,
            parameter_schema=op.parameter_schema,
            response_schema=op.response_schema,
            group_key=op.group_key,
            when_to_use=when_to_use,
            tags=list(op.tags),
            safety_level=op.safety_level,
            requires_approval=op.requires_approval,
            llm_instructions=op.llm_instructions,
            embedding_service=embedding_service,
        )
    _log.info(
        "installer_typed_operations_registered",
        count=len(INSTALLER_TYPED_OPS),
        product=InstallerConnector.product,
        version=InstallerConnector.version,
        impl_id=InstallerConnector.impl_id,
    )
