# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: declarative typed-op metadata table. This module is a
# data file — 14 SddcTypedOp records (the #2306 audited 12-read set plus the
# #2837 network-pool pre-flight reads), each a data-only value with grounded
# operator-facing prose (summary / description / llm_instructions) the
# BM25 + embedding search surfaces read verbatim. Cyclomatic complexity is
# trivial; the length is the 14-op catalog, not logic. Splitting the tuple
# across files fragments the single source of truth the registrar iterates.

"""Typed-op metadata + registrar for :class:`SddcManagerConnector` (#2306).

The audited SDDC Manager read set (the #2294 12-read lab-audit surface),
extended with the network-pool pre-flight reads (#2837), is registered as
typed ops (``source_kind="typed"``) so it dispatches on a
fresh boot with **zero catalog ingest** -- the #2247 failure class the
older ingested-row enablement was subject to
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

The typed set here is the **documented operational surface**; the
ingested 375-op VCF catalog stays as profiled-dispatch breadth (#2271)
under its own ``METHOD:path`` op_ids, enable-able through the generic
review flow (``ReviewService.enable_reads``). The two never collide: a
typed op carries a dotted op-id (``sddc.domain.list``) and a code
``handler_ref``, so it resolves through
:func:`~meho_backplane.operations._branches.dispatch_typed`
and never through an ``endpoint_descriptor`` ingested row (#2262). The
three non-audited reads (releases/system, domain detail, bundles) remain
ingested browse breadth.

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
* op-id ``sddc.credential.list`` declared ``credential_read`` in
  ``sddc_manager/op_classes.py`` (decision
  #3's op-id classifier) -- ``classify_op`` returns ``credential_read`` so
  audit + broadcast rows collapse to aggregate-only (redacted).
* the handler additionally scrubs secret-keyed values at the connector
  boundary (:func:`~meho_backplane.connectors.sddc_manager.typed_reads._redact_secrets`).

The curated workload-domain **write** surface (#3497) — network-pool
create, host validate/commission, domain validate/create, plus the
``sddc.task.get`` build poll — ships alongside the reads as typed ops in
:mod:`.typed_writes` (bodies) and this module (metadata), each
approval-gated at the ``caution`` / ``dangerous`` tier and dispatched via
:meth:`HttpConnector._post_json`. The session/auth itself (#2290) and the
profiled ingested dispatch (#2271) remain out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

from meho_backplane.connectors.sddc_manager.typed_writes import (
    SDDC_DOMAIN_CREATE_OP_ID,
    SDDC_DOMAIN_VALIDATE_OP_ID,
    SDDC_HOST_COMMISSION_OP_ID,
    SDDC_HOST_VALIDATE_OP_ID,
    SDDC_NETWORK_POOL_CREATE_OP_ID,
)

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
_GROUP_LIFECYCLE = "sddc-lifecycle"


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
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Short text per typed-op group. The agent reads it through
#: ``list_operation_groups`` to pick a group, so it says which reads and
#: changes each group holds. ``register_typed_operation`` requires a
#: non-empty string whenever ``group_key`` is set.
SDDC_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_INVENTORY: (
        "Reads of the VCF stack SDDC Manager manages: domains and one domain's "
        "status, clusters, ESXi hosts, vCenters, NSX clusters, and network "
        "pools with their free and used IPs. All reads; nothing here changes "
        "anything."
    ),
    _GROUP_PLATFORM: (
        "Reads of SDDC Manager itself: its appliances, its system details, the "
        "VCF services and their status, and the license keys. All reads."
    ),
    _GROUP_TASKS: (
        "Reads of VCF workflow tasks and check results: list recent tasks, read "
        "one task by id to follow a host commission or a domain build, or read "
        "one host or domain check by id to get its result. All reads."
    ),
    _GROUP_CREDENTIALS: (
        "One read: the accounts SDDC Manager stores for ESXi, vCenter, NSX and "
        "backup. A person must approve each call, and passwords are always "
        "hidden."
    ),
    _GROUP_LIFECYCLE: (
        "Changes that build a workload domain: create a network pool, check "
        "and add ESXi hosts, check and create the domain. The two checks "
        "change nothing; read their result with sddc.host.validation.get and "
        "sddc.domain.validation.get. Creating a pool, adding hosts and creating a domain "
        "wait for a person's approval. Creating a pool returns the pool; keep "
        "its id for sddc.host.validate. Adding hosts and creating a domain "
        "return a task; follow them with sddc.task.get."
    ),
}


# ---------------------------------------------------------------------------
# Shared schema helpers
# ---------------------------------------------------------------------------

#: The empty parameter object shared by the no-argument reads.
_NO_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}


def _instructions(
    *,
    when_to_call: str,
    output_shape: str,
    next_step: str,
    preconditions: str | None = None,
    parameter_hints: dict[str, str] | None = None,
    result_scalars: tuple[str, ...] | None = None,
    result_digest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the per-op ``llm_instructions`` blob with the canonical keys.

    ``preconditions`` is set on every op that is not ``safe``: what must be
    true before the call. ``result_scalars`` / ``result_digest`` are the
    JSONFlux reduction hints: on the workload-domain poll ops the vendor
    ``Task`` / ``Validation`` carries a large set-shaped field
    (``subTasks[]`` / ``validationChecks[]``), so ``result_scalars`` names the
    scalar siblings (``id`` / ``status``) that survive the reduction on the
    inline summary and ``result_digest`` names the collection to reduce
    (SDDC's ``Task`` carries three list fields — ``subTasks`` / ``errors`` /
    ``resources`` — so the hint is what makes the reducer reduce ``subTasks``
    instead of passing the whole document through the multi-list
    detail-object exemption).
    """
    blob: dict[str, Any] = {
        "when_to_call": when_to_call,
        "output_shape": output_shape,
        "next_step": next_step,
    }
    if preconditions is not None:
        blob["preconditions"] = preconditions
    if parameter_hints is not None:
        blob["parameter_hints"] = parameter_hints
    if result_scalars is not None:
        blob["result_scalars"] = {"keys": list(result_scalars)}
    if result_digest is not None:
        blob["result_digest"] = dict(result_digest)
    return blob


def _field(type_: str, description: str) -> dict[str, Any]:
    """One described schema field."""
    return {"type": type_, "description": description}


def _object(description: str, properties: dict[str, Any]) -> dict[str, Any]:
    """An open object schema that names its main fields."""
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "additionalProperties": True,
    }


def _list_of(description: str, item_properties: dict[str, Any]) -> dict[str, Any]:
    """An array of open objects that name their main fields."""
    return {
        "type": "array",
        "description": description,
        "items": {"type": "object", "properties": item_properties, "additionalProperties": True},
    }


def _ref(what: str, *, name_field: str = "name") -> dict[str, Any]:
    """A vendor reference object: the id plus a name or FQDN."""
    return _object(
        f"The {what} this belongs to.",
        {
            "id": _field("string", f"The {what} id."),
            name_field: _field("string", f"The {what} {name_field}."),
        },
    )


#: The vendor ``PageMetadata`` every list read returns next to ``elements``.
_PAGE_METADATA: dict[str, Any] = _object(
    "Paging data for the list.",
    {
        "pageNumber": _field("integer", "The current page number."),
        "pageSize": _field("integer", "The number of elements in this page."),
        "totalElements": _field("integer", "The total number of elements."),
        "totalPages": _field("integer", "The total number of pages."),
    },
)


def _page_of(what: str, item_properties: dict[str, Any]) -> dict[str, Any]:
    """The vendor ``PageOf<X>`` answer: ``elements`` plus ``pageMetadata``."""
    return _object(
        f"A page of {what}.",
        {
            "elements": _list_of(f"The {what}.", item_properties),
            "pageMetadata": _PAGE_METADATA,
        },
    )


# ---------------------------------------------------------------------------
# Response fields, named after the SDDC Manager 9.x OpenAPI schemas
# ---------------------------------------------------------------------------

_DOMAIN_FIELDS: dict[str, Any] = {
    "id": _field("string", "The domain id."),
    "name": _field("string", "The domain name."),
    "type": _field("string", "The domain type, for example MANAGEMENT or VI."),
    "status": _field(
        "string", "The domain state, for example ACTIVE, ACTIVATING, UPGRADING or ERROR."
    ),
    "upgradeState": _field("string", "The domain upgrade state."),
    "vcenters": _list_of(
        "The vCenters of the domain.",
        {
            "id": _field("string", "The vCenter id."),
            "fqdn": _field("string", "The vCenter FQDN."),
        },
    ),
    "clusters": _list_of(
        "The clusters of the domain.",
        {
            "id": _field("string", "The cluster id."),
            "name": _field("string", "The cluster name."),
        },
    ),
    "nsxtCluster": _ref("NSX cluster", name_field="vipFqdn"),
    "ssoName": _field("string", "The SSO domain name."),
}

_NETWORK_FIELDS: dict[str, Any] = {
    "id": _field("string", "The network id."),
    "type": _field("string", "The network type, for example VMOTION, VSAN or NFS."),
    "vlanId": _field("integer", "The VLAN id."),
    "mtu": _field("integer", "The MTU."),
    "subnet": _field("string", "The subnet."),
    "mask": _field("string", "The subnet mask."),
    "gateway": _field("string", "The gateway."),
    "ipPools": _list_of(
        "The IP ranges of the network.",
        {
            "start": _field("string", "The first IP of the range."),
            "end": _field("string", "The last IP of the range."),
        },
    ),
    "freeIps": {
        "type": "array",
        "description": "The free IPs. Its length is the free capacity.",
        "items": {"type": "string"},
    },
    "usedIps": {
        "type": "array",
        "description": "The used IPs.",
        "items": {"type": "string"},
    },
}

_NETWORK_POOL_FIELDS: dict[str, Any] = {
    "id": _field("string", "The network pool id."),
    "name": _field("string", "The network pool name."),
    "hostsCount": _field("integer", "The number of hosts that use the pool."),
    "networks": _list_of("The networks of the pool.", _NETWORK_FIELDS),
}

_TASK_FIELDS: dict[str, Any] = {
    "id": _field("string", "The task id. Use it with sddc.task.get."),
    "name": _field("string", "The task name."),
    "type": _field("string", "The task type."),
    "status": _field(
        "string", "The task state, for example PENDING, IN_PROGRESS, SUCCESSFUL or FAILED."
    ),
    "creationTimestamp": _field("string", "When the task was created."),
    "completionTimestamp": _field("string", "When the task ended."),
    "subTasks": _list_of(
        "The steps of the task.",
        {
            "name": _field("string", "The step name."),
            "description": _field("string", "What the step does."),
            "status": _field("string", "The step state."),
        },
    ),
    "errors": _list_of(
        "The errors, when the task failed.",
        {
            "errorCode": _field("string", "The error code."),
            "message": _field("string", "The error message."),
            "remediationMessage": _field("string", "How to fix the error."),
        },
    ),
    "resources": _list_of(
        "The objects the task works on.",
        {
            "resourceId": _field("string", "The object id."),
            "type": _field("string", "The object type."),
            "name": _field("string", "The object name."),
        },
    ),
    "isRetryable": _field("boolean", "Whether the task can be retried."),
}

_VALIDATION_FIELDS: dict[str, Any] = {
    "id": _field("string", "The validation id."),
    "description": _field("string", "What the validation checks."),
    "executionStatus": _field(
        "string", "The run state, for example IN_PROGRESS, COMPLETED or FAILED."
    ),
    "resultStatus": _field(
        "string", "The verdict once it ran, for example SUCCEEDED, WARNING or FAILED."
    ),
    "validationChecks": _list_of(
        "The single checks of the validation.",
        {
            "description": _field("string", "What the check checks."),
            "severity": _field("string", "How serious a failure is."),
            "resultStatus": _field("string", "The verdict of the check."),
            "errorResponse": _object(
                "The error, when the check failed.",
                {"message": _field("string", "The error message.")},
            ),
        },
    ),
}


# ---------------------------------------------------------------------------
# JSONFlux reduction hints for the workload-domain write and poll ops
# ---------------------------------------------------------------------------

#: ``Task`` scalars that must survive a ``subTasks[]`` reduction:
#: ``id`` is the poll key for ``sddc.task.get``; ``status`` carries the
#: lifecycle state; the rest identify the task. Field names pinned by the
#: ``sddc-manager-9.1`` OpenAPI (``components.schemas.Task``).
_TASK_RESULT_SCALARS: tuple[str, ...] = ("id", "name", "status", "type", "creationTimestamp")

#: ``Task`` row-digest hint. A live workload-domain build's ``Task``
#: carries a large ``subTasks[]`` AND populated ``errors[]`` / ``resources[]``
#: — three list fields, so the reducer's dict-of-arrays detail-object
#: exemption would pass the whole document through UNREDUCED. This hint names
#: ``subTasks`` as THE collection to reduce and drives the inline digest:
#: per-``status`` counts, the ``name`` of every IN_PROGRESS/PENDING sub-task,
#: and every FAILED sub-task preserved whole. Status values pinned by the
#: ``sddc-manager-9.1`` OpenAPI (``components.schemas.SubTask.status``:
#: PENDING / IN_PROGRESS / SUCCESSFUL / FAILED / SKIPPED / NOT_APPLICABLE).
_TASK_RESULT_DIGEST: dict[str, Any] = {
    "collection": "subTasks",
    "status_field": "status",
    "name_field": "name",
    "active_states": ["IN_PROGRESS", "PENDING"],
    "failed_states": ["FAILED"],
}

#: ``Validation`` scalars that must survive a ``validationChecks[]`` reduction:
#: ``id`` is the poll key; ``executionStatus`` / ``resultStatus`` carry the
#: lifecycle + verdict; ``description`` names the run. Field names pinned by
#: the ``sddc-manager-9.1`` OpenAPI (``components.schemas.Validation``).
_VALIDATION_RESULT_SCALARS: tuple[str, ...] = (
    "id",
    "description",
    "executionStatus",
    "resultStatus",
)


# ---------------------------------------------------------------------------
# Workload-domain write parameter schemas
# ---------------------------------------------------------------------------

#: The ``{"id": <task id>}`` parameter schema of ``sddc.task.get``.
_TASK_GET_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "minLength": 1,
            "description": "The task id returned by a host commission or a domain create.",
        },
    },
    "required": ["id"],
    "additionalProperties": False,
    "examples": [{"id": "<task-id>"}],
}

#: The ``{"id": <validation id>}`` parameter schema of the two validation reads.
#: The id goes into the URL path, so only letters, digits, ``-`` and ``_`` are
#: allowed (SDDC Manager validation ids are UUIDs).
_VALIDATION_GET_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "minLength": 1,
            "pattern": "^[A-Za-z0-9_-]+$",
            "description": "The validation id returned by the validate call.",
        },
    },
    "required": ["id"],
    "additionalProperties": False,
    "examples": [{"id": "validation-id"}],
}

#: The ``{"spec": <NetworkPool>}`` parameter schema of ``sddc.network_pool.create``.
#: The vendor ``NetworkPool`` requires ``name`` + ``networks``; the body is
#: otherwise passed through verbatim (``additionalProperties`` open) so the
#: full vendor shape reaches the wire without this connector re-modelling it.
_NETWORK_POOL_CREATE_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "spec": {
            "type": "object",
            "description": ("The vendor NetworkPool body, sent as is to POST /v1/network-pools."),
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The name of the new network pool.",
                },
                "networks": {
                    "type": "array",
                    "description": (
                        "The networks of the pool, for example one VMOTION and one NFS or "
                        "VSAN network."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": _field(
                                "string", "The network type, for example VMOTION, VSAN or NFS."
                            ),
                            "vlanId": _field("integer", "The VLAN id of the network."),
                            "mtu": _field("integer", "The MTU of the network."),
                            "subnet": _field("string", "The subnet of the network."),
                            "mask": _field("string", "The subnet mask of the network."),
                            "gateway": _field("string", "The gateway of the network."),
                            "ipPools": {
                                "type": "array",
                                "description": "The IP ranges the hosts get their IPs from.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "start": _field("string", "The first IP of the range."),
                                        "end": _field("string", "The last IP of the range."),
                                    },
                                    "additionalProperties": True,
                                },
                            },
                        },
                        "additionalProperties": True,
                    },
                },
            },
            "required": ["name", "networks"],
            "additionalProperties": True,
        },
    },
    "required": ["spec"],
    "additionalProperties": False,
    "examples": [
        {
            "spec": {
                "name": "<network-pool-name>",
                "networks": [
                    {
                        "type": "VMOTION",
                        "vlanId": 100,
                        "mtu": 9000,
                        "subnet": "<subnet>",
                        "mask": "<subnet-mask>",
                        "gateway": "<gateway-ip>",
                        "ipPools": [{"start": "<first-ip>", "end": "<last-ip>"}],
                    }
                ],
            }
        }
    ],
}

#: The storage types SDDC Manager 9.x accepts in a ``HostCommissionSpec``.
#: The vendor schema types ``storageType`` as a plain string and lists the
#: values in its example text; this is the union of the 9.0 and 9.1 lists.
_HOST_STORAGE_TYPES: list[str] = [
    "VSAN",
    "VSAN_ESA",
    "VSAN_REMOTE",
    "VSAN_MAX",
    "NFS",
    "NFS41",
    "VMFS",
    "VMFS_FC",
    "FC",
    "VVOL",
]

#: The ``{"spec": [<HostCommissionSpec>, ...]}`` parameter schema shared by
#: ``sddc.host.validate`` and ``sddc.host.commission``. The vendor endpoint
#: takes a JSON **array** of ``HostCommissionSpec`` (required ``fqdn`` /
#: ``username`` / ``password`` / ``storageType`` / ``networkPoolId``).
_HOST_COMMISSION_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "spec": {
            "type": "array",
            "minItems": 1,
            "description": (
                "A list of vendor HostCommissionSpec objects, one per ESXi host, sent as is."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "fqdn": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The FQDN of the ESXi host.",
                    },
                    "username": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The ESXi user, usually root.",
                    },
                    "password": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The password of that ESXi user.",
                    },
                    "storageType": {
                        "type": "string",
                        "enum": _HOST_STORAGE_TYPES,
                        "description": "The storage the host will use, for example VSAN or NFS.",
                    },
                    "networkPoolId": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The id of the network pool the host joins.",
                    },
                },
                "required": ["fqdn", "username", "password", "storageType", "networkPoolId"],
                "additionalProperties": True,
            },
        },
    },
    "required": ["spec"],
    "additionalProperties": False,
    "examples": [
        {
            "spec": [
                {
                    "fqdn": "<esxi-host-fqdn>",
                    "username": "root",
                    "password": "<esxi-password>",
                    "storageType": "NFS",
                    "networkPoolId": "<network-pool-id>",
                }
            ]
        }
    ],
}


def _domain_spec_parameter_schema(posted_to: str) -> dict[str, Any]:
    """The ``{"spec": <DomainCreationSpec>}`` schema shared by the domain ops.

    The body is passed through verbatim (``additionalProperties`` open) so the
    full vendor ``DomainCreationSpec`` reaches the wire without this connector
    re-modelling every field — but the **NFS datastore sub-shape** is modelled
    so a well-formed NFS spec validates while a malformed ``nasVolume`` (the
    common trap — reusing a vSAN-shaped spec and swapping only the datastore)
    is rejected before dispatch. ``nfsDatastoreSpecs`` stays optional (a vSAN
    or VMFS domain is a valid spec too); when present, each entry must carry a
    ``nasVolume`` with ``serverName`` / ``path`` / ``readOnly``.
    """
    nas_volume = {
        "type": "object",
        "description": "The NFS share the datastore mounts.",
        "properties": {
            "serverName": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "The NFS server names or IPs.",
            },
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "The path of the share on the NFS server.",
            },
            "readOnly": {
                "type": "boolean",
                "description": "Mount the share read-only. Use false for a domain datastore.",
            },
        },
        "required": ["serverName", "path", "readOnly"],
        "additionalProperties": True,
    }
    nfs_datastore_spec = {
        "type": "object",
        "properties": {"nasVolume": nas_volume},
        "required": ["nasVolume"],
        "additionalProperties": True,
    }
    datastore_spec = {
        "type": "object",
        "description": "The storage of the cluster.",
        "properties": {
            "nfsDatastoreSpecs": {
                "type": "array",
                "items": nfs_datastore_spec,
                "description": "The NFS datastores. Use this, not vsanDatastoreSpec, for NFS.",
            },
        },
        "additionalProperties": True,
    }
    cluster_spec = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The name of the new cluster."},
            "datastoreSpec": datastore_spec,
        },
        "additionalProperties": True,
    }
    compute_spec = {
        "type": "object",
        "description": "The clusters of the new domain.",
        "properties": {
            "clusterSpecs": {
                "type": "array",
                "items": cluster_spec,
                "description": "One entry per cluster: its hosts, network and storage.",
            },
        },
        "additionalProperties": True,
    }
    return {
        "type": "object",
        "properties": {
            "spec": {
                "type": "object",
                "description": (
                    f"The vendor DomainCreationSpec, sent as is to {posted_to}. "
                    "Only the NFS storage part is checked by MEHO."
                ),
                "properties": {
                    "domainName": {"type": "string", "description": "The name of the new domain."},
                    "computeSpec": compute_spec,
                },
                "additionalProperties": True,
            },
        },
        "required": ["spec"],
        "additionalProperties": False,
        "examples": [
            {
                "spec": {
                    "domainName": "<domain-name>",
                    "computeSpec": {
                        "clusterSpecs": [
                            {
                                "name": "<cluster-name>",
                                "datastoreSpec": {
                                    "nfsDatastoreSpecs": [
                                        {
                                            "nasVolume": {
                                                "serverName": ["<nfs-server>"],
                                                "path": "<nfs-share-path>",
                                                "readOnly": False,
                                            }
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                }
            }
        ],
    }


#: Shared ``parameter_hints`` text of the two domain ops.
_DOMAIN_SPEC_HINT = (
    "The full vendor DomainCreationSpec: vcenterSpec, nsxTSpec, and computeSpec "
    "with the hosts, network and storage of each cluster. The example shows only "
    "the parts MEHO checks. For NFS storage use computeSpec.clusterSpecs[]."
    "datastoreSpec.nfsDatastoreSpecs[].nasVolume (serverName[], path, readOnly), "
    "never vsanDatastoreSpec."
)


def _id_params(description: str, placeholder: str) -> dict[str, Any]:
    """The ``{"id": ...}`` parameter schema of the one-object reads."""
    return {
        "type": "object",
        "properties": {"id": {"type": "string", "minLength": 1, "description": description}},
        "required": ["id"],
        "additionalProperties": False,
        "examples": [{"id": placeholder}],
    }


#: The ``{"domainId"?}`` parameter schema of the domain-filtered list reads.
_DOMAIN_FILTER_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domainId": {
            "type": "string",
            "minLength": 1,
            "description": "Optional VCF domain id filter. Omit for all domains.",
        },
    },
    "additionalProperties": False,
    "examples": [{"domainId": "<domain-id>"}],
}


# ---------------------------------------------------------------------------
# sddc.domain.list
# ---------------------------------------------------------------------------

_DOMAIN_LIST = SddcTypedOp(
    op_id="sddc.domain.list",
    handler_attr="domain_list",
    summary="List the VCF domains (management and workload) SDDC Manager manages.",
    description=(
        "Lists every VCF domain via GET /v1/domains: the management domain and "
        "all workload domains, with their vCenters, clusters and NSX cluster. "
        "Start here for any domain question. Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=_page_of("domains", _DOMAIN_FIELDS),
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "domain", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to list all VCF domains, or to find the id of one domain before a "
            "domain-scoped read."
        ),
        output_shape=(
            "{elements: [{id, name, type, status, vcenters: [{id, fqdn}], "
            "clusters: [{id, name}], nsxtCluster: {id, vipFqdn}}], pageMetadata}."
        ),
        next_step=(
            "Pass a domain id to sddc.domain.status, or as domainId to "
            "sddc.cluster.list, sddc.host.list or sddc.vcenter.list."
        ),
    ),
)


# ---------------------------------------------------------------------------
# sddc.domain.status
# ---------------------------------------------------------------------------

_DOMAIN_STATUS = SddcTypedOp(
    op_id="sddc.domain.status",
    handler_attr="domain_status",
    summary="Read one VCF domain and its state (ACTIVE, ACTIVATING, ERROR, ...).",
    description=(
        "Reads one VCF domain via GET /v1/domains/{id}. Its status field says "
        "whether the domain is ACTIVE, still being built or changed, or in "
        "ERROR. Needs a domain id from sddc.domain.list. Read-only."
    ),
    parameter_schema=_id_params("The VCF domain id (from sddc.domain.list).", "<domain-id>"),
    response_schema=_object("One VCF domain.", _DOMAIN_FIELDS),
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "domain", "status"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call with a domain id to check whether a domain is ACTIVE or still "
            "activating, upgrading or in error, for example while a domain build runs. "
            "SDDC Manager 9.x has no separate /status call; this reads the domain object."
        ),
        output_shape=(
            "{id, name, type, status, upgradeState, vcenters, clusters, nsxtCluster, ...} "
            "-- the full domain. Report the status and any error detail."
        ),
        next_step=(
            "If the status is not ACTIVE, look for the running or failed task with sddc.task.list."
        ),
        parameter_hints={"id": "The VCF domain id from sddc.domain.list."},
    ),
)


# ---------------------------------------------------------------------------
# sddc.cluster.list
# ---------------------------------------------------------------------------

_CLUSTER_LIST = SddcTypedOp(
    op_id="sddc.cluster.list",
    handler_attr="cluster_list",
    summary="List the vSphere clusters of all VCF domains or of one domain.",
    description=(
        "Lists vSphere clusters via GET /v1/clusters, for all VCF domains or for "
        "one domain with domainId. Shows each cluster's domain, primary datastore "
        "and hosts. Read-only."
    ),
    parameter_schema=_DOMAIN_FILTER_PARAMS,
    response_schema=_page_of(
        "clusters",
        {
            "id": _field("string", "The cluster id."),
            "name": _field("string", "The cluster name."),
            "status": _field("string", "The cluster state, for example ACTIVE or ERROR."),
            "domain": _ref("domain"),
            "primaryDatastoreName": _field("string", "The primary datastore name."),
            "primaryDatastoreType": _field(
                "string", "The primary datastore type, for example VSAN or NFS."
            ),
            "hosts": _list_of(
                "The hosts of the cluster.",
                {
                    "id": _field("string", "The host id."),
                    "fqdn": _field("string", "The host FQDN."),
                },
            ),
            "isStretched": _field("boolean", "Whether the cluster is a stretched cluster."),
            "isImageBased": _field(
                "boolean", "Whether vSphere Lifecycle Manager images manage the cluster."
            ),
        },
    ),
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "cluster", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call="Call to list vSphere clusters; pass domainId to scope to one domain.",
        output_shape=(
            "{elements: [{id, name, status, domain: {id, name}, primaryDatastoreName, "
            "primaryDatastoreType, hosts: [{id, fqdn}]}], pageMetadata}."
        ),
        next_step="Pass a cluster id as clusterId to sddc.host.list to see its hosts.",
        parameter_hints={"domainId": "VCF domain id; omit for all domains."},
    ),
)


# ---------------------------------------------------------------------------
# sddc.host.list
# ---------------------------------------------------------------------------

_HOST_LIST = SddcTypedOp(
    op_id="sddc.host.list",
    handler_attr="host_list",
    summary="List the ESXi hosts SDDC Manager knows, with optional filters.",
    description=(
        "Lists ESXi hosts via GET /v1/hosts, for all domains or filtered by "
        "domainId, clusterId or status. Shows FQDN, ESXi version, IPs, domain, "
        "cluster, network pool and status. The list can hold hundreds of hosts. "
        "Read-only."
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
                "enum": ["ASSIGNED", "UNASSIGNED_USEABLE", "UNASSIGNED_UNUSEABLE"],
                "description": (
                    "Optional host status filter: ASSIGNED, UNASSIGNED_USEABLE or "
                    "UNASSIGNED_UNUSEABLE. Omit for all statuses."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{"status": "UNASSIGNED_USEABLE"}],
    },
    response_schema=_page_of(
        "ESXi hosts",
        {
            "id": _field("string", "The host id."),
            "fqdn": _field("string", "The host FQDN."),
            "esxiVersion": _field("string", "The ESXi version."),
            "status": _field(
                "string",
                "ASSIGNED (in a domain), UNASSIGNED_USEABLE (free) or UNASSIGNED_UNUSEABLE.",
            ),
            "ipAddresses": _list_of(
                "The IPs of the host.",
                {
                    "ipAddress": _field("string", "The IP address."),
                    "type": _field("string", "What the IP is used for, for example MANAGEMENT."),
                },
            ),
            "domain": _ref("domain"),
            "cluster": _ref("cluster"),
            "networkpool": _ref("network pool"),
            "hardwareVendor": _field("string", "The hardware vendor."),
            "hardwareModel": _field("string", "The hardware model."),
            "compatibleStorageType": _field("string", "The storage type the host fits."),
        },
    ),
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "host", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to list ESXi hosts; narrow with domainId, clusterId or status when "
            "the operator named one. status=UNASSIGNED_USEABLE lists the free hosts."
        ),
        output_shape=(
            "{elements: [{id, fqdn, esxiVersion, status, ipAddresses: [{ipAddress, "
            "type}], domain: {id, name}, cluster: {id, name}, networkpool: {id, "
            "name}}], pageMetadata}. Large lists come back as a result handle."
        ),
        next_step=(
            "Use free host ids when building a domain; use result_query on a handle "
            "to filter a large list."
        ),
        parameter_hints={
            "domainId": "VCF domain id; omit for all.",
            "clusterId": "cluster id; omit for all.",
            "status": "ASSIGNED / UNASSIGNED_USEABLE / UNASSIGNED_UNUSEABLE; omit for all.",
        },
    ),
)


# ---------------------------------------------------------------------------
# sddc.vcenter.list
# ---------------------------------------------------------------------------

_VCENTER_LIST = SddcTypedOp(
    op_id="sddc.vcenter.list",
    handler_attr="vcenter_list",
    summary="List the vCenter Server instances SDDC Manager manages.",
    description=(
        "Lists the vCenters SDDC Manager manages via GET /v1/vcenters, for all "
        "domains or for one domain with domainId. Shows each vCenter's FQDN, "
        "version, status and domain. Read-only."
    ),
    parameter_schema=_DOMAIN_FILTER_PARAMS,
    response_schema=_page_of(
        "vCenters",
        {
            "id": _field("string", "The vCenter id."),
            "fqdn": _field("string", "The vCenter FQDN."),
            "version": _field("string", "The vCenter version."),
            "status": _field("string", "The vCenter state."),
            "domain": _ref("domain"),
        },
    ),
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "vcenter", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=("Call to list managed vCenters, or to find the vCenter of one domain."),
        output_shape=(
            "{elements: [{id, fqdn, version, status, domain: {id, name}}], pageMetadata}."
        ),
        next_step=("Find the vSphere target with this fqdn (list_targets) for VM-level reads."),
        parameter_hints={"domainId": "VCF domain id; omit for all."},
    ),
)


# ---------------------------------------------------------------------------
# sddc.nsxt_cluster.list
# ---------------------------------------------------------------------------

_NSXT_CLUSTER_LIST = SddcTypedOp(
    op_id="sddc.nsxt_cluster.list",
    handler_attr="nsxt_cluster_list",
    summary="List the NSX manager clusters SDDC Manager manages.",
    description=(
        "Lists the NSX manager clusters via GET /v1/nsxt-clusters, with the "
        "domains each one serves and its manager nodes. Use it to find which "
        "NSX cluster a workload domain uses. Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=_page_of(
        "NSX clusters",
        {
            "id": _field("string", "The NSX cluster id."),
            "vipFqdn": _field("string", "The FQDN of the cluster's virtual IP."),
            "version": _field("string", "The NSX version."),
            "status": _field("string", "The NSX cluster state."),
            "isShared": _field("boolean", "Whether more than one domain uses the cluster."),
            "domains": _list_of(
                "The domains the cluster serves.",
                {
                    "id": _field("string", "The domain id."),
                    "name": _field("string", "The domain name."),
                },
            ),
            "nodes": _list_of(
                "The NSX manager nodes.",
                {
                    "id": _field("string", "The node id."),
                    "fqdn": _field("string", "The node FQDN."),
                    "name": _field("string", "The node VM name."),
                },
            ),
        },
    ),
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "nsxt", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to list NSX clusters and the domains they serve, for example to map "
            "a workload domain to its NSX cluster."
        ),
        output_shape=(
            "{elements: [{id, vipFqdn, version, status, domains: [{id, name}], "
            "nodes: [{id, fqdn, name}]}], pageMetadata}."
        ),
        next_step="Find the NSX target with this vipFqdn (list_targets) for NSX reads.",
    ),
)


# ---------------------------------------------------------------------------
# sddc.network_pool.list
# ---------------------------------------------------------------------------

_NETWORK_POOL_LIST = SddcTypedOp(
    op_id="sddc.network_pool.list",
    handler_attr="network_pool_list",
    summary="List the VCF network pools (IP ranges and VLANs) used to add hosts.",
    description=(
        "Lists the network pools SDDC Manager uses when it adds ESXi hosts, via "
        "GET /v1/network-pools. Pick a pool here, then read its free and used IPs "
        "with sddc.network_pool.get. Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=_page_of("network pools", _NETWORK_POOL_FIELDS),
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "network-pool", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to list network pools before adding hosts, and to find the pool a "
            "domain or cluster uses."
        ),
        output_shape=(
            "{elements: [{id, name, hostsCount, networks: [{type, vlanId, subnet, "
            "ipPools}]}], pageMetadata}."
        ),
        next_step="Pass a pool id to sddc.network_pool.get to check its free IPs.",
    ),
)


# ---------------------------------------------------------------------------
# sddc.network_pool.get
# ---------------------------------------------------------------------------

_NETWORK_POOL_GET = SddcTypedOp(
    op_id="sddc.network_pool.get",
    handler_attr="network_pool_get",
    summary="Read one network pool with the free and used IPs of each network.",
    description=(
        "Reads one network pool via GET /v1/network-pools/{id}: each network's "
        "type, VLAN, subnet, IP ranges and the lists of free and used IPs. Use it "
        "to check there are enough free IPs before adding hosts. Read-only."
    ),
    parameter_schema=_id_params(
        "The network-pool id (from sddc.network_pool.list).", "<network-pool-id>"
    ),
    response_schema=_object("One network pool.", _NETWORK_POOL_FIELDS),
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "network-pool", "capacity"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call with a pool id to check whether its VMOTION and VSAN or NFS "
            "networks still have enough free IPs before adding N hosts. Free count "
            "is len(freeIps); used count is len(usedIps), per network."
        ),
        output_shape=(
            "{id, name, hostsCount, networks: [{type, vlanId, subnet, mask, gateway, "
            "ipPools: [{start, end}], freeIps: [...], usedIps: [...]}]}. Report the "
            "free and used counts per network type."
        ),
        next_step=(
            "If each network has at least N free IPs, go on with sddc.host.validate; "
            "if not, the pool needs a bigger IP range first."
        ),
        parameter_hints={"id": "The network-pool id from sddc.network_pool.list."},
    ),
)


# ---------------------------------------------------------------------------
# sddc.credential.list  (credential-read gated + redacted)
# ---------------------------------------------------------------------------

_CREDENTIAL_LIST = SddcTypedOp(
    op_id="sddc.credential.list",
    handler_attr="credential_list",
    summary="List the accounts SDDC Manager stores; needs approval, passwords hidden.",
    description=(
        "Lists the ESXi, vCenter, NSX and backup accounts SDDC Manager stores, "
        "via GET /v1/credentials. A person must approve each call. Passwords and "
        "keys are always replaced by ***REDACTED***, and the audit log keeps only "
        "a count."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=_page_of(
        "stored accounts",
        {
            "id": _field("string", "The credential id."),
            "credentialType": _field("string", "The credential type, for example SSH or API."),
            "accountType": _field("string", "USER, SYSTEM or SERVICE."),
            "username": _field("string", "The account user name."),
            "password": _field("string", "Always ***REDACTED***; never the real password."),
            "resource": _object(
                "The system the account belongs to.",
                {
                    "resourceId": _field("string", "The system id."),
                    "resourceName": _field("string", "The system name."),
                    "resourceType": _field(
                        "string", "The system type, for example ESXI, VCENTER or NSXT_MANAGER."
                    ),
                    "domainNames": {
                        "type": "array",
                        "description": "The domains of the system.",
                        "items": {"type": "string"},
                    },
                },
            ),
            "creationTimestamp": _field("string", "When the credential was created."),
            "modificationTimestamp": _field("string", "When the credential last changed."),
            "expiry": _object(
                "When the password expires.",
                {
                    "expiryDate": _field("string", "The expiry date."),
                    "status": _field("string", "The expiry state."),
                },
            ),
        },
    ),
    group_key=_GROUP_CREDENTIALS,
    tags=("read-only", "sddc", "vcf", "credential-read"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_call=(
            "Call during an account audit or an outage to see which ESXi, vCenter, "
            "NSX and backup accounts SDDC Manager holds and when they expire."
        ),
        output_shape=(
            "{elements: [{id, credentialType, accountType, username, password: "
            "***REDACTED***, resource: {resourceName, resourceType}, expiry}], "
            "pageMetadata}. Never contains a real secret."
        ),
        next_step=(
            "Report which accounts exist and which expire soon. Do not ask for the "
            "real passwords; MEHO never returns them."
        ),
        preconditions=(
            "A person must approve the call before it runs; the call waits until then. "
            "Only call it when the operator asked about stored accounts."
        ),
    ),
)


# ---------------------------------------------------------------------------
# sddc.task.list
# ---------------------------------------------------------------------------

_TASK_LIST = SddcTypedOp(
    op_id="sddc.task.list",
    handler_attr="task_list",
    summary="List running or recent VCF workflow tasks, with an optional status filter.",
    description=(
        "Lists running or recently finished VCF workflow tasks via GET /v1/tasks, "
        "for example host commissions, domain builds and updates. Use it to watch "
        "a workflow or to find one that failed. Read-only."
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
        "examples": [{"status": "Failed"}],
    },
    response_schema=_page_of("tasks", _TASK_FIELDS),
    group_key=_GROUP_TASKS,
    tags=("read-only", "sddc", "vcf", "task", "workflow"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to see which VCF workflows are running or ran lately; pass "
            "status='Failed' when looking for a failure."
        ),
        output_shape=(
            "{elements: [{id, name, type, status, creationTimestamp, subTasks, "
            "errors}], pageMetadata}. Check each task's own status field."
        ),
        next_step=(
            "Pass a task id to sddc.task.get for its steps; for a failed task report "
            "errors[].message and errors[].remediationMessage."
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
    summary="Read the SDDC Manager system details (VCF instance name and domain limit).",
    description=(
        "Reads the SDDC Manager system object via GET /v1/system: its id, the "
        "VCF instance name, and the number of domains the subscription allows. "
        "Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=_object(
        "The SDDC Manager system object.",
        {
            "id": _field("string", "The system id."),
            "vcfInstanceName": _field(
                "string", "The name of this VCF instance (SDDC Manager 9.1 and later)."
            ),
            "maxAllowedDomainsInSubscription": _field(
                "integer", "How many domains the subscription allows."
            ),
        },
    ),
    group_key=_GROUP_PLATFORM,
    tags=("read-only", "sddc", "vcf", "system", "platform"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to learn the VCF instance name, or how many domains the "
            "subscription allows before planning a new domain."
        ),
        output_shape="{id, vcfInstanceName, maxAllowedDomainsInSubscription}.",
        next_step=(
            "Compare the domain limit with the count from sddc.domain.list before "
            "building a new domain."
        ),
    ),
)


# ---------------------------------------------------------------------------
# sddc.vcf_service.list
# ---------------------------------------------------------------------------

_VCF_SERVICE_LIST = SddcTypedOp(
    op_id="sddc.vcf_service.list",
    handler_attr="vcf_service_list",
    summary="List the VCF services on the SDDC Manager appliance and their status.",
    description=(
        "Lists the VCF services that run on the SDDC Manager appliance, with "
        "version and status, via GET /v1/vcf-services. Use it to find which "
        "service is down when SDDC Manager calls fail for no clear reason. "
        "Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=_page_of(
        "VCF services",
        {
            "id": _field("string", "The service id."),
            "name": _field("string", "The service name."),
            "version": _field("string", "The service version."),
            "status": _field("string", "The service state, for example UP or DOWN."),
        },
    ),
    group_key=_GROUP_PLATFORM,
    tags=("read-only", "sddc", "vcf", "service", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to check that the SDDC Manager services are healthy, or to find "
            "which VCF service is down."
        ),
        output_shape="{elements: [{id, name, version, status}], pageMetadata}.",
        next_step=(
            "Report every service whose status is not UP. MEHO cannot restart a "
            "service; the operator does that on the appliance."
        ),
    ),
)


# ---------------------------------------------------------------------------
# sddc.manager.list
# ---------------------------------------------------------------------------

_MANAGER_LIST = SddcTypedOp(
    op_id="sddc.manager.list",
    handler_attr="manager_list",
    summary="List the SDDC Manager appliances with FQDN, version and domain.",
    description=(
        "Lists the SDDC Manager appliances via GET /v1/sddc-managers: FQDN, IP, "
        "version and the management domain each one belongs to. Use it to learn "
        "which SDDC Manager runs this VCF stack. Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=_page_of(
        "SDDC Manager appliances",
        {
            "id": _field("string", "The SDDC Manager id."),
            "fqdn": _field("string", "The SDDC Manager FQDN."),
            "version": _field("string", "The SDDC Manager version."),
            "ipAddress": _field("string", "The SDDC Manager IP (deprecated by the vendor)."),
            "domain": _ref("management domain"),
        },
    ),
    group_key=_GROUP_PLATFORM,
    tags=("read-only", "sddc", "vcf", "manager", "platform"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to identify the SDDC Manager appliance (FQDN, version, management "
            "domain), for example as a first check of a new target."
        ),
        output_shape=(
            "{elements: [{id, fqdn, version, ipAddress, domain: {id, name}}], pageMetadata}."
        ),
        next_step="Use sddc.domain.list next to see the domains this appliance manages.",
    ),
)


# ---------------------------------------------------------------------------
# sddc.license.list
# ---------------------------------------------------------------------------

_LICENSE_LIST = SddcTypedOp(
    op_id="sddc.license.list",
    handler_attr="license_list",
    summary="List the license keys registered with SDDC Manager and their usage.",
    description=(
        "Lists the license keys registered with SDDC Manager via "
        "GET /v1/license-keys: product type, description, how much of each key "
        "is used, and the key masked to its last 5 characters. Use it for "
        "licensing questions. Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=_page_of(
        "license keys",
        {
            "id": _field("string", "The license key id."),
            "key_masked": {
                "type": ["string", "null"],
                "description": (
                    "The license key with only its last 5 characters visible, for "
                    "example '*****-*****-*****-*****-AB12C'. Null when SDDC Manager "
                    "sends an empty key or a key that is not text. MEHO never returns "
                    "a full key."
                ),
            },
            "productType": _field(
                "string", "The product, for example VCENTER, VSAN, ESXI, NSXT or WCP."
            ),
            "description": _field("string", "The description the user gave the key."),
            "productVersion": _field("string", "The product version."),
            "isUnlimited": _field("boolean", "Whether the key has no usage limit."),
            "licenseKeyUsage": _object(
                "How much of the key is used.",
                {
                    "total": _field("integer", "The total units."),
                    "used": _field("integer", "The used units."),
                    "remaining": _field("integer", "The free units."),
                    "licenseUnit": _field("string", "The unit, for example CPU or core."),
                },
            ),
        },
    ),
    group_key=_GROUP_PLATFORM,
    # "license-key" marks this op as secret-bearing (its "key" token is in
    # SECRET_FAMILY_TAGS), so the flight recorder leaves the raw vendor
    # body of the call out of the trace. The handler masks the key only
    # in its own return value.
    tags=("read-only", "sddc", "vcf", "license", "platform", "license-key"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to answer a licensing question: is the stack licensed, and which "
            "keys are close to their limit."
        ),
        output_shape=(
            "{elements: [{id, key_masked, productType, description, licenseKeyUsage: "
            "{total, used, remaining}}], pageMetadata}. key_masked shows only the "
            "last 5 characters of the key."
        ),
        next_step=(
            "Report keys with few remaining units per product type. MEHO does not "
            "add or change license keys."
        ),
    ),
)


# ---------------------------------------------------------------------------
# sddc.task.get — workload-domain build poll
# ---------------------------------------------------------------------------

_TASK_GET = SddcTypedOp(
    op_id="sddc.task.get",
    handler_attr="task_get",
    summary="Read one VCF workflow task by id, to follow a host commission or domain build.",
    description=(
        "Reads one VCF workflow task via GET /v1/tasks/{id}. Use it to follow a "
        "host commission or domain create, which both return a task at once and "
        "run on. status says PENDING, IN_PROGRESS, SUCCESSFUL or FAILED; subTasks "
        "shows each step. Read-only."
    ),
    parameter_schema=_TASK_GET_PARAMS,
    response_schema=_object("One VCF workflow task.", _TASK_FIELDS),
    group_key=_GROUP_TASKS,
    tags=("read-only", "sddc", "vcf", "task", "lifecycle"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call with a task id to follow a host commission or domain create until "
            "it ends; read subTasks for the progress of each step."
        ),
        output_shape=(
            "{id, name, status, type, creationTimestamp, subTasks: [...], "
            "errors: [...]}. When subTasks[] comes back as a result handle, id, name "
            "and status stay top-level with a count per step status."
        ),
        next_step=(
            "While status is PENDING or IN_PROGRESS, call again later. On SUCCESSFUL "
            "after a domain create, check sddc.domain.status. On FAILED, report "
            "errors[].message and the failed steps."
        ),
        parameter_hints={"id": "The task id from sddc.host.commission / sddc.domain.create."},
        result_scalars=_TASK_RESULT_SCALARS,
        result_digest=_TASK_RESULT_DIGEST,
    ),
)


# ---------------------------------------------------------------------------
# sddc.domain.validation.get / sddc.host.validation.get — validation results
# ---------------------------------------------------------------------------


def _validation_get_op(kind: str, path: str, next_op: str) -> SddcTypedOp:
    """The safe read of one ``sddc.<kind>.validate`` result by its id (#3518).

    ``result_scalars`` keeps ``executionStatus`` / ``resultStatus`` in the
    answer when ``validationChecks[]`` becomes a result handle, so the caller
    reads the result without opening the handle.
    """
    return SddcTypedOp(
        op_id=f"sddc.{kind}.validation.get",
        handler_attr=f"{kind}_validation_get",
        summary=f"Read the result of a {kind} check (sddc.{kind}.validate) by its id.",
        description=(
            f"Reads one {kind} validation via GET {path}. executionStatus says "
            "whether the check is done; resultStatus says whether it passed "
            "(SUCCEEDED, WARNING or FAILED); validationChecks lists each single "
            "check. Read-only."
        ),
        parameter_schema=_VALIDATION_GET_PARAMS,
        response_schema=_object(f"One {kind} validation.", _VALIDATION_FIELDS),
        group_key=_GROUP_TASKS,
        tags=("read-only", "sddc", "vcf", kind, "validation", "lifecycle"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_instructions(
            when_to_call=(
                f"Call with the id that sddc.{kind}.validate returned, to get the "
                "result of the check."
            ),
            output_shape=(
                "{id, description, executionStatus, resultStatus, validationChecks: "
                "[...]}. When validationChecks[] comes back as a result handle, id, "
                "executionStatus and resultStatus stay top-level; use result_query "
                "on the handle to find the failed checks."
            ),
            next_step=(
                "While executionStatus is IN_PROGRESS, call again later. When it is "
                f"COMPLETED and resultStatus is SUCCEEDED, go on with {next_op}. "
                "Otherwise report the failed validationChecks."
            ),
            parameter_hints={"id": f"The id from the sddc.{kind}.validate answer."},
            result_scalars=_VALIDATION_RESULT_SCALARS,
        ),
    )


_DOMAIN_VALIDATION_GET = _validation_get_op(
    "domain", "/v1/domains/validations/{id}", SDDC_DOMAIN_CREATE_OP_ID
)
_HOST_VALIDATION_GET = _validation_get_op(
    "host", "/v1/hosts/validations/{id}", SDDC_HOST_COMMISSION_OP_ID
)


# ---------------------------------------------------------------------------
# sddc.network_pool.create — workload-domain write
# ---------------------------------------------------------------------------

_NETWORK_POOL_CREATE = SddcTypedOp(
    op_id=SDDC_NETWORK_POOL_CREATE_OP_ID,
    handler_attr="network_pool_create",
    summary="Create a network pool that new ESXi hosts take their IPs from.",
    description=(
        "Creates a VCF network pool via POST /v1/network-pools. This is step 1 of "
        "a workload domain build: the hosts you add later take their vMotion and "
        "storage IPs from this pool. Waits for a person's approval. Returns the "
        "new pool."
    ),
    parameter_schema=_NETWORK_POOL_CREATE_PARAMS,
    response_schema=_object("The new network pool.", _NETWORK_POOL_FIELDS),
    group_key=_GROUP_LIFECYCLE,
    tags=("write", "sddc", "vcf", "network-pool", "lifecycle"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_call=(
            "Call with a NetworkPool spec to create the pool the hosts of a new "
            "workload domain will use."
        ),
        output_shape="{id, name, networks: [...]} -- the new network pool.",
        next_step=("Keep the new pool id; pass it as networkPoolId in sddc.host.validate."),
        preconditions=(
            "Check with sddc.network_pool.list that no pool with this name or these "
            "VLANs exists. The VLANs, subnets and IP ranges must be free on the "
            "network. A person must approve the call before it runs."
        ),
        parameter_hints={
            "spec": (
                "The NetworkPool body: name and networks[] (for example VMOTION plus "
                "NFS or VSAN), each with type, vlanId, mtu, subnet, mask, gateway "
                "and ipPools[]."
            ),
        },
    ),
)


# ---------------------------------------------------------------------------
# sddc.host.validate — workload-domain pre-flight
# ---------------------------------------------------------------------------

_HOST_VALIDATE = SddcTypedOp(
    op_id=SDDC_HOST_VALIDATE_OP_ID,
    handler_attr="host_validate",
    summary="Check ESXi hosts before commissioning them (changes nothing).",
    description=(
        "Sends a list of HostCommissionSpec to POST /v1/hosts/validations. SDDC "
        "Manager checks the hosts and changes nothing. Returns a validation; run "
        "it until resultStatus is SUCCEEDED before sddc.host.commission."
    ),
    parameter_schema=_HOST_COMMISSION_PARAMS,
    response_schema=_object("The validation run.", _VALIDATION_FIELDS),
    group_key=_GROUP_LIFECYCLE,
    tags=("write", "sddc", "vcf", "host", "validation", "lifecycle"),
    safety_level="caution",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call with the list of HostCommissionSpec to check the hosts before "
            "sddc.host.commission."
        ),
        output_shape=(
            "{id, description, executionStatus, resultStatus, validationChecks: "
            "[...]}. Wait for executionStatus COMPLETED, then read resultStatus."
        ),
        next_step=(
            "While executionStatus is IN_PROGRESS, read the result with "
            "sddc.host.validation.get and the returned id. If resultStatus is "
            "SUCCEEDED, call sddc.host.commission with the same spec. If not, "
            "report the failed validationChecks and fix the spec."
        ),
        preconditions=(
            "The network pool must exist (sddc.network_pool.get). The hosts must be "
            "reachable, run a supported ESXi version, and not be in SDDC Manager yet."
        ),
        parameter_hints={
            "spec": "The list of HostCommissionSpec, one entry per ESXi host.",
        },
        result_scalars=_VALIDATION_RESULT_SCALARS,
    ),
)


# ---------------------------------------------------------------------------
# sddc.host.commission — workload-domain write
# ---------------------------------------------------------------------------

_HOST_COMMISSION = SddcTypedOp(
    op_id=SDDC_HOST_COMMISSION_OP_ID,
    handler_attr="host_commission",
    summary="Commission ESXi hosts into SDDC Manager's free pool.",
    description=(
        "Sends the checked list of HostCommissionSpec to POST /v1/hosts. SDDC "
        "Manager adds the ESXi hosts to its free pool. This is step 2 of a "
        "workload domain build. Waits for a person's approval, then returns a "
        "task at once; follow it with sddc.task.get."
    ),
    parameter_schema=_HOST_COMMISSION_PARAMS,
    response_schema=_object("The task that adds the hosts.", _TASK_FIELDS),
    group_key=_GROUP_LIFECYCLE,
    tags=("write", "sddc", "vcf", "host", "commission", "dangerous"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_call=(
            "Call with the checked list of HostCommissionSpec to add the hosts to SDDC Manager."
        ),
        output_shape=(
            "{id, name, status, subTasks: [...]} -- a task. Keep the id. When "
            "subTasks[] comes back as a result handle, id and status stay top-level."
        ),
        next_step=(
            "Call sddc.task.get with the task id until status is SUCCESSFUL or "
            "FAILED. Then check sddc.host.list with status=UNASSIGNED_USEABLE."
        ),
        preconditions=(
            "Run sddc.host.validate with the same spec first, and go on only when "
            "its resultStatus is SUCCEEDED. A person must approve the call before it "
            "runs."
        ),
        parameter_hints={"spec": "The checked list of HostCommissionSpec."},
        result_scalars=_TASK_RESULT_SCALARS,
        result_digest=_TASK_RESULT_DIGEST,
    ),
)


# ---------------------------------------------------------------------------
# sddc.domain.validate — workload-domain pre-flight
# ---------------------------------------------------------------------------

_DOMAIN_VALIDATE = SddcTypedOp(
    op_id=SDDC_DOMAIN_VALIDATE_OP_ID,
    handler_attr="domain_validate",
    summary="Check a DomainCreationSpec before creating the domain (changes nothing).",
    description=(
        "Sends a DomainCreationSpec to POST /v1/domains/validations. SDDC Manager "
        "checks the spec and changes nothing. Returns a validation; run it until "
        "resultStatus is SUCCEEDED before sddc.domain.create, because the spec "
        "is easy to get wrong."
    ),
    parameter_schema=_domain_spec_parameter_schema("/v1/domains/validations"),
    response_schema=_object("The validation run.", _VALIDATION_FIELDS),
    group_key=_GROUP_LIFECYCLE,
    # "credential": the spec holds the passwords of the new domain and can hold
    # licence keys. The word is a secret tag word of the flight recorder
    # (SECRET_FAMILY_TAGS), so a trace keeps no request or answer body of this
    # call, the same as for a credential write.
    tags=("write", "sddc", "vcf", "domain", "validation", "lifecycle", "credential"),
    safety_level="caution",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call with a DomainCreationSpec to check the domain before sddc.domain.create."
        ),
        output_shape=(
            "{id, description, executionStatus, resultStatus, validationChecks: "
            "[...]}. Wait for executionStatus COMPLETED, then read resultStatus."
        ),
        next_step=(
            "While executionStatus is IN_PROGRESS, read the result with "
            "sddc.domain.validation.get and the returned id. If resultStatus is "
            "SUCCEEDED, call sddc.domain.create with the same spec. If not, "
            "report the failed validationChecks and fix the spec."
        ),
        preconditions=(
            "The hosts in the spec must be commissioned and free "
            "(sddc.host.list status=UNASSIGNED_USEABLE). Check the domain limit with "
            "sddc.system.info."
        ),
        parameter_hints={"spec": _DOMAIN_SPEC_HINT},
        result_scalars=_VALIDATION_RESULT_SCALARS,
    ),
)


# ---------------------------------------------------------------------------
# sddc.domain.create — workload-domain write
# ---------------------------------------------------------------------------

_DOMAIN_CREATE = SddcTypedOp(
    op_id=SDDC_DOMAIN_CREATE_OP_ID,
    handler_attr="domain_create",
    summary="Create a VCF workload domain; returns a task, the build takes hours.",
    description=(
        "Sends a checked DomainCreationSpec to POST /v1/domains. This is the last "
        "step of a workload domain build. Waits for a person's approval, then "
        "returns a task at once while the build runs for hours. The approver "
        "never sees the spec body or its passwords."
    ),
    parameter_schema=_domain_spec_parameter_schema("/v1/domains"),
    response_schema=_object("The task that builds the domain.", _TASK_FIELDS),
    group_key=_GROUP_LIFECYCLE,
    # "credential" keeps the spec out of trace bodies (see sddc.domain.validate).
    tags=("write", "sddc", "vcf", "domain", "create", "dangerous", "credential"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=_instructions(
        when_to_call=("Call with a checked DomainCreationSpec to build the workload domain."),
        output_shape=(
            "{id, name, status, subTasks: [...]} -- a task. Keep the id. When "
            "subTasks[] comes back as a result handle, id and status stay top-level "
            "with a count per step status."
        ),
        next_step=(
            "Call sddc.task.get with the task id until status is SUCCESSFUL or "
            "FAILED, then sddc.domain.status until the domain is ACTIVE."
        ),
        preconditions=(
            "Run sddc.domain.validate with the same spec first, and go on only when "
            "its resultStatus is SUCCEEDED. A person must approve the call before it "
            "runs."
        ),
        parameter_hints={"spec": _DOMAIN_SPEC_HINT},
        result_scalars=_TASK_RESULT_SCALARS,
        result_digest=_TASK_RESULT_DIGEST,
    ),
)


#: The typed ops :class:`SddcManagerConnector` registers at lifespan
#: startup -- the audited 12-read set (#2306), the network-pool pre-flight
#: reads (#2837), and the curated workload-domain write path (#3497:
#: network-pool create, host validate/commission, domain validate/create,
#: plus the task poll), and the two validation-result reads (#3518). Ordered
#: inventory -> credentials -> tasks -> platform -> lifecycle to match the
#: operator's typical path.
SDDC_TYPED_OPS: tuple[SddcTypedOp, ...] = (
    _DOMAIN_LIST,
    _DOMAIN_STATUS,
    _CLUSTER_LIST,
    _HOST_LIST,
    _VCENTER_LIST,
    _NSXT_CLUSTER_LIST,
    _NETWORK_POOL_LIST,
    _NETWORK_POOL_GET,
    _CREDENTIAL_LIST,
    _TASK_LIST,
    _TASK_GET,
    _DOMAIN_VALIDATION_GET,
    _HOST_VALIDATION_GET,
    _SYSTEM_INFO,
    _VCF_SERVICE_LIST,
    _MANAGER_LIST,
    _LICENSE_LIST,
    _NETWORK_POOL_CREATE,
    _HOST_VALIDATE,
    _HOST_COMMISSION,
    _DOMAIN_VALIDATE,
    _DOMAIN_CREATE,
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
