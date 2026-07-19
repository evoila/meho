# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.host.vsan_health`` typed op (#2258).

Re-ships the former ``vmware.composite.host.vsan_health`` read as a
``source_kind="typed"`` bound method on :class:`VmwareRestConnector`, in
the :mod:`~meho_backplane.connectors.vmware_rest.typed_ops` mould
established by ``vmware.host.usage`` (#2257). The op reads per-cluster
vSAN health (health-test groups + overall status) directly on the
connector session -- no ``dispatch_child``, no ingested descriptor -- so
it works on a fresh boot with **zero catalog ingest**.

vSAN health is the one read the plain vSphere Automation REST surface
cannot reproduce: the health-test-group / overall-status roll-up lives
on the dedicated ``/vsanHealth`` vmomi service, whose
``VsanVcClusterHealthSystem`` managed object (singleton moId
``vsan-cluster-health-system``) answers
``VsanQueryVcClusterHealthSummary`` at cluster grain -- the ``govc
vsan.health.*`` equivalent. It drives cluster-health triage ('is vSAN
healthy?' / 'which health group is red?').

The request-building + parse logic is carried over verbatim from the
composite; only the dispatch mechanism changed (composite
``_read_sub_op`` -> the same ``mount_op_path`` + ``_post_json`` call
``host.usage`` issues).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "HOST_VSAN_HEALTH_WHEN_TO_USE",
    "VMWARE_HOST_VSAN_HEALTH_OP",
    "build_vsan_query_health_params",
    "host_vsan_health_impl",
]

_log = structlog.get_logger(__name__)

# VsanQueryVcClusterHealthSummary against the singleton
# ``vsan-cluster-health-system`` moId (carried in the path, so the body
# is only the method's ``cluster`` argument). Spec-relative; mounted
# onto /api or /rest per-target by mount_op_path.
_VSAN_QUERY_HEALTH_PATH = (
    "/VsanVcClusterHealthSystem/vsan-cluster-health-system/VsanQueryVcClusterHealthSummary"
)
_CLUSTER_COMPUTE_RESOURCE_MO_TYPE = "ClusterComputeResource"

HOST_VSAN_HEALTH_GROUP_KEY = "vmware-host-vsan-health"


def _parse_vsan_health_test(test: dict[str, Any]) -> dict[str, Any]:
    """Flatten one WS-API ``VsanClusterHealthTest`` into the operator-facing row.

    A single health check inside a group: ``testId`` / ``testName`` /
    ``testHealth`` (the ``green`` / ``yellow`` / ``red`` colour) plus the
    short human-readable description. The vSAN health-service owns the
    inner colour vocabulary; the op passes it through verbatim.
    """
    return {
        "test_id": test.get("testId"),
        "test_name": test.get("testName"),
        "test_health": test.get("testHealth"),
        "test_short_description": test.get("testShortDescription"),
    }


def _parse_vsan_health_group(group: dict[str, Any]) -> dict[str, Any]:
    """Flatten one WS-API ``VsanClusterHealthGroup`` into the operator-facing row.

    A group buckets related health checks (network, physical disk,
    cluster, data, …). ``groupHealth`` is the group-level roll-up
    colour; ``groupTests`` is the per-check list flattened via
    :func:`_parse_vsan_health_test`. A missing / non-list ``groupTests``
    degrades to an empty list rather than raising -- the group-level
    colour is still meaningful on its own.
    """
    raw_tests = group.get("groupTests")
    tests = (
        [_parse_vsan_health_test(t) for t in raw_tests if isinstance(t, dict)]
        if isinstance(raw_tests, list)
        else []
    )
    return {
        "group_id": group.get("groupId"),
        "group_name": group.get("groupName"),
        "group_health": group.get("groupHealth"),
        "tests": tests,
    }


def build_vsan_query_health_params(cluster_moid: str) -> dict[str, Any]:
    """Build the ``VsanQueryVcClusterHealthSummary`` request body for one cluster.

    The ``vsan-cluster-health-system`` singleton moId rides the request
    path (:data:`_VSAN_QUERY_HEALTH_PATH`); the body carries the method's
    ``cluster`` argument -- the target cluster's MoRef (a
    ``ClusterComputeResource``). Every other parameter of the method
    (``includeObjUuids`` / ``fields`` / …) is optional and left to the
    health service's defaults so the read returns the full summary.
    """
    return {
        "cluster": {"type": _CLUSTER_COMPUTE_RESOURCE_MO_TYPE, "value": cluster_moid},
    }


async def host_vsan_health_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.host.vsan_health`` -- per-cluster vSAN health.

    Reads, directly on the connector session (no ``dispatch_child``, no
    ingested descriptor):

    1. ``POST .../VsanVcClusterHealthSystem/vsan-cluster-health-system/
       VsanQueryVcClusterHealthSummary`` (mounted) against the
       ``vsan-cluster-health-system`` singleton, scoped to the target
       cluster's MoRef. Best-effort: the cluster is already identified by
       the ``cluster`` param, so when the health-service read errors (a
       cluster with vSAN disabled, a ``/vsanHealth`` endpoint that rejects
       the vi-json call, a transient auth expiry) the summary is returned
       with ``groups`` / ``overall_health`` set to ``null`` and a
       ``read_note`` recording why, rather than propagating the transport
       error.

    The call routes through
    :meth:`VmwareRestConnector._post_vmomi_json`, which mounts the vmomi
    method on the documented VI-JSON base ``/sdk/vim25/{release}`` (with a
    single ``/api`` fallback) so ``VsanQueryVcClusterHealthSummary``
    resolves on vCenter 8.0.x instead of 404ing (#2466); on legacy /
    vcsim targets it stays on the ``/rest`` mount.

    Returns ``{"cluster": <moid>, "overall_health": <colour|null>,
    "groups": [...]}``.
    """
    cluster_moid = params["cluster"]

    out: dict[str, Any] = {"cluster": cluster_moid}
    try:
        health_result = await connector._post_vmomi_json(
            target,
            _VSAN_QUERY_HEALTH_PATH,
            operator=operator,
            json=build_vsan_query_health_params(cluster_moid),
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        out["overall_health"] = None
        out["groups"] = None
        out["read_note"] = (
            f"vsan health-service read skipped: VsanQueryVcClusterHealthSummary for "
            f"cluster {cluster_moid!r} failed with {type(exc).__name__}: {exc}"
        )
        return out
    summary = _unwrap_value(health_result)
    raw_groups = summary.get("groups") if isinstance(summary, dict) else None
    overall = summary.get("overallHealth") if isinstance(summary, dict) else None
    out["overall_health"] = overall
    out["groups"] = (
        [_parse_vsan_health_group(g) for g in raw_groups if isinstance(g, dict)]
        if isinstance(raw_groups, list)
        else []
    )
    _log.info("vmware_host_vsan_health_read", target=target.name, cluster=cluster_moid)
    return out


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Managed-object ID of the vSAN cluster to check (e.g. "
                "'domain-c123'). The op scopes the health-service query to "
                "this ClusterComputeResource MoRef."
            ),
        },
    },
    "required": ["cluster"],
    "additionalProperties": False,
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "string",
            "description": "Managed-object ID of the cluster the summary was read for.",
        },
        "overall_health": {
            "type": ["string", "null"],
            "description": (
                "Cluster-wide vSAN health roll-up colour from "
                "``VsanClusterHealthSummary.overallHealth``; ``null`` when "
                "the best-effort health-service read was skipped (see "
                "``read_note``)."
            ),
        },
        "groups": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {
                    "group_id": {
                        "type": ["string", "null"],
                        "description": "Health-group identifier (``groupId``).",
                    },
                    "group_name": {
                        "type": ["string", "null"],
                        "description": "Human-readable group name (``groupName``).",
                    },
                    "group_health": {
                        "type": ["string", "null"],
                        "description": "Group-level roll-up colour (``groupHealth``).",
                    },
                    "tests": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "test_id": {
                                    "type": ["string", "null"],
                                    "description": "Health-test identifier (``testId``).",
                                },
                                "test_name": {
                                    "type": ["string", "null"],
                                    "description": "Human-readable test name (``testName``).",
                                },
                                "test_health": {
                                    "type": ["string", "null"],
                                    "description": "Per-test colour (``testHealth``).",
                                },
                                "test_short_description": {
                                    "type": ["string", "null"],
                                    "description": (
                                        "Short human-readable description "
                                        "(``testShortDescription``)."
                                    ),
                                },
                            },
                            "required": [],
                        },
                        "description": (
                            "Individual health checks in this group from ``groupTests``."
                        ),
                    },
                },
                "required": ["tests"],
            },
            "description": (
                "Health-test groups from ``VsanClusterHealthSummary.groups``; "
                "``null`` when the best-effort health-service read was skipped "
                "(see ``read_note``)."
            ),
        },
        "read_note": {
            "type": "string",
            "description": (
                "Present only when the health-service read was skipped; "
                "records the failing method, its status, and the underlying "
                "error."
            ),
        },
    },
    "required": ["cluster"],
}

#: Curated ``when_to_use`` blurb for the host-vsan-health group.
HOST_VSAN_HEALTH_WHEN_TO_USE = (
    "Use to read per-cluster vSAN health across a vCenter: the "
    "cluster-wide overall_health roll-up colour plus the health-test "
    "groups (each with its own colour and per-check tests: id / name / "
    "colour / short description). The govc vsan.health.* equivalent and "
    "the one read the plain vSphere Automation REST surface cannot "
    "reproduce -- it lives on the dedicated /vsanHealth vmomi endpoint. "
    "The right op for cluster-health triage: 'is vSAN healthy?' / 'which "
    "health group is red?'. Reads VsanQueryVcClusterHealthSummary "
    "directly on the connector session, so it works with zero catalog "
    "ingest. Read-only."
)

VMWARE_HOST_VSAN_HEALTH_OP = VmwareTypedOp(
    op_id="vmware.host.vsan_health",
    handler_attr="host_vsan_health",
    summary="Per cluster, vSAN health-test groups + overall health status.",
    description=(
        "Returns the cluster-wide vSAN overall_health colour plus the "
        "health-test groups list (each group with its own roll-up colour + "
        "per-check tests: id / name / colour / short description) for one "
        "cluster. Queries VsanQueryVcClusterHealthSummary on the "
        "'vsan-cluster-health-system' singleton scoped to the target "
        "cluster's ClusterComputeResource MoRef, directly on the connector "
        "session, so it works with zero catalog ingest -- vSAN health is "
        "the one read the plain vSphere Automation REST surface cannot "
        "reproduce (it lives on the dedicated /vsanHealth vmomi endpoint), "
        "making this the 'govc vsan.health.*' equivalent for cluster-health "
        "triage. Best-effort: a cluster with vSAN disabled or a rejected "
        "health call returns null groups / overall_health with a read_note "
        "rather than failing. safety_level=safe, read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=HOST_VSAN_HEALTH_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "host", "vsan", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator asks whether vSAN is healthy on a cluster, "
            "which vSAN health group is red/yellow, or wants the vSAN "
            "health-test roll-up -- data the plain vCenter REST surface cannot "
            "supply. Requires the target cluster's MoRef."
        ),
        "parameter_hints": {
            "cluster": "Managed-object ID of the vSAN cluster (e.g. 'domain-c123').",
        },
        "output_shape": (
            "{cluster, overall_health, groups: [{group_id, group_name, "
            "group_health, tests: [{test_id, test_name, test_health, "
            "test_short_description}]}]}. When the best-effort health-service "
            "read failed, overall_health/groups are null and the payload "
            "carries a read_note."
        ),
    },
)
