# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: declarative typed-op metadata table. This module is a
# data file — 12 audited-read SddcTypedOp records, each a data-only value with
# grounded operator-facing prose (summary / description / llm_instructions) the
# BM25 + embedding search surfaces read verbatim. Cyclomatic complexity is
# trivial; the length is the 12-op catalog, not logic. Splitting the tuple
# across files fragments the single source of truth the registrar iterates.

"""Typed-op metadata + registrar for :class:`SddcManagerConnector` (#2306).

The audited SDDC Manager read set (the #2294 12-read lab-audit surface) is
registered as typed ops (``source_kind="typed"``) so it dispatches on a
fresh boot with **zero catalog ingest** -- the #2247 failure class the
ingested-row curation in
:mod:`meho_backplane.connectors.sddc_manager.core_ops` was subject to
(per-deploy catalog state). The op bodies live in
:mod:`meho_backplane.connectors.sddc_manager.typed_reads`; thin
bound-method shims on
:class:`~meho_backplane.connectors.sddc_manager.connector.SddcManagerConnector`
expose them under the ``handler_attr`` names below so the dispatcher's
:func:`~meho_backplane.operations._handler_resolve.import_handler` walk
recovers each callable from its persisted ``module.ClassName.method`` path.

The dataclass + tuple + module-level registrar shape mirrors
:mod:`meho_backplane.connectors.nsx.typed_ops`.

Two surfaces, no resolver shadowing (#1750/#1798 class)
------------------------------------------------------

The typed curated set here is the **documented operational surface**; the
ingested 375-op VCF catalog stays as profiled-dispatch breadth (#2271)
under its own ``METHOD:path`` op_ids. The two never collide: a typed op
carries a dotted op-id (``sddc.domain.list``) and a code ``handler_ref``,
so it resolves through :func:`~meho_backplane.operations._branches.dispatch_typed`
and never through an ``endpoint_descriptor`` ingested row (#2262). The
converted reads are removed from the ``core_ops.py`` ingested curation;
the four non-audited curated reads (releases/system, domain detail,
network-pools, bundles) stay ingested-curated there.

Credential-read gating (``sddc.credential.list``)
-------------------------------------------------

``GET /v1/credentials`` is SDDC Manager's system-of-record read for
nested-infra credentials. Its typed op is gated as a credential-read
mapped to the **existing** safety_level/policy mechanism:

* ``requires_approval=True`` -- the dispatcher's
  :func:`~meho_backplane.operations._validate.policy_gate` routes a
  ``requires_approval`` op to the approval queue (G11.7-T1 #1401), so it
  is **not dispatchable without the elevated policy path** (operator
  approval).
* op-id ``sddc.credential.list`` on
  :data:`~meho_backplane.broadcast.events._CREDENTIAL_READ_OPS` (decision
  #3's op-id classifier) -- ``classify_op`` returns ``credential_read`` so
  audit + broadcast rows collapse to aggregate-only (redacted).
* the handler additionally scrubs secret-keyed values at the connector
  boundary (:func:`~meho_backplane.connectors.sddc_manager.typed_reads._redact_secrets`).

The write surface (none wanted for SDDC), the session/auth itself
(#2290), and the profiled ingested dispatch (#2271) are out of this
task's scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "SDDC_TYPED_OPS",
    "SDDC_TYPED_WHEN_TO_USE_BY_GROUP",
    "SddcTypedOp",
    "register_sddc_typed_operations",
]

_log = structlog.get_logger(__name__)

# Typed-op group keys. Each groups the audited reads by the operator
# question they answer; the ``when_to_use`` blurbs below are what the
# agent reads verbatim through ``list_operation_groups`` to pick a group.
_GROUP_INVENTORY = "sddc-inventory"
_GROUP_PLATFORM = "sddc-platform"
_GROUP_TASKS = "sddc-tasks-typed"
_GROUP_CREDENTIALS = "sddc-credentials"


@dataclass(frozen=True)
class SddcTypedOp:
    """Metadata for one SDDC Manager typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :func:`register_sddc_typed_operations` can splat the
    dataclass into the helper without per-op boilerplate. ``handler_attr``
    is the attribute name on
    :class:`~meho_backplane.connectors.sddc_manager.connector.SddcManagerConnector`
    exposing the async handler; the registrar resolves the bound method
    against the class at registration time. Mirrors
    :class:`~meho_backplane.connectors.nsx.typed_ops.NsxTypedOp`.
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
#: ``register_typed_operation`` requires a non-empty string whenever
#: ``group_key`` is set (typed_register ``_validate_when_to_use_pairing``).
SDDC_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_INVENTORY: (
        "Use to read the VCF infrastructure inventory SDDC Manager governs: "
        "domains and their lifecycle status (sddc.domain.list / "
        "sddc.domain.status), vSphere clusters (sddc.cluster.list), ESXi "
        "hosts (sddc.host.list), vCenters (sddc.vcenter.list), and NSX-T "
        "clusters (sddc.nsxt_cluster.list). The right group for 'what does "
        "this VCF stack contain' and for mapping a workload domain to its "
        "vCenter and NSX-T cluster. Read-only."
    ),
    _GROUP_PLATFORM: (
        "Use to read the SDDC Manager platform itself: the appliance "
        "inventory (sddc.manager.list), system-level settings "
        "(sddc.system.info), the running VCF micro-services and their "
        "health (sddc.vcf_service.list), and the license keys "
        "(sddc.license.list). The right group when confirming which SDDC "
        "Manager manages the stack, whether its control plane is healthy, "
        "or whether the deployment is licensed. Read-only."
    ),
    _GROUP_TASKS: (
        "Use to read VCF workflow tasks (sddc.task.list) -- in-flight or "
        "recently completed domain-expand, host-commission, or update-apply "
        "operations, optionally filtered by status. The right group when "
        "the question is 'what is running against this VCF stack right now' "
        "or when triaging a workflow that failed. Read-only."
    ),
    _GROUP_CREDENTIALS: (
        "Use to read the nested-infra credential inventory SDDC Manager is "
        "the system of record for (sddc.credential.list) -- the ESXi / "
        "vCenter / NSX-T / backup service accounts it stores and rotates. "
        "Credential-read gated: dispatch requires operator approval and the "
        "secret values are redacted; the read shows which accounts exist, "
        "never their passwords. The group an operator reaches for during a "
        "nested-infra credential audit or outage."
    ),
}


# ---------------------------------------------------------------------------
# Shared parameter-schema fragments
# ---------------------------------------------------------------------------

#: The empty parameter object shared by the no-argument reads.
_NO_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _instructions(
    *,
    when_to_use: str,
    output_shape: str,
    parameter_hints: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the per-op ``llm_instructions`` blob with the canonical keys."""
    blob: dict[str, Any] = {"when_to_use": when_to_use, "output_shape": output_shape}
    if parameter_hints is not None:
        blob["parameter_hints"] = parameter_hints
    return blob


# ---------------------------------------------------------------------------
# sddc.domain.list
# ---------------------------------------------------------------------------

_DOMAIN_LIST = SddcTypedOp(
    op_id="sddc.domain.list",
    handler_attr="domain_list",
    summary="VCF domains (management + workload) SDDC Manager governs.",
    description=(
        "Lists every VCF domain the SDDC Manager governs via "
        "GET /v1/domains -- the management domain plus any workload "
        "domains. The entry point for domain-scoped cluster / host / "
        "vCenter reads and for mapping a workload domain to its vCenter and "
        "NSX-T cluster. Works with zero catalog ingest. safety_level=safe, "
        "read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "domain", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to list all VCF domains -- the management domain and any "
            "workload domains -- as the entry point for domain-scoped reads."
        ),
        output_shape=(
            "{elements: [{id, name, type (MANAGEMENT/WORKLOAD), vcenters, "
            "nsxtCluster}, ...], pageMetadata}."
        ),
    ),
)


# ---------------------------------------------------------------------------
# sddc.domain.status
# ---------------------------------------------------------------------------

_DOMAIN_STATUS = SddcTypedOp(
    op_id="sddc.domain.status",
    handler_attr="domain_status",
    summary="Lifecycle status of one VCF domain.",
    description=(
        "Reads the lifecycle status of one VCF domain via "
        "GET /v1/domains/{id}/status -- its ACTIVE / ACTIVATING / ERROR "
        "state and the last status transition. Requires a domain id from "
        "sddc.domain.list. The read an operator runs when a domain-create "
        "or expand workflow is in flight or a domain is reported unhealthy. "
        "Works with zero catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "The VCF domain id (from sddc.domain.list).",
            },
        },
        "required": ["id"],
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "domain", "status"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call with a domain id to check whether a domain is ACTIVE or "
            "still activating / errored -- e.g. while a domain-expand "
            "workflow runs."
        ),
        output_shape="{status, ...}. Surface the state and any error detail.",
        parameter_hints={"id": "The VCF domain id from sddc.domain.list."},
    ),
)


# ---------------------------------------------------------------------------
# sddc.cluster.list
# ---------------------------------------------------------------------------

_CLUSTER_LIST = SddcTypedOp(
    op_id="sddc.cluster.list",
    handler_attr="cluster_list",
    summary="vSphere clusters across all or one VCF domain.",
    description=(
        "Lists vSphere clusters across all VCF domains via GET /v1/clusters, "
        "or narrowed to one domain via the optional domainId filter. The "
        "primary inventory read for cluster count, datastore type, and host "
        "membership. Works with zero catalog ingest. safety_level=safe, "
        "read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "domainId": {
                "type": "string",
                "minLength": 1,
                "description": "Optional VCF domain id filter. Omit for all domains.",
            },
        },
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "cluster", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=("Call to list vSphere clusters; pass domainId to scope to one domain."),
        output_shape=(
            "{elements: [{id, name, primaryDatastoreType, domainId, hosts}, ...], pageMetadata}."
        ),
        parameter_hints={"domainId": "VCF domain id; omit for all domains."},
    ),
)


# ---------------------------------------------------------------------------
# sddc.host.list
# ---------------------------------------------------------------------------

_HOST_LIST = SddcTypedOp(
    op_id="sddc.host.list",
    handler_attr="host_list",
    summary="ESXi hosts across all VCF domains, optionally filtered.",
    description=(
        "Enumerates ESXi hosts across all VCF domains via GET /v1/hosts, or "
        "narrowed by the optional domainId / clusterId / status filters. "
        "The primary read for host count, FQDN, ESXi version, and "
        "assignment status. Large VCF deployments return dozens or hundreds "
        "of hosts. Works with zero catalog ingest. safety_level=safe, "
        "read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "domainId": {
                "type": "string",
                "minLength": 1,
                "description": "Optional VCF domain id filter. Omit for all domains.",
            },
            "clusterId": {
                "type": "string",
                "minLength": 1,
                "description": "Optional cluster id filter. Omit for all clusters.",
            },
            "status": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Optional host status filter (e.g. ASSIGNED, "
                    "UNASSIGNED_USEABLE). Omit for all statuses."
                ),
            },
        },
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "host", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to enumerate ESXi hosts; narrow with domainId / clusterId "
            "/ status when the operator named one."
        ),
        output_shape=(
            "{elements: [{id, fqdn, esxiVersion, ipAddresses, domain, "
            "cluster, status}, ...], pageMetadata}."
        ),
        parameter_hints={
            "domainId": "VCF domain id; omit for all.",
            "clusterId": "cluster id; omit for all.",
            "status": "ASSIGNED / UNASSIGNED_USEABLE; omit for all.",
        },
    ),
)


# ---------------------------------------------------------------------------
# sddc.vcenter.list
# ---------------------------------------------------------------------------

_VCENTER_LIST = SddcTypedOp(
    op_id="sddc.vcenter.list",
    handler_attr="vcenter_list",
    summary="vCenter Server instances SDDC Manager manages.",
    description=(
        "Lists the vCenter Server instances SDDC Manager manages via "
        "GET /v1/vcenters, or narrowed to one domain via the optional "
        "domainId filter. Cross the returned fqdn against the vSphere "
        "connector for VM-level reads. Works with zero catalog ingest. "
        "safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "domainId": {
                "type": "string",
                "minLength": 1,
                "description": "Optional VCF domain id filter. Omit for all domains.",
            },
        },
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "vcenter", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to list managed vCenters; cross the fqdn against the "
            "vSphere connector for VM reads."
        ),
        output_shape="{elements: [{id, fqdn, domain}, ...], pageMetadata}.",
        parameter_hints={"domainId": "VCF domain id; omit for all."},
    ),
)


# ---------------------------------------------------------------------------
# sddc.nsxt_cluster.list
# ---------------------------------------------------------------------------

_NSXT_CLUSTER_LIST = SddcTypedOp(
    op_id="sddc.nsxt_cluster.list",
    handler_attr="nsxt_cluster_list",
    summary="NSX-T manager clusters SDDC Manager manages.",
    description=(
        "Lists the NSX-T manager clusters SDDC Manager manages via "
        "GET /v1/nsxt-clusters and the domains each one backs. The read for "
        "mapping which NSX-T cluster fronts a given workload domain. Works "
        "with zero catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "nsxt", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to list NSX-T clusters and the domains they back -- e.g. "
            "when mapping a workload domain to its NSX-T cluster."
        ),
        output_shape="{elements: [{id, vipFqdn, domainIds}, ...], pageMetadata}.",
    ),
)


# ---------------------------------------------------------------------------
# sddc.credential.list  (credential-read gated + redacted)
# ---------------------------------------------------------------------------

_CREDENTIAL_LIST = SddcTypedOp(
    op_id="sddc.credential.list",
    handler_attr="credential_list",
    summary="Nested-infra credential inventory (credential-read gated, redacted).",
    description=(
        "Lists the nested-infra credentials SDDC Manager is the system of "
        "record for via GET /v1/credentials -- the ESXi / vCenter / NSX-T / "
        "backup service accounts it stores and rotates. Credential-read "
        "gated: dispatch routes through the approval queue "
        "(requires_approval=True -- not dispatchable without operator "
        "approval), the op-id is classified credential_read so audit + "
        "broadcast rows collapse to aggregate-only, and the handler scrubs "
        "every secret-keyed value at the boundary. The read shows which "
        "accounts exist (username, resource, accountType) with the password "
        "/ private-key material replaced by ***REDACTED***. Works with zero "
        "catalog ingest. safety_level=caution, requires_approval=True."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_CREDENTIALS,
    tags=("read-only", "sddc", "vcf", "credential-read"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_use=(
            "Call during a nested-infra credential audit or outage to see "
            "which ESXi / vCenter / NSX-T / backup accounts SDDC Manager "
            "holds. Dispatch requires operator approval; secret values are "
            "always redacted."
        ),
        output_shape=(
            "{elements: [{id, resource, accountType, credentialType, "
            "username, password: ***REDACTED***}, ...]}. Never contains a "
            "live secret."
        ),
    ),
)


# ---------------------------------------------------------------------------
# sddc.task.list
# ---------------------------------------------------------------------------

_TASK_LIST = SddcTypedOp(
    op_id="sddc.task.list",
    handler_attr="task_list",
    summary="In-flight or recent VCF workflow tasks, optionally filtered.",
    description=(
        "Lists in-flight or recently completed VCF workflow tasks via "
        "GET /v1/tasks, optionally narrowed by the status filter "
        "(Successful / Failed / In_Progress / Pending / Cancelled). The "
        "read for monitoring a domain-expand, host-commission, or "
        "update-apply workflow, or triaging one that failed. Works with "
        "zero catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["Successful", "Failed", "In_Progress", "Pending", "Cancelled"],
                "description": "Optional task status filter. Omit for all statuses.",
            },
        },
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_TASKS,
    tags=("read-only", "sddc", "vcf", "task", "workflow"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to see what VCF workflows are running or recently ran; "
            "narrow with status='Failed' when triaging."
        ),
        output_shape=(
            "{elements: [{id, name, status, type, subtasks, errors}, ...], "
            "pageMetadata}. For a failed task surface errors[].message."
        ),
        parameter_hints={
            "status": "Successful / Failed / In_Progress / Pending / Cancelled; omit for all.",
        },
    ),
)


# ---------------------------------------------------------------------------
# sddc.system.info
# ---------------------------------------------------------------------------

_SYSTEM_INFO = SddcTypedOp(
    op_id="sddc.system.info",
    handler_attr="system_info",
    summary="SDDC Manager system-level settings summary.",
    description=(
        "Reads the SDDC Manager system-level settings summary via "
        "GET /v1/system -- the appliance-wide configuration (proxy, CEIP, "
        "DNS/NTP posture) SDDC Manager exposes at its /v1/system root. The "
        "read for confirming the SDDC Manager's own platform configuration "
        "before an inventory or lifecycle operation. Works with zero "
        "catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_PLATFORM,
    tags=("read-only", "sddc", "vcf", "system", "platform"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to read the SDDC Manager appliance-wide system settings "
            "before a heavier operation."
        ),
        output_shape="{...} system settings object.",
    ),
)


# ---------------------------------------------------------------------------
# sddc.vcf_service.list
# ---------------------------------------------------------------------------

_VCF_SERVICE_LIST = SddcTypedOp(
    op_id="sddc.vcf_service.list",
    handler_attr="vcf_service_list",
    summary="Running VCF micro-services on the SDDC Manager appliance + health.",
    description=(
        "Lists the VCF platform micro-services running on the SDDC Manager "
        "appliance and their status via GET /v1/vcf-services. The read for "
        "confirming the SDDC Manager control plane is healthy -- which "
        "service is degraded when an operation is failing for no obvious "
        "inventory reason. Works with zero catalog ingest. "
        "safety_level=safe, read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_PLATFORM,
    tags=("read-only", "sddc", "vcf", "service", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to confirm the SDDC Manager control plane is healthy, or "
            "to find which VCF micro-service is degraded."
        ),
        output_shape="{elements: [{id, name, status, version}, ...]}.",
    ),
)


# ---------------------------------------------------------------------------
# sddc.manager.list
# ---------------------------------------------------------------------------

_MANAGER_LIST = SddcTypedOp(
    op_id="sddc.manager.list",
    handler_attr="manager_list",
    summary="SDDC Manager appliance inventory.",
    description=(
        "Lists the SDDC Manager appliances via GET /v1/sddc-managers -- "
        "their FQDN, IP, version, and the management domain each belongs "
        "to. The primary read for 'which SDDC Manager manages this VCF "
        "stack', and the same surface fingerprint reads, exposed as an "
        "operator-callable typed op. Works with zero catalog ingest. "
        "safety_level=safe, read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_PLATFORM,
    tags=("read-only", "sddc", "vcf", "manager", "platform"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to identify the SDDC Manager appliance(s) -- FQDN, IP, "
            "version, management domain -- as a probe or before targeted "
            "reads."
        ),
        output_shape=("{elements: [{id, fqdn, ipAddress, version, domain}, ...], pageMetadata}."),
    ),
)


# ---------------------------------------------------------------------------
# sddc.license.list
# ---------------------------------------------------------------------------

_LICENSE_LIST = SddcTypedOp(
    op_id="sddc.license.list",
    handler_attr="license_list",
    summary="License keys registered with SDDC Manager.",
    description=(
        "Lists the license keys registered with SDDC Manager via "
        "GET /v1/license-keys -- their product type, key, description, and "
        "usage. The read for a licensing-compliance question ('is this VCF "
        "stack licensed', 'which keys are near their limit'). Works with "
        "zero catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_PLATFORM,
    tags=("read-only", "sddc", "vcf", "license", "platform"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=("Call to answer a licensing-compliance question about the VCF stack."),
        output_shape=(
            "{elements: [{key, productType, description, licenseKeyUsage}, ...], pageMetadata}."
        ),
    ),
)


#: The typed ops :class:`SddcManagerConnector` registers at lifespan
#: startup -- the audited 12-read set (#2306). Ordered inventory ->
#: credentials -> tasks -> platform to match the operator's typical path.
SDDC_TYPED_OPS: tuple[SddcTypedOp, ...] = (
    _DOMAIN_LIST,
    _DOMAIN_STATUS,
    _CLUSTER_LIST,
    _HOST_LIST,
    _VCENTER_LIST,
    _NSXT_CLUSTER_LIST,
    _CREDENTIAL_LIST,
    _TASK_LIST,
    _SYSTEM_INFO,
    _VCF_SERVICE_LIST,
    _MANAGER_LIST,
    _LICENSE_LIST,
)


async def register_sddc_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`SDDC_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``; the runner
    (:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`)
    invokes it after
    :func:`~meho_backplane.connectors.registry._eager_import_connectors`
    has walked every connector subpackage, so the descriptor rows land
    before the first dispatch. Idempotent across pod restarts (the helper
    skips the embedding recompute on unchanged summary / description /
    tags). Mirrors :func:`~meho_backplane.connectors.nsx.typed_ops.register_nsx_typed_operations`.

    The ``embedding_service`` keyword-only parameter is the runner
    contract: :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar,
    so each registrar must accept the kwarg. It is forwarded to
    :func:`register_typed_operation` (which falls back to the process-wide
    singleton when ``None``).
    """
    # Lazy imports: the operations package pulls in the embedding pipeline
    # (ONNX runtime + model), which pure connector/handler unit tests
    # should not pay. Lifespan callers have it warmed by the time this runs.
    from meho_backplane.connectors.sddc_manager.connector import SddcManagerConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in SDDC_TYPED_OPS:
        handler = getattr(SddcManagerConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"SddcManagerConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else SDDC_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"SddcManagerConnector typed op {op.op_id!r} declares "
                f"group_key={op.group_key!r} but no curated when_to_use exists for "
                f"that key. Add an entry to SDDC_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=SddcManagerConnector.product,
            version=SddcManagerConnector.version,
            impl_id=SddcManagerConnector.impl_id,
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
        "sddc_typed_operations_registered",
        count=len(SDDC_TYPED_OPS),
        product=SddcManagerConnector.product,
        version=SddcManagerConnector.version,
        impl_id=SddcManagerConnector.impl_id,
    )
