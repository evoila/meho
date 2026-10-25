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

Stored result and the slow fresh check (#4922). By default vCenter runs a
fresh health check for every call. While a host of the cluster is in
maintenance mode or powered off, that check can take longer than the
30-second read limit of the shared HTTP client, and the call failed with
``connector_timeout`` and no health data. Now the caller can ask for the
last stored result with ``from_cache: true`` (vSAN's ``fetchFromCache``,
answered in well under a second), the answer carries the time of the check
(``checked_at``, from the summary's ``timestamp``), and when a fresh check
ends with a read timeout the op asks once more for the stored result and
returns it with ``fresh_check: "timed_out"`` and a plain note.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest._mount import VsanMethod
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.vim_body import vim_moref

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "FRESH_CHECK_TIMED_OUT",
    "HOST_VSAN_HEALTH_WHEN_TO_USE",
    "VMWARE_HOST_VSAN_HEALTH_OP",
    "VSAN_QUERY_HEALTH_SUMMARY",
    "build_vsan_query_health_params",
    "fresh_check_timeout_text",
    "host_vsan_health_impl",
]

_log = structlog.get_logger(__name__)

#: ``VsanQueryVcClusterHealthSummary`` on the vSAN health service object of
#: vCenter. The object id is fixed (vendor sample ``vsanapiutils.py``), so
#: the body holds only the method's arguments.
VSAN_QUERY_HEALTH_SUMMARY = VsanMethod(
    mo_type="VsanVcClusterHealthSystem",
    moid="vsan-cluster-health-system",
    method="VsanQueryVcClusterHealthSummary",
)
_CLUSTER_COMPUTE_RESOURCE_MO_TYPE = "ClusterComputeResource"

HOST_VSAN_HEALTH_GROUP_KEY = "vmware-host-vsan-health"

#: ``fresh_check`` in the answer when the fresh check did not finish in time
#: and the answer is the last stored result instead (#4922).
FRESH_CHECK_TIMED_OUT = "timed_out"


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


def build_vsan_query_health_params(
    cluster_moid: str, *, from_cache: bool = False
) -> dict[str, Any]:
    """Build the ``VsanQueryVcClusterHealthSummary`` request body for one cluster.

    The ``vsan-cluster-health-system`` object rides the request path
    (:data:`VSAN_QUERY_HEALTH_SUMMARY`); the body carries the method's
    ``cluster`` argument -- the target cluster's MoRef (a
    ``ClusterComputeResource``), ``_typeName``-annotated per the vim wire
    format (#3103). With *from_cache* the body also carries
    ``fetchFromCache: true``, so vSAN answers with its last stored health
    summary instead of running a fresh check (#4922). The method defines
    ``cluster`` before ``fetchFromCache``, and SOAP needs that order. Every
    other parameter of the method (``includeObjUuids`` / ``fields`` / …) is
    left to the health service's defaults so the read returns the full
    summary.
    """
    body: dict[str, Any] = {
        "cluster": vim_moref(_CLUSTER_COMPUTE_RESOURCE_MO_TYPE, cluster_moid),
    }
    if from_cache:
        body["fetchFromCache"] = True
    return body


def _request_of(exc: httpx.HTTPError) -> httpx.Request | None:
    """The request *exc* was raised for, or ``None`` when it has none."""
    try:
        return exc.request
    except RuntimeError:
        return None


def _read_timeout_seconds(exc: httpx.TimeoutException) -> float | None:
    """The read limit of the request that timed out, or ``None`` when unknown.

    httpx keeps the timeout map of a request in its ``timeout`` extension,
    the same place ``result_connector_timeout`` reads ``configured_timeout``.
    """
    request = _request_of(exc)
    timeout = request.extensions.get("timeout") if request is not None else None
    if isinstance(timeout, Mapping):
        value = timeout.get("read")
        if isinstance(value, (int, float)):
            return float(value)
    return None


def fresh_check_timeout_text(read_timeout: float | None) -> str:
    """The plain text that explains a fresh vSAN check that did not finish in time."""
    limit = f"{read_timeout:g} seconds" if read_timeout is not None else "MEHO's time limit"
    return (
        f"The fresh vSAN check did not finish within {limit}. "
        "A host of the cluster may be down or in maintenance mode."
    )


async def _query_health_summary(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    cluster_moid: str,
    *,
    from_cache: bool,
) -> dict[str, Any]:
    """Send one ``VsanQueryVcClusterHealthSummary`` and return the summary dict.

    An answer without a health summary raises, so a failed read never looks
    like a healthy, empty one (#4441).
    """
    health_result = await connector._post_vsan(
        target,
        VSAN_QUERY_HEALTH_SUMMARY,
        operator=operator,
        json=build_vsan_query_health_params(cluster_moid, from_cache=from_cache),
    )
    summary = _unwrap_value(health_result)
    if not isinstance(summary, dict):
        raise RuntimeError(
            f"vSAN health read for cluster {cluster_moid!r}: vCenter sent no health summary"
        )
    return summary


async def _stored_result_after_timeout(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    cluster_moid: str,
    timeout_exc: httpx.ReadTimeout,
) -> tuple[dict[str, Any], str]:
    """Read the stored health summary once after a fresh check timed out (#4922).

    Returns the summary and the plain text that explains the timeout. When
    the stored result cannot be read either, raises the read timeout again
    with that text, so the dispatcher answers ``connector_timeout`` as
    before and the caller still learns why.
    """
    timeout_text = fresh_check_timeout_text(_read_timeout_seconds(timeout_exc))
    _log.warning(
        "vmware_host_vsan_health_fresh_check_timed_out",
        target=target.name,
        cluster=cluster_moid,
    )
    try:
        summary = await _query_health_summary(
            connector, operator, target, cluster_moid, from_cache=True
        )
    except Exception as cache_exc:
        raise httpx.ReadTimeout(
            f"{timeout_text} The last stored result could not be read either.",
            request=_request_of(timeout_exc),
        ) from cache_exc
    return summary, timeout_text


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

    ``from_cache: true`` asks vSAN for its last stored result instead of a
    fresh check (#4922). Without it, a fresh check that ends with a read
    timeout is followed by exactly one call for the stored result, which is
    returned with ``fresh_check: "timed_out"`` and a plain ``note``. When
    that call fails too, the read timeout is raised again with the same
    plain text, so the dispatcher answers ``connector_timeout`` as before.

    Any other failed call is not caught here (#4441). The dispatcher turns
    it into an error with vCenter's fault type and text; a
    ``ManagedObjectNotFound`` for the cluster becomes MEHO's ``not_found``.
    An answer without a health summary raises too, so a failed read never
    looks like a healthy, empty one.

    Returns ``{"cluster": <moid>, "overall_health": <colour|null>,
    "groups": [...], "checked_at": <time|null>, "from_cache": <bool>}`` plus
    ``fresh_check`` and ``note`` after a timed-out fresh check.
    """
    cluster_moid = params["cluster"]
    from_cache = bool(params.get("from_cache", False))
    timeout_text: str | None = None
    try:
        summary = await _query_health_summary(
            connector, operator, target, cluster_moid, from_cache=from_cache
        )
    except httpx.ReadTimeout as timeout_exc:
        if from_cache:
            raise
        summary, timeout_text = await _stored_result_after_timeout(
            connector, operator, target, cluster_moid, timeout_exc
        )
        from_cache = True
    _log.info(
        "vmware_host_vsan_health_read",
        target=target.name,
        cluster=cluster_moid,
        from_cache=from_cache,
    )
    answer: dict[str, Any] = {
        "cluster": cluster_moid,
        "overall_health": summary.get("overallHealth"),
        "groups": [_parse_vsan_health_group(g) for g in _dict_items(summary.get("groups"))],
        "checked_at": summary.get("timestamp"),
        "from_cache": from_cache,
    }
    if timeout_text is not None:
        answer["fresh_check"] = FRESH_CHECK_TIMED_OUT
        answer["note"] = f"{timeout_text} This is the last stored result."
    return answer


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
        "from_cache": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, return the last health result vSAN has stored instead of "
                "running a fresh check. It answers in well under a second. Use it while "
                "a host of the cluster is down or in maintenance mode, when a fresh check "
                "can take longer than MEHO's 30-second limit. checked_at in the answer "
                "shows how old the result is. Default false: a fresh check."
            ),
        },
    },
    "required": ["cluster"],
    "additionalProperties": False,
    "examples": [{"cluster": "domain-c7"}, {"cluster": "domain-c7", "from_cache": True}],
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
        "checked_at": {
            "type": ["string", "null"],
            "description": (
                "When vSAN ran the health check this result comes from "
                "(``VsanClusterHealthSummary.timestamp``). A stored result keeps the time of "
                "its check, so this shows how old the result is. ``null`` when the summary "
                "has no time."
            ),
        },
        "from_cache": {
            "type": "boolean",
            "description": (
                "True when the result is the last one vSAN had stored: the caller asked for "
                "it with from_cache, or the fresh check did not finish in time."
            ),
        },
        "fresh_check": {
            "type": "string",
            "enum": [FRESH_CHECK_TIMED_OUT],
            "description": (
                "Only present when the caller asked for a fresh check and it did not finish "
                "within MEHO's limit. The answer is then the last stored result."
            ),
        },
        "note": {
            "type": "string",
            "description": (
                "Plain text for the caller, only present together with fresh_check: the "
                "fresh check did not finish, a host may be down or in maintenance mode, "
                "this is the last stored result."
            ),
        },
    },
    "required": ["cluster", "overall_health", "groups", "checked_at", "from_cache"],
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
    "ingest. While a host is down or in maintenance mode, ask for the last "
    "stored result with from_cache: true; it answers in well under a second. "
    "Read-only."
)

VMWARE_HOST_VSAN_HEALTH_OP = VmwareTypedOp(
    op_id="vmware.host.vsan_health",
    handler_attr="host_vsan_health",
    summary="vSAN health of one cluster: the overall colour and each group of health tests.",
    description=(
        "Returns the vSAN health summary of one cluster: the overall colour and each health "
        "group with its tests (id, name, colour, short text), plus the time of the check "
        "(checked_at). By default vSAN runs a fresh check. With from_cache: true it returns "
        "the last stored result in well under a second; use that while a host of the cluster "
        "is down or in maintenance mode. When a fresh check does not finish within MEHO's "
        "30-second limit, the call returns the last stored result with fresh_check: "
        "'timed_out' and a note. When the vSAN call fails, the call returns an error with "
        "vCenter's fault, not an empty answer. Read-only."
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
            "or test is yellow or red. The vCenter REST API cannot give this. When a host "
            "of the cluster is down or in maintenance mode, call with from_cache: true to "
            "get the last stored result fast."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid, e.g. 'domain-c7'.",
            "from_cache": (
                "true to get the last stored result instead of a fresh check. Use it when "
                "a host is down or in maintenance mode, or after a connector_timeout. "
                "Default false."
            ),
        },
        "output_shape": (
            "{cluster, overall_health, groups: [{group_id, group_name, group_health, "
            "tests: [{test_id, test_name, test_health, test_short_description}]}], "
            "checked_at, from_cache}. Colours are the vSAN health colours, such as green, "
            "yellow and red. checked_at is the time of the check the result comes from; "
            "from_cache is true for a stored result. When a fresh check did not finish in "
            "time, the answer is the last stored result and also has fresh_check: "
            "'timed_out' and a plain note. When the vSAN call fails, the call returns an "
            "error with vCenter's fault instead; a cluster id that does not exist gives "
            "not_found."
        ),
        "next_step": (
            "For each group that is not green, report its tests that are not green. Tell "
            "the operator the time of the check (checked_at) when the result is from the "
            "cache. To see the disks of a host, call vmware.host.storage_devices."
        ),
    },
)
