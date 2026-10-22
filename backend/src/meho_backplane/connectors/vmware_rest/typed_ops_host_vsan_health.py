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
on the vSAN health service of vCenter, whose
``VsanVcClusterHealthSystem`` managed object (fixed moId
``vsan-cluster-health-system``) answers
``VsanQueryVcClusterHealthSummary`` at cluster grain -- the ``govc
vsan.health.*`` equivalent. It drives cluster-health triage ('is vSAN
healthy?' / 'which health group is red?').

How the call is sent (#4441): through
:meth:`VmwareRestConnector._post_vsan`, the one vSAN transport. On vCenter
9.x that is VI-JSON ``POST /sdk/vim25/{release}/vsan/VsanVcClusterHealthSystem/
vsan-cluster-health-system/VsanQueryVcClusterHealthSummary``; on vCenter 8.0,
whose VI-JSON API has no vSAN methods, it is SOAP to ``/vsanHealth``. Before
#4441 the path had no ``/vsan`` part, vCenter 9.1 answered HTTP 500
``ManagedObjectNotFound``, and the read hid that behind empty health fields.
Now a failed call raises, so the caller gets an error with vCenter's fault
and never an empty answer that looks healthy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest._mount import VsanMethod
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.vim_body import vim_moref

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "HOST_VSAN_HEALTH_WHEN_TO_USE",
    "VMWARE_HOST_VSAN_HEALTH_OP",
    "VSAN_QUERY_HEALTH_SUMMARY",
    "build_vsan_query_health_params",
    "host_vsan_health_impl",
]

_log = structlog.get_logger(__name__)

#: ``VsanQueryVcClusterHealthSummary`` on the vSAN health service object of
#: vCenter. The object id is fixed (vendor sample ``vsanapiutils.py``), so
#: the body holds only the method's ``cluster`` argument.
VSAN_QUERY_HEALTH_SUMMARY = VsanMethod(
    mo_type="VsanVcClusterHealthSystem",
    moid="vsan-cluster-health-system",
    method="VsanQueryVcClusterHealthSummary",
)
_CLUSTER_COMPUTE_RESOURCE_MO_TYPE = "ClusterComputeResource"

HOST_VSAN_HEALTH_GROUP_KEY = "vmware-host-vsan-health"


def _dict_items(value: Any) -> list[dict[str, Any]]:
    """The dict entries of a vSAN list field, as a list.

    VI-JSON always sends a list. SOAP (vCenter 8.0) has no list marker, so a
    list with one entry comes back as a single dict. A missing or other
    value gives an empty list.
    """
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


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
    :func:`_parse_vsan_health_test`. A missing ``groupTests`` degrades to
    an empty list rather than raising -- the group-level colour is still
    meaningful on its own.
    """
    tests = [_parse_vsan_health_test(t) for t in _dict_items(group.get("groupTests"))]
    return {
        "group_id": group.get("groupId"),
        "group_name": group.get("groupName"),
        "group_health": group.get("groupHealth"),
        "tests": tests,
    }


def build_vsan_query_health_params(cluster_moid: str) -> dict[str, Any]:
    """Build the ``VsanQueryVcClusterHealthSummary`` request body for one cluster.

    The ``vsan-cluster-health-system`` object rides the request path
    (:data:`VSAN_QUERY_HEALTH_SUMMARY`); the body carries the method's
    ``cluster`` argument -- the target cluster's MoRef (a
    ``ClusterComputeResource``), ``_typeName``-annotated per the vim wire
    format (#3103). Every other parameter of the method
    (``includeObjUuids`` / ``fields`` / …) is optional and left to the
    health service's defaults so the read returns the full summary.
    """
    return {
        "cluster": vim_moref(_CLUSTER_COMPUTE_RESOURCE_MO_TYPE, cluster_moid),
    }


async def host_vsan_health_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.host.vsan_health`` -- per-cluster vSAN health.

    Sends ``VsanQueryVcClusterHealthSummary`` to the
    ``vsan-cluster-health-system`` object, scoped to the target cluster's
    MoRef, directly on the connector session (no ``dispatch_child``, no
    ingested descriptor). :meth:`VmwareRestConnector._post_vsan` picks the
    transport: VI-JSON on vCenter 9.x, SOAP to ``/vsanHealth`` on vCenter
    8.0.

    A failed call is not caught here (#4441). The dispatcher turns it into
    an error with vCenter's fault type and text; a ``ManagedObjectNotFound``
    for the cluster becomes MEHO's ``not_found``. An answer without a health
    summary raises too, so a failed read never looks like a healthy, empty
    one.

    Returns ``{"cluster": <moid>, "overall_health": <colour|null>,
    "groups": [...]}``.
    """
    cluster_moid = params["cluster"]
    health_result = await connector._post_vsan(
        target,
        VSAN_QUERY_HEALTH_SUMMARY,
        operator=operator,
        json=build_vsan_query_health_params(cluster_moid),
    )
    summary = _unwrap_value(health_result)
    if not isinstance(summary, dict):
        raise RuntimeError(
            f"vSAN health read for cluster {cluster_moid!r}: vCenter sent no health summary"
        )
    _log.info("vmware_host_vsan_health_read", target=target.name, cluster=cluster_moid)
    return {
        "cluster": cluster_moid,
        "overall_health": summary.get("overallHealth"),
        "groups": [_parse_vsan_health_group(g) for g in _dict_items(summary.get("groups"))],
    }


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
    "examples": [{"cluster": "domain-c7"}],
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
                "vCenter's summary has no overall colour."
            ),
        },
        "groups": {
            "type": "array",
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
            "description": "Health-test groups from ``VsanClusterHealthSummary.groups``.",
        },
    },
    "required": ["cluster", "overall_health", "groups"],
}

#: Curated ``when_to_use`` blurb for the host-vsan-health group.
HOST_VSAN_HEALTH_WHEN_TO_USE = (
    "Use to read per-cluster vSAN health across a vCenter: the "
    "cluster-wide overall_health roll-up colour plus the health-test "
    "groups (each with its own colour and per-check tests: id / name / "
    "colour / short description). The govc vsan.health.* equivalent and "
    "the one read the plain vSphere Automation REST surface cannot "
    "reproduce -- it lives on the vSAN health service of vCenter. "
    "The right op for cluster-health triage: 'is vSAN healthy?' / 'which "
    "health group is red?'. Reads VsanQueryVcClusterHealthSummary "
    "directly on the connector session, so it works with zero catalog "
    "ingest. Read-only."
)

VMWARE_HOST_VSAN_HEALTH_OP = VmwareTypedOp(
    op_id="vmware.host.vsan_health",
    handler_attr="host_vsan_health",
    summary="vSAN health of one cluster: the overall colour and each group of health tests.",
    description=(
        "Returns the vSAN health summary of one cluster: the overall colour and each health "
        "group with its tests (id, name, colour, short text). When the vSAN call fails, the "
        "call returns an error with vCenter's fault, not an empty answer. Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=HOST_VSAN_HEALTH_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "host", "vsan", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to check whether vSAN is healthy on a cluster and which health group "
            "or test is yellow or red. The vCenter REST API cannot give this."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid, e.g. 'domain-c7'.",
        },
        "output_shape": (
            "{cluster, overall_health, groups: [{group_id, group_name, group_health, "
            "tests: [{test_id, test_name, test_health, test_short_description}]}]}. "
            "Colours are the vSAN health colours, such as green, yellow and red. When the "
            "vSAN call fails, the call returns an error with vCenter's fault instead; a "
            "cluster id that does not exist gives not_found."
        ),
        "next_step": (
            "For each group that is not green, report its tests that are not green. To "
            "see the disks of a host, call vmware.host.storage_devices."
        ),
    },
)
