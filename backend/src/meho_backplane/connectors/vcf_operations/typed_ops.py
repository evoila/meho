# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) read ops for :class:`VcfOperationsConnector`.

Initiative #2266 (T3, #2303) converts the vROps *audited* read set —
the ops the adopter actually runs (audit #2294) — from ``is_enabled``
curation over ingested ``endpoint_descriptor`` rows to **typed** ops
(``source_kind="typed"``) dispatched directly on the connector's
existing hand-rolled HTTP Basic (+ optional ``auth-source``) session.
A typed op works on a fresh boot with **zero catalog ingest**: the
dispatcher resolves its ``handler_ref`` (a bound method on
:class:`~meho_backplane.connectors.vcf_operations.connector.VcfOperationsConnector`)
and calls it directly, never touching an ingested descriptor row (the
#2262 registration invariant).

The audited set (three ops):

* ``vrops.liveness`` — appliance liveness + identity. Reads
  ``GET /suite-api/api/versions/current`` — the same surface
  :meth:`VcfOperationsConnector.probe` / :meth:`fingerprint` already
  use as vROps' reachability check. The adopter's audit named the
  probe ``casa/health``, but the CaSA (Cluster and Slice
  Administration) API is a **private, undocumented** surface; vROps
  exposes no public dedicated health endpoint distinct from the
  version surface (see the connector's "Probe" docstring). The
  documented ``versions/current`` probe is therefore the grounded
  liveness op. Supersedes the curated ``vrops.about``.
* ``vrops.alert.list`` — alert triage. Reads ``GET /suite-api/api/alerts``
  with the vROps alert filters. Supersedes the curated
  ``GET:/suite-api/api/alerts`` ingested row.
* ``vrops.resource.query`` — resource lookup. A **body-shaped POST**
  to ``POST /suite-api/api/resources/query`` carrying a typed
  ``ResourceQuerySpec`` request body (a richer query surface than the
  ``GET /suite-api/api/resources`` list the ingested curation still
  browses).

Sibling precedent for the dataclass + registrar shape:
:mod:`meho_backplane.connectors.argocd.ops` (metadata dataclasses here,
thin bound-method handlers on the connector, a module-level registrar
queued onto :func:`register_typed_op_registrar`) and
:mod:`meho_backplane.connectors.vmware_rest.typed_ops`.

Unconverted curated ops (resource list/get, alert definitions, symptoms,
recommendations, super metrics) are **declined** from typed conversion —
they are not in the adopter's audited operational set; the ingested
breadth catalog still covers the browse case, and their ``is_enabled``
curation stays in :mod:`.core_ops` until T7 retires the apparatus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VROPS_ALERT_LIST_OP",
    "VROPS_LIVENESS_OP",
    "VROPS_RESOURCE_QUERY_BODY_FIELDS",
    "VROPS_RESOURCE_QUERY_OP",
    "VROPS_TYPED_OPS",
    "VROPS_TYPED_WHEN_TO_USE_BY_GROUP",
    "VropsTypedOp",
    "register_vcf_operations_typed_operations",
]

_log = structlog.get_logger(__name__)

#: Group keys for the typed read surface. Chosen distinct from the
#: ingested classifier slugs in
#: :data:`~meho_backplane.connectors.vcf_operations.core_ops.VROPS_PATH_RULES`
#: (``vrops-system`` / ``vrops-alerts`` / ``vrops-resources`` / ...) so
#: the typed OperationGroup rows never share a row with the ingested
#: browse groups and their curated ``when_to_use`` blurbs can't tug
#: against each other.
_LIVENESS_GROUP_KEY = "vrops-liveness"
_ALERT_TRIAGE_GROUP_KEY = "vrops-alert-triage"
_RESOURCE_QUERY_GROUP_KEY = "vrops-resource-query"

#: Top-level ``ResourceQuerySpec`` body fields ``vrops.resource.query``
#: forwards (a curated subset of the full spec). ``page`` / ``pageSize``
#: are pagination *query* params, not body fields, so they are excluded
#: here and threaded onto the URL by the handler.
VROPS_RESOURCE_QUERY_BODY_FIELDS: tuple[str, ...] = (
    "resourceId",
    "name",
    "regex",
    "adapterKind",
    "resourceKind",
    "resourceState",
    "resourceStatus",
    "resourceHealth",
    "parentId",
    "statKey",
)


@dataclass(frozen=True)
class VropsTypedOp:
    """Metadata for one vROps typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :func:`register_vcf_operations_typed_operations` can splat
    the dataclass into the helper without per-op boilerplate.
    ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.vcf_operations.connector.VcfOperationsConnector`
    exposing the async handler; the registrar resolves the bound method
    against the class so the dispatcher's
    :func:`~meho_backplane.operations._handler_resolve.import_handler`
    walk recovers the callable from the persisted ``module.ClassName.method``
    path. Mirrors
    :class:`~meho_backplane.connectors.argocd.ops.ArgoCdOp`.
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
#: :func:`register_typed_operation` requires a non-empty string whenever
#: ``group_key`` is set (typed_register ``_validate_when_to_use_pairing``).
VROPS_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _LIVENESS_GROUP_KEY: (
        "Use to check that a vROps (VMware Aria Operations) appliance is "
        "reachable and to read its identity — release name and build "
        "number. The cheap pre-flight probe to run before any heavier "
        "alert or resource read, or when confirming which vROps instance a "
        "target points at. Read-only."
    ),
    _ALERT_TRIAGE_GROUP_KEY: (
        "Use to triage vROps alerts: list currently firing or recently "
        "resolved alerts, filtered by whether they are still active, by "
        "criticality, by status, or scoped to a specific resource. The "
        "right group when the question is 'what is alerting right now?', "
        "'show me the critical alerts', or 'what is firing on this "
        "resource?'. Read-only."
    ),
    _RESOURCE_QUERY_GROUP_KEY: (
        "Use to look up vROps-monitored resources (VMs, hosts, datastores, "
        "cloud objects) by a structured query — match on name, regex, "
        "adapter kind, resource kind, state, status, health, or parent — "
        "with pagination. The right group when the question is 'find the "
        "resource(s) matching X' or 'which monitored objects are in state "
        "Y?'. Read-only."
    ),
}


VROPS_LIVENESS_OP = VropsTypedOp(
    op_id="vrops.liveness",
    handler_attr="liveness",
    summary="vROps appliance liveness and identity (release name + build number).",
    description=(
        "Reads GET /suite-api/api/versions/current directly on the "
        "connector's HTTP Basic (+ optional auth-source) session — the same "
        "surface the connector's reachability probe uses — so it works with "
        "zero catalog ingest. Returns the appliance's releaseName and "
        "buildNumber (and humanlyReadableReleaseName when the build emits "
        "it). The cheap pre-flight liveness/identity probe before any "
        "heavier vROps read. vROps exposes no public dedicated health "
        "endpoint distinct from the version surface (the CaSA API is "
        "private/undocumented), so this documented endpoint is the "
        "liveness check. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "releaseName": {"type": ["string", "null"]},
            "buildNumber": {"type": ["integer", "string", "null"]},
        },
        "additionalProperties": True,
    },
    group_key=_LIVENESS_GROUP_KEY,
    tags=("read-only", "vrops", "vmware", "liveness", "probe"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call first to confirm a vROps appliance is reachable and to "
            "read which version/build it runs, before any alert or resource "
            "query. Supersedes the older vrops.about op."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{releaseName: '9.0.0.1.23456789', buildNumber: 23456789, "
            "humanlyReadableReleaseName?: '...'}. A returned payload means "
            "the appliance answered; the dispatcher surfaces transport "
            "failures as a connector_error instead."
        ),
    },
)


VROPS_ALERT_LIST_OP = VropsTypedOp(
    op_id="vrops.alert.list",
    handler_attr="alert_list",
    summary="List vROps alerts (currently firing or recently resolved) for triage.",
    description=(
        "Reads GET /suite-api/api/alerts directly on the connector session "
        "(no catalog ingest). Optional filters: activeOnly (bool, default "
        "false — server-side false lists resolved alerts too), "
        "alertCriticality (CRITICAL / IMMEDIATE / WARNING / INFORMATION / "
        "UNKNOWN), alertStatus (ACTIVE / CANCELED / SUSPENDED / UPDATED), "
        "resourceId (one or more resource UUIDs to scope to), and page / "
        "pageSize pagination. Each alert carries alertId, "
        "alertDefinitionId, alertDefinitionName, alertLevel, alertImpact, "
        "resourceId, startTimeUTC, cancelTimeUTC, status and controlState. "
        "Large alert sets are reduced to a JSONFlux handle with a bounded "
        "inline sample. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "activeOnly": {
                "type": "boolean",
                "description": (
                    "When true, list only alerts that are still active; "
                    "omit or false to include recently resolved alerts."
                ),
            },
            "alertCriticality": {
                "type": "string",
                "enum": ["CRITICAL", "IMMEDIATE", "WARNING", "INFORMATION", "UNKNOWN"],
                "description": "Scope to a single alert criticality level.",
            },
            "alertStatus": {
                "type": "string",
                "enum": ["ACTIVE", "CANCELED", "SUSPENDED", "UPDATED"],
                "description": "Scope to a single alert status.",
            },
            "resourceId": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": (
                    "Optional list of resource UUIDs to scope the alert "
                    "listing to (from vrops.resource.query)."
                ),
            },
            "page": {
                "type": "integer",
                "minimum": 0,
                "description": "0-based page number (pagination).",
            },
            "pageSize": {
                "type": "integer",
                "minimum": 1,
                "description": "Number of alerts per page (pagination).",
            },
        },
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "alerts": {"type": "array"},
            "pageInfo": {"type": "object"},
        },
        "additionalProperties": True,
    },
    group_key=_ALERT_TRIAGE_GROUP_KEY,
    tags=("read-only", "vrops", "vmware", "alerts"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator asks what is alerting in vROps, wants "
            "the critical/active alerts, or wants alerts firing on a "
            "specific resource. Filter with activeOnly / alertCriticality / "
            "alertStatus / resourceId to narrow large sets."
        ),
        "parameter_hints": {
            "activeOnly": "true to hide resolved alerts; omit for all.",
            "alertCriticality": "e.g. 'CRITICAL' to see only critical alerts.",
            "resourceId": "List of resource UUIDs to scope to; omit for all.",
        },
        "output_shape": (
            "{alerts: [{alertId, alertDefinitionId, alertDefinitionName, "
            "alertLevel, alertImpact, resourceId, startTimeUTC, "
            "cancelTimeUTC, status, controlState}, ...], pageInfo, links}. "
            "Cross-reference resourceId with vrops.resource.query for "
            "affected-object detail. Large sets return a JSONFlux handle."
        ),
    },
)


VROPS_RESOURCE_QUERY_OP = VropsTypedOp(
    op_id="vrops.resource.query",
    handler_attr="resource_query",
    summary="Look up vROps-monitored resources by a structured query (body POST).",
    description=(
        "Issues POST /suite-api/api/resources/query with a typed "
        "ResourceQuerySpec request body directly on the connector session "
        "(no catalog ingest). Match resources by any combination of: "
        "resourceId (UUIDs), name, regex, adapterKind (e.g. 'VMWARE'), "
        "resourceKind (e.g. 'VirtualMachine', 'HostSystem', 'Datastore'), "
        "resourceState, resourceStatus, resourceHealth, parentId, and "
        "statKey; page / pageSize paginate. The POST query surface is the "
        "richer counterpart of the GET /suite-api/api/resources list (each "
        "array field is OR-matched within, AND-matched across fields). "
        "Returns resourceList[] with identifier (UUID), resourceKey (name "
        "+ resourceKindKey + adapterKindKey + resourceIdentifiers[]), "
        "resourceStatusStates[], plus pageInfo. Large sets are reduced to "
        "a JSONFlux handle. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "resourceId": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": "Match specific resource UUIDs.",
            },
            "name": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": "Match resources whose name equals any listed value.",
            },
            "regex": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": "Match resource names against any listed regex.",
            },
            "adapterKind": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": "Match by adapter kind key (e.g. 'VMWARE').",
            },
            "resourceKind": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": (
                    "Match by resource kind key (e.g. 'VirtualMachine', 'HostSystem', 'Datastore')."
                ),
            },
            "resourceState": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": "Match by resource state (e.g. 'STARTED', 'STOPPED').",
            },
            "resourceStatus": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": "Match by resource status (e.g. 'DATA_RECEIVING').",
            },
            "resourceHealth": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": "Match by health (e.g. 'GREEN', 'YELLOW', 'RED').",
            },
            "parentId": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": "Match resources whose parent is any listed UUID.",
            },
            "statKey": {
                "type": "string",
                "minLength": 1,
                "description": "Restrict to resources that report this stat key.",
            },
            "page": {
                "type": "integer",
                "minimum": 0,
                "description": "0-based page number (pagination).",
            },
            "pageSize": {
                "type": "integer",
                "minimum": 1,
                "description": "Number of resources per page (pagination).",
            },
        },
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "resourceList": {"type": "array"},
            "pageInfo": {"type": "object"},
        },
        "additionalProperties": True,
    },
    group_key=_RESOURCE_QUERY_GROUP_KEY,
    tags=("read-only", "vrops", "vmware", "resources", "query"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to find vROps-monitored resources matching a structured "
            "query — by name/regex, adapterKind, resourceKind, state, "
            "status, health, or parent. Prefer this over browsing the "
            "ingested resource list when the operator names concrete match "
            "criteria."
        ),
        "parameter_hints": {
            "resourceKind": "e.g. 'VirtualMachine' / 'HostSystem' / 'Datastore'.",
            "name": "Exact-name matches (list); use regex for patterns.",
            "adapterKind": "e.g. 'VMWARE'.",
        },
        "output_shape": (
            "{resourceList: [{identifier, resourceKey: {name, "
            "resourceKindKey, adapterKindKey, resourceIdentifiers}, "
            "resourceStatusStates, creationTime}, ...], pageInfo, links}. "
            "Feed identifier UUIDs into vrops.alert.list(resourceId=...) to "
            "find alerts on a matched resource. Large sets return a "
            "JSONFlux handle."
        ),
    },
)


#: The typed ops :class:`VcfOperationsConnector` registers at lifespan
#: startup — the audited vROps read set (#2303). The tuple shape lets a
#: future typed read join without touching the registrar.
VROPS_TYPED_OPS: tuple[VropsTypedOp, ...] = (
    VROPS_LIVENESS_OP,
    VROPS_ALERT_LIST_OP,
    VROPS_RESOURCE_QUERY_OP,
)


async def register_vcf_operations_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`VROPS_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``; the runner
    (:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`)
    invokes it after
    :func:`~meho_backplane.connectors.registry._eager_import_connectors`
    has walked every connector subpackage, so the descriptor rows land
    before the first dispatch. Idempotent across pod restarts (the helper
    skips the embedding recompute on unchanged summary / description /
    tags). Mirrors :func:`register_argocd_typed_operations` and the
    vmware typed-op registrar.

    The ``embedding_service`` keyword-only parameter is the runner
    contract: :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar,
    so each registrar must accept the kwarg. It is forwarded to
    :func:`register_typed_operation` (which falls back to the process-wide
    singleton when ``None``).
    """
    # Lazy import: the operations package pulls in the embedding pipeline
    # (ONNX runtime + model), which pure connector/handler unit tests
    # should not pay. Lifespan callers have it warmed by the time this runs.
    from meho_backplane.connectors.vcf_operations.connector import VcfOperationsConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in VROPS_TYPED_OPS:
        handler = getattr(VcfOperationsConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"VcfOperationsConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else VROPS_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"VcfOperationsConnector typed op {op.op_id!r} declares "
                f"group_key={op.group_key!r} but no curated when_to_use exists for "
                f"that key. Add an entry to VROPS_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=VcfOperationsConnector.product,
            version=VcfOperationsConnector.version,
            impl_id=VcfOperationsConnector.impl_id,
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
        "vcf_operations_typed_operations_registered",
        count=len(VROPS_TYPED_OPS),
        product=VcfOperationsConnector.product,
        version=VcfOperationsConnector.version,
        impl_id=VcfOperationsConnector.impl_id,
    )
