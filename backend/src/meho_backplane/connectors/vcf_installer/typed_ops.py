# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`InstallerConnector`.

The Installer's governed bring-up surface ships as six submit/poll/list typed
primitives (``source_kind="typed"``, #3078; retry #3123; list #3418) so they dispatch on
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
* ``installer.sddc.list`` — list every bring-up task with its id and status,
  no id needed (``GET /v1/sddcs``; ``safe``; #3418).

Plus the **air-gapped depot lifecycle** family (#3121, group
``installer-depot``): ``installer.system.depot.get`` / ``.set`` (read /
configure the release depot, PUT running the vendor connection check
synchronously), ``installer.system.depot.sync-status`` (the catalog
ingest that runs after a connect, #3406),
``installer.system.trusted-certificates.list`` / ``.add``
(outbound trust store — the depot TLS-trust fix), and
``installer.bundles.list`` / ``.download`` (bundle inventory + the mandatory
explicit GA pre-download), plus ``installer.bundles.download-status`` (the
per-bundle failure message and progress, #3410). Reads are ``safe``; the
three writes are ``caution`` + ``requires_approval``.

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

from meho_backplane.connectors.vcf_installer._response_schemas import (
    BUNDLES_DOWNLOAD_RESPONSE,
    BUNDLES_DOWNLOAD_STATUS_RESPONSE,
    BUNDLES_LIST_RESPONSE,
    DEPOT_GET_RESPONSE,
    DEPOT_SET_RESPONSE,
    DEPOT_SYNC_STATUS_RESPONSE,
    SDDC_LIST_RESPONSE,
    SDDC_TASK_RESPONSE,
    TRUSTED_CERTIFICATE_ADD_RESPONSE,
    TRUSTED_CERTIFICATES_LIST_RESPONSE,
    VALIDATION_RESPONSE,
)
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
    "SDDC_SPEC_EXAMPLE",
    "SDDC_SPEC_HINT",
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
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
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
        "ROLLBACK_SUCCESS / COMPLETED_WITH_FAILURE) and per-stage sub-tasks; "
        "installer.sddc.list "
        "lists every bring-up task with its id and status and needs no id, so "
        "use it to find a running bring-up whose id you do not have; on "
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
        "and retry; after a successful connect the appliance ingests the "
        "release catalog in the background, so poll "
        "installer.system.depot.sync-status until the sync has finished "
        "(errorMessage empty and lastSyncCompletionTimestamp later than the "
        "depot.set call; a non-empty errorMessage means the sync failed) "
        "instead of polling installer.bundles.list blindly; then "
        "installer.bundles.list lists the ingested bundles; then "
        "installer.bundles.download (approval-gated, one batch) explicitly "
        "downloads the release bundles BEFORE validating a spec — a freshly "
        "connected installer pins components to the newest catalog versions "
        "and hard-fails Versions-and-Bundles when those binaries are absent "
        "from the depot, so the explicit GA download is a required step, not "
        "an optimization. Poll installer.bundles.list downloadStatus until "
        "the set is SUCCESSFUL; installer.bundles.download-status reads the "
        "per-bundle failure message, isDownloadable flag and downloaded bytes, "
        "so a FAILED bundle can be told apart as retry-worthy or not "
        "downloadable from this depot."
    ),
}


def _instructions(
    *,
    when_to_call: str,
    output_shape: str,
    next_step: str,
    parameter_hints: dict[str, str],
    preconditions: str | None = None,
    result_scalars: tuple[str, ...] | None = None,
    result_digest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    instructions: dict[str, Any] = {
        "when_to_call": when_to_call,
        "output_shape": output_shape,
        "next_step": next_step,
        "parameter_hints": parameter_hints,
    }
    if preconditions is not None:
        instructions["preconditions"] = preconditions
    if result_scalars is not None:
        # #3084: the JSONFlux reduction hint — when the vendor object's
        # set-shaped field (validationChecks[] / sddcSubTasks[]) rides the
        # reduction, the listed scalar siblings survive on the inline
        # summary so the submit → poll loop stays drivable through the
        # governed surface (the reducer's ``_preserved_scalars`` contract).
        instructions["result_scalars"] = {"keys": list(result_scalars)}
    if result_digest is not None:
        # #3122: the row-digest hint — names the collection to reduce (so a
        # two-list SddcTask reduces sddcSubTasks[] instead of passing through
        # the reducer's multi-list detail-object exemption) and drives the
        # inline per-state counts / IN_PROGRESS names / FAILED-rows-with-errors
        # digest (the reducer's ``_row_digest`` contract).
        instructions["result_digest"] = dict(result_digest)
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

#: ``SddcTask`` row-digest hint (#3122). On a live management-domain bring-up
#: ``sddcSubTasks[]`` runs to ~260 rows AND the vendor ``SddcTask`` carries a
#: populated ``milestones[]`` — two list fields, so the reducer's #2113
#: dict-of-arrays detail-object exemption would pass the whole document
#: through UNREDUCED (tens of KB every 150 s poll). This hint names
#: ``sddcSubTasks`` as THE collection to reduce (so it reduces despite the
#: sibling ``milestones``) and drives the inline digest: per-``status`` counts,
#: the ``name`` of every IN_PROGRESS sub-task, and every FAILED /
#: COMPLETED_WITH_FAILURE sub-task preserved WHOLE (with its ``errors[]``) so
#: the restart-from-failed flow (installer.sddc.bringup.retry) never needs a
#: drill-in. The full ``sddcSubTasks`` array stays behind the result handle for
#: ``result_query`` drill-in. Status field names match the vendored
#: ``vcf-installer-9.1`` OpenAPI (``components.schemas.SddcSubTask.status``).
_SDDC_TASK_RESULT_DIGEST: dict[str, Any] = {
    "collection": "sddcSubTasks",
    "status_field": "status",
    "name_field": "name",
    "active_states": ["IN_PROGRESS"],
    "failed_states": ["FAILED", "COMPLETED_WITH_FAILURE"],
}

#: ``PageOfBundleDownloadStatusInfo`` digest (#3410). A release-wide read runs
#: to ~180 rows and reduces to a handle; this hint adds per-``downloadStatus``
#: counts and the ``bundleId`` of every IN_PROGRESS row inline. FAILED rows are
#: deliberately NOT kept whole inline (a depot outage fails every row, which
#: would put the whole set back inline); callers read them with a ``bundle_id``
#: call or a ``result_query`` on the handle. Field names match the vendored
#: ``vcf-installer-9.1`` OpenAPI (``components.schemas.BundleDownloadStatusInfo``).
_BUNDLE_DOWNLOAD_STATUS_DIGEST: dict[str, Any] = {
    "collection": "elements",
    "status_field": "downloadStatus",
    "name_field": "bundleId",
    "active_states": ["IN_PROGRESS"],
}


#: The shared ``spec`` parameter hint for the submit primitives — carrying the
#: 9.1 SddcSpec shape rules that are undocumented upstream and were each paid
#: for with a live bring-up iteration (#3121 follow-up; c3-class program,
#: 2026-08-25). Field names pinned by the vendored ``vcf-installer-9.1``
#: OpenAPI; the FLEET_LCM consequence observed live in the SDDC Manager
#: inventory translation.
SDDC_SPEC_HINT = (
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


#: A short placeholder SddcSpec for the examples. A real spec has many more
#: fields; the parameter schema keeps ``spec`` open, so this is a valid call
#: shape. Field names from the pinned ``vcf-installer-9.1`` OpenAPI
#: (``components.schemas.SddcSpec``; ``sddcId``, ``vcenterSpec``,
#: ``networkSpecs`` and ``dnsSpec`` are its required fields).
SDDC_SPEC_EXAMPLE: dict[str, Any] = {
    "sddcId": "<sddc-id>",
    "vcfInstanceName": "<vcf-instance-name>",
    "workflowType": "VCF",
    "vcenterSpec": {"vcenterHostname": "<vcenter-fqdn>"},
    "networkSpecs": [
        {"networkType": "MANAGEMENT", "vlanId": "<vlan-id>", "subnet": "<subnet-cidr>"}
    ],
    "dnsSpec": {"subdomain": "<dns-subdomain>", "nameservers": ["<dns-server-ip>"]},
    "hostSpecs": [{"hostname": "<esx-host-fqdn>"}],
}


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
        "examples": [{"spec": SDDC_SPEC_EXAMPLE}],
    }


def _id_parameter_schema(description: str, example: str) -> dict[str, Any]:
    """A ``{"id": <string>}`` parameter schema with one example call."""
    return {
        "type": "object",
        "properties": {"id": {"type": "string", "minLength": 1, "description": description}},
        "required": ["id"],
        "additionalProperties": False,
        "examples": [{"id": example}],
    }


_SPEC_VALIDATE = InstallerTypedOp(
    op_id=INSTALLER_SPEC_VALIDATE_OP_ID,
    handler_attr="sddc_spec_validate",
    summary="Check an SddcSpec on the Installer before a bring-up (deploys nothing).",
    description=(
        "Sends the SddcSpec to POST /v1/sddcs/validations. The Installer checks the "
        "spec and deploys nothing. Returns the validation run at once, usually "
        "with executionStatus IN_PROGRESS; follow it with "
        "installer.sddc.validation.status."
    ),
    parameter_schema=_sddc_spec_parameter_schema("/v1/sddcs/validations"),
    response_schema=VALIDATION_RESPONSE,
    group_key=_GROUP_BRINGUP,
    tags=("vcf", "installer", "bringup", "validation", "write"),
    safety_level="caution",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call with a full SddcSpec to check it before any bring-up starts. Nothing is deployed."
        ),
        output_shape=(
            "{id, description, executionStatus, resultStatus, validationChecks: "
            "[...]}. When validationChecks comes back as a result handle, id, "
            "executionStatus and resultStatus stay top-level."
        ),
        next_step=(
            "Keep the id. Call installer.sddc.validation.status with it until "
            "executionStatus is no longer IN_PROGRESS."
        ),
        preconditions=(
            "The depot is set and the release bundles are downloaded "
            "(installer.bundles.list shows them SUCCESSFUL). The call runs at once, "
            "without approval; it changes no system outside the Installer."
        ),
        parameter_hints={"spec": SDDC_SPEC_HINT},
        result_scalars=_VALIDATION_RESULT_SCALARS,
    ),
)

_VALIDATION_STATUS = InstallerTypedOp(
    op_id="installer.sddc.validation.status",
    handler_attr="sddc_validation_status",
    summary="Read one SddcSpec validation run and its verdict.",
    description=(
        "Reads GET /v1/sddcs/validations/{id}: the validation run with "
        "executionStatus (where the run is), resultStatus (SUCCEEDED, WARNING or "
        "FAILED) and validationChecks[] (each check). Needs the id from "
        "installer.sddc.spec.validate. Read-only."
    ),
    parameter_schema=_id_parameter_schema(
        "The validation id returned by installer.sddc.spec.validate.", "<validation-id>"
    ),
    response_schema=VALIDATION_RESPONSE,
    group_key=_GROUP_BRINGUP,
    tags=("read-only", "vcf", "installer", "bringup", "validation", "status"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call with a validation id to see whether an SddcSpec check has "
            "finished and whether it passed."
        ),
        output_shape=(
            "{id, description, executionStatus, resultStatus, validationChecks: "
            "[...]}. When validationChecks comes back as a result handle, id, "
            "executionStatus and resultStatus stay top-level."
        ),
        next_step=(
            "If executionStatus is IN_PROGRESS, call again later. If resultStatus is "
            "SUCCEEDED or WARNING, go on with installer.sddc.bringup.start. If it is "
            "FAILED, report the failed checks and fix the spec."
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
        "Sends the SddcSpec to POST /v1/sddcs and starts the bring-up. A person "
        "must approve first; the approver sees the SDDC names and networks, never "
        "passwords. Returns the bring-up task at once. The bring-up runs for "
        "hours."
    ),
    parameter_schema=_sddc_spec_parameter_schema("/v1/sddcs"),
    response_schema=SDDC_TASK_RESPONSE,
    group_key=_GROUP_BRINGUP,
    tags=("vcf", "installer", "bringup", "deploy", "dangerous"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_call=("Call with a checked SddcSpec to start the management-domain bring-up."),
        output_shape=(
            "{id, name, status, vcfInstanceName, ...} (SddcTask). When the "
            "sub-task list comes back as a result handle, id and status stay "
            "top-level, with a status_counts / in_progress / failed digest of the "
            "sub-tasks (see installer.sddc.bringup.status)."
        ),
        next_step=(
            "Keep the id. Call installer.sddc.bringup.status with it until status is "
            "COMPLETED_WITH_SUCCESS, COMPLETED_WITH_FAILURE or ROLLBACK_SUCCESS. "
            "ROLLBACK_SUCCESS means the bring-up failed and the Installer undid "
            "what it had built; the task is finished, so stop polling."
        ),
        preconditions=(
            "Run installer.sddc.spec.validate with the same spec first, and go on "
            "only when its resultStatus is SUCCEEDED or WARNING; this call sends the "
            "spec as it is. Check with installer.sddc.list that no bring-up is "
            "running. A person must approve the call before it runs."
        ),
        parameter_hints={"spec": SDDC_SPEC_HINT},
        result_scalars=_SDDC_TASK_RESULT_SCALARS,
        result_digest=_SDDC_TASK_RESULT_DIGEST,
    ),
)

_BRINGUP_RETRY = InstallerTypedOp(
    op_id=INSTALLER_BRINGUP_RETRY_OP_ID,
    handler_attr="sddc_bringup_retry",
    summary="Retry a failed VCF management-domain bring-up from its failed sub-task.",
    description=(
        "Sends PATCH /v1/sddcs/{id} (the vendor retrySddc). The bring-up runs again "
        "from its failed sub-task, not from the start. Leave out spec to retry the "
        "stored spec, or pass a fixed spec. A person must approve first. Returns "
        "the same bring-up task."
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
        "examples": [{"id": "<bringup-task-id>"}],
    },
    response_schema=SDDC_TASK_RESPONSE,
    group_key=_GROUP_BRINGUP,
    tags=("vcf", "installer", "bringup", "retry", "dangerous"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_call=(
            "Call with the id of a failed bring-up to run it again from the failed sub-task."
        ),
        output_shape=(
            "{id, name, status, vcfInstanceName, ...} (SddcTask). The id does not "
            "change. When the sub-task list comes back as a result handle, id and "
            "status stay top-level, with a status_counts / in_progress / failed "
            "digest of the sub-tasks."
        ),
        next_step=(
            "Call installer.sddc.bringup.status with the same id until status is "
            "COMPLETED_WITH_SUCCESS, COMPLETED_WITH_FAILURE or ROLLBACK_SUCCESS. "
            "ROLLBACK_SUCCESS means the bring-up failed and the Installer undid "
            "what it had built; the task is finished, so stop polling."
        ),
        preconditions=(
            "installer.sddc.bringup.status shows status COMPLETED_WITH_FAILURE. Read "
            "the errors[] of the failed sub-task first and fix the cause. A person "
            "must approve the call before it runs."
        ),
        parameter_hints={
            "id": "The failed bring-up (SDDC) task id from the deploy.",
            "spec": (
                "Optional corrected SddcSpec (edit-and-retry); usually omitted. "
                "Same 9.1 shape rules as installer.sddc.spec.validate's spec hint."
            ),
        },
        result_scalars=_SDDC_TASK_RESULT_SCALARS,
        result_digest=_SDDC_TASK_RESULT_DIGEST,
    ),
)

_BRINGUP_STATUS = InstallerTypedOp(
    op_id="installer.sddc.bringup.status",
    handler_attr="sddc_bringup_status",
    summary="Read one VCF bring-up task: its status and its steps.",
    description=(
        "Reads GET /v1/sddcs/{id}: the bring-up task with its status "
        "(IN_PROGRESS, COMPLETED_WITH_SUCCESS, ROLLBACK_SUCCESS or "
        "COMPLETED_WITH_FAILURE), sddcSubTasks[] (each step) and milestones[] "
        "(the main stages). Needs the id from the start call or from "
        "installer.sddc.list. Read-only."
    ),
    parameter_schema=_id_parameter_schema(
        "The bring-up (SDDC) task id returned by the deploy.", "<bringup-task-id>"
    ),
    response_schema=SDDC_TASK_RESPONSE,
    group_key=_GROUP_BRINGUP,
    tags=("read-only", "vcf", "installer", "bringup", "status"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call with a bring-up id to see whether a VCF management-domain "
            "deployment has finished, is still running, or failed."
        ),
        output_shape=(
            "{id, name, status, vcfInstanceName, sddcSubTasks: [...], milestones: "
            "[...]}. On a live bring-up the ~260 sddcSubTasks come back as a result "
            "handle. The inline summary then keeps id and status and adds a "
            "digest: status_counts (steps per state), in_progress (names of running "
            "steps) and failed (failed steps WITH their errors[])."
        ),
        next_step=(
            "If status is IN_PROGRESS, call again later. If it is "
            "COMPLETED_WITH_FAILURE, report the failed steps and their errors, fix "
            "the cause, then use installer.sddc.bringup.retry. If it is "
            "ROLLBACK_SUCCESS, the bring-up failed and the Installer undid what it "
            "had built; report the failed steps and stop polling. Read all steps "
            "with result_query on the handle."
        ),
        parameter_hints={"id": "The bring-up (SDDC) task id from the deploy."},
        result_scalars=_SDDC_TASK_RESULT_SCALARS,
        result_digest=_SDDC_TASK_RESULT_DIGEST,
    ),
)


_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

_SDDC_LIST = InstallerTypedOp(
    op_id="installer.sddc.list",
    handler_attr="sddc_list",
    summary="List every VCF bring-up task on the Installer with its id and status.",
    description=(
        "Reads GET /v1/sddcs: every bring-up task the Installer knows, running or "
        "done. Needs no id, so use it to find a lost bring-up id. Each row keeps "
        "only id, name, status, deploymentType, vcfInstanceName and "
        "creationTimestamp. Read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema=SDDC_LIST_RESPONSE,
    group_key=_GROUP_BRINGUP,
    tags=("read-only", "vcf", "installer", "bringup", "status", "list"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to see which VCF bring-ups exist on the Installer and their "
            "state, or to find the id of a running bring-up you no longer have."
        ),
        output_shape=(
            "{elements: [{id, name, status, deploymentType, vcfInstanceName, "
            "creationTimestamp}], pageMetadata?}. status is IN_PROGRESS / "
            "COMPLETED_WITH_SUCCESS / ROLLBACK_SUCCESS / COMPLETED_WITH_FAILURE. "
            "No step detail here. A long list comes back as a result handle."
        ),
        next_step=(
            "Call installer.sddc.bringup.status with the id of the task you want "
            "to follow. Read more rows with result_query on the handle."
        ),
        parameter_hints={},
    ),
)

_DEPOT_GET = InstallerTypedOp(
    op_id="installer.system.depot.get",
    handler_attr="system_depot_get",
    summary="Read the Installer's release-depot settings and connection status.",
    description=(
        "Reads GET /v1/system/settings/depot: the depot settings (vmwareAccount, "
        "offlineAccount, depotConfiguration). Every password field is removed. "
        "The account status (for example DEPOT_CONNECTION_SUCCESSFUL) tells you "
        "whether a depot is connected. Read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema=DEPOT_GET_RESPONSE,
    group_key=_GROUP_DEPOT,
    tags=("read-only", "vcf", "installer", "depot", "system"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to check whether (and which) release depot the Installer is "
            "connected to, before you set one or start downloads."
        ),
        output_shape=(
            "{vmwareAccount?, offlineAccount?, depotConfiguration: {isOfflineDepot, "
            "hostname, port, url}}. The account status is the connection verdict; "
            "password fields are never present."
        ),
        next_step=(
            "If no account shows DEPOT_CONNECTION_SUCCESSFUL, set the depot with "
            "installer.system.depot.set. If one does, go on with "
            "installer.system.depot.sync-status."
        ),
        parameter_hints={},
    ),
)

_DEPOT_SET = InstallerTypedOp(
    op_id=INSTALLER_DEPOT_SET_OP_ID,
    handler_attr="system_depot_set",
    summary="Set the Installer's release depot; the appliance tests the connection.",
    description=(
        "Sends the depot settings to PUT /v1/system/settings/depot. The appliance "
        "tests the connection at once, so the answer is the verdict: status ok, "
        "or status depot_error with the vendor error. A person must approve "
        "first; the approver never sees a password."
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
        "examples": [
            {
                "settings": {
                    "offlineAccount": {
                        "username": "<depot-user>",
                        "password": "<depot-password>",
                    },
                    "depotConfiguration": {
                        "isOfflineDepot": True,
                        "hostname": "depot.example.com",
                        "port": 443,
                    },
                }
            }
        ],
    },
    response_schema=DEPOT_SET_RESPONSE,
    group_key=_GROUP_DEPOT,
    tags=("vcf", "installer", "depot", "system", "write"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to point the Installer at a release depot: an offline "
            "(air-gapped) depot or the online Broadcom depot."
        ),
        output_shape=(
            "{status: 'ok', settings: {...password fields removed...}} or {status: "
            "'depot_error', http_status, error_code, message, remediation?}."
        ),
        next_step=(
            "On ok, call installer.system.depot.sync-status until the catalog sync "
            "has finished. On depot_error with VMWARE_DEPOT_CONNECT_FAILURE for an "
            "HTTPS depot, import its certificate with "
            "installer.system.trusted-certificates.add and call this again."
        ),
        preconditions=(
            "Read the current settings with installer.system.depot.get first. The "
            "depot must be reachable from the Installer. A person must approve the "
            "call before it runs."
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

_DEPOT_SYNC_STATUS = InstallerTypedOp(
    op_id="installer.system.depot.sync-status",
    handler_attr="system_depot_sync_status",
    summary="Read whether the Installer has finished loading the depot catalog.",
    description=(
        "Reads GET /v1/system/settings/depot/depot-sync-info: syncStatus, "
        "errorMessage and lastSyncCompletionTimestamp. After a depot is set, the "
        "appliance loads the release catalog in the background; "
        "installer.bundles.list stays empty until that is done. Read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema=DEPOT_SYNC_STATUS_RESPONSE,
    group_key=_GROUP_DEPOT,
    tags=("read-only", "vcf", "installer", "depot", "system", "sync"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call after installer.system.depot.set succeeds, and again until the "
            "catalog sync is done, before installer.bundles.list or "
            "installer.bundles.download. Also call it when installer.bundles.list "
            "is empty, to tell 'still loading' from 'failed'."
        ),
        output_shape=(
            "{syncStatus, errorMessage?, lastSyncCompletionTimestamp?}. syncStatus "
            "is passed through as the vendor sends it (the 9.1 API does not list "
            "its values)."
        ),
        next_step=(
            "When errorMessage is empty and lastSyncCompletionTimestamp is later "
            "than the depot.set call, call installer.bundles.list. A non-empty "
            "errorMessage means the sync failed: fix the depot instead of waiting."
        ),
        parameter_hints={},
    ),
)

_TRUSTED_CERTIFICATES_LIST = InstallerTypedOp(
    op_id="installer.system.trusted-certificates.list",
    handler_attr="system_trusted_certificates_list",
    summary="List the Installer's outbound trusted certificates.",
    description=(
        "Reads GET /v1/sddc-manager/trusted-certificates: the certificates the "
        "appliance trusts when it connects out, for example to a depot. "
        "Certificates are public. Read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema=TRUSTED_CERTIFICATES_LIST_RESPONSE,
    group_key=_GROUP_DEPOT,
    tags=("read-only", "vcf", "installer", "certificates", "system"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to see which outbound TLS certificates the Installer already "
            "trusts, for example before or after you add a depot certificate."
        ),
        output_shape="{elements: [{alias, certificate}], pageMetadata}.",
        next_step=(
            "If the depot certificate is missing, add it with "
            "installer.system.trusted-certificates.add."
        ),
        parameter_hints={},
    ),
)

_TRUSTED_CERTIFICATE_ADD = InstallerTypedOp(
    op_id=INSTALLER_TRUSTED_CERTIFICATE_ADD_OP_ID,
    handler_attr="system_trusted_certificate_add",
    summary="Trust an outbound TLS certificate on the Installer (depot TLS trust).",
    description=(
        "Sends {certificate, certificateUsageType} to POST "
        "/v1/sddc-manager/trusted-certificates. This fixes "
        "VMWARE_DEPOT_CONNECT_FAILURE for an HTTPS depot with a self-signed or "
        "private-CA certificate. A person must approve first; the approver sees "
        "a fingerprint of the certificate."
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
        "examples": [{"certificate": "<pem-certificate>"}],
    },
    response_schema=TRUSTED_CERTIFICATE_ADD_RESPONSE,
    group_key=_GROUP_DEPOT,
    tags=("vcf", "installer", "certificates", "system", "write"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_call=(
            "Call when installer.system.depot.set returns depot_error with "
            "VMWARE_DEPOT_CONNECT_FAILURE for an HTTPS depot: trust the depot's "
            "certificate."
        ),
        output_shape="{status: 'ok', result: {elements: [{alias, certificate}], ...}}.",
        next_step="Call installer.system.depot.set again with the same settings.",
        preconditions=(
            "Get the PEM certificate of the depot or of its CA from a source you "
            "trust. Check with installer.system.trusted-certificates.list that it "
            "is not there yet. A person must approve the call before it runs."
        ),
        parameter_hints={
            "certificate": "PEM string of the depot/CA certificate.",
            "certificate_usage_type": "Omit for TRUSTED_FOR_OUTBOUND.",
        },
    ),
)

_BUNDLES_LIST = InstallerTypedOp(
    op_id="installer.bundles.list",
    handler_attr="bundles_list",
    summary="List the Installer's bundles and their download status.",
    description=(
        "Reads GET /v1/bundles: the bundles the catalog sync loaded after a depot "
        "was set (empty before that). Each row has id, version, downloadStatus and "
        "components. Use it to follow downloads after installer.bundles.download. "
        "Read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema=BUNDLES_LIST_RESPONSE,
    group_key=_GROUP_DEPOT,
    tags=("read-only", "vcf", "installer", "bundles", "depot"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call after the depot catalog sync to see the bundles and their "
            "download status, and to follow downloads after "
            "installer.bundles.download."
        ),
        output_shape=(
            "{elements: [{id, version, downloadStatus, components, ...}], "
            "pageMetadata}. A 9.1 release depot has about 180 bundles, so the list "
            "comes back as a result handle; filter by downloadStatus or version."
        ),
        next_step=(
            "Download the bundles of the target release with "
            "installer.bundles.download. When a bundle is FAILED, read why with "
            "installer.bundles.download-status."
        ),
        parameter_hints={},
    ),
)

_BUNDLES_DOWNLOAD_STATUS = InstallerTypedOp(
    op_id="installer.bundles.download-status",
    handler_attr="bundles_download_status",
    summary="Per-bundle download detail: status, failure message, progress.",
    description=(
        "Reads GET /v1/bundles/download-status. Each row has bundleId, version, "
        "downloadStatus and the detail installer.bundles.list lacks: message (why "
        "a download failed), downloadedSize, isDownloadable and the times. Filter "
        "by bundle_id, release_version or image_type. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "bundle_id": {
                "type": "string",
                "minLength": 1,
                "description": "Only this bundle (an id from installer.bundles.list).",
            },
            "release_version": {
                "type": "string",
                "minLength": 1,
                "description": "Only bundles of this VCF release, e.g. 9.1.0.0.",
            },
            "image_type": {
                "type": "string",
                "enum": ["INSTALL", "PATCH"],
                "description": "Only INSTALL or only PATCH bundles.",
            },
        },
        "additionalProperties": False,
        "examples": [{"release_version": "<vcf-release-version>", "image_type": "INSTALL"}],
    },
    response_schema=BUNDLES_DOWNLOAD_STATUS_RESPONSE,
    group_key=_GROUP_DEPOT,
    tags=("read-only", "vcf", "installer", "bundles", "depot"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call when installer.bundles.list shows a bundle as FAILED, or a "
            "download seems stuck, to learn why and how far it got."
        ),
        output_shape=(
            "{elements: [{bundleId, componentType, version, downloadStatus, "
            "message?, downloadedSize?, isDownloadable?, downloadId?, "
            "downloadScheduledTime, downloadStartTime?, downloadEndTime?, "
            "isDownloadCancellable}], pageMetadata}. downloadedSize is bytes; "
            "compare it with sizeMB from installer.bundles.list. A release-wide "
            "read comes back as a handle with status_counts and the in_progress "
            "bundle ids inline."
        ),
        next_step=(
            "A FAILED row with isDownloadable=false cannot be downloaded from this "
            "depot now: stop and fix the depot. A FAILED row with "
            "isDownloadable=true and a transfer error in message is worth one more "
            "installer.bundles.download. Read FAILED rows with bundle_id or "
            "result_query."
        ),
        parameter_hints={
            "bundle_id": "Pass one id to read a single bundle's detail.",
            "release_version": "Pass the target release to check its whole set.",
            "image_type": "INSTALL for bring-up bundles; omit for both.",
        },
        result_digest=_BUNDLE_DOWNLOAD_STATUS_DIGEST,
    ),
)

_BUNDLES_DOWNLOAD = InstallerTypedOp(
    op_id=INSTALLER_BUNDLES_DOWNLOAD_OP_ID,
    handler_attr="bundles_download",
    summary="Start release-bundle downloads on the Installer (one approved batch).",
    description=(
        "Sends PATCH /v1/bundles/{id} with downloadNow for every id in bundle_ids. "
        "An offline bring-up needs this before the spec check, or the "
        "Versions-and-Bundles check fails. A person approves the whole batch once. "
        "A refused bundle does not stop the others (status partial)."
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
        "examples": [{"bundle_ids": ["<bundle-id>"]}],
    },
    response_schema=BUNDLES_DOWNLOAD_RESPONSE,
    group_key=_GROUP_DEPOT,
    tags=("vcf", "installer", "bundles", "depot", "write"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_call=(
            "Call after the bundle list fills to download the release (GA) bundles "
            "before you check an SddcSpec. One approval covers the whole batch."
        ),
        output_shape=(
            "{status: 'ok'|'partial', accepted, failed, results: [{id, status: "
            "'accepted'|'failed', task_id?, error_code?, message?}]}."
        ),
        next_step=(
            "Call installer.bundles.list until every bundle of the release is "
            "SUCCESSFUL. For a failed row, read installer.bundles.download-status."
        ),
        preconditions=(
            "The depot catalog sync is done (installer.system.depot.sync-status) "
            "and the ids come from installer.bundles.list. Starting a bundle that "
            "is already downloading does no harm. A person must approve the call "
            "before it runs."
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
#: failed-task retry between its submit and its poll, and the id-less task
#: list after its poll; the depot family runs
#: in its air-gapped lifecycle order: settings read/write → catalog sync
#: read → trust store read/write → bundle inventory read / download-detail
#: read / download).
INSTALLER_TYPED_OPS: tuple[InstallerTypedOp, ...] = (
    _SPEC_VALIDATE,
    _VALIDATION_STATUS,
    _BRINGUP_START,
    _BRINGUP_RETRY,
    _BRINGUP_STATUS,
    _SDDC_LIST,
    _DEPOT_GET,
    _DEPOT_SET,
    _DEPOT_SYNC_STATUS,
    _TRUSTED_CERTIFICATES_LIST,
    _TRUSTED_CERTIFICATE_ADD,
    _BUNDLES_LIST,
    _BUNDLES_DOWNLOAD_STATUS,
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
