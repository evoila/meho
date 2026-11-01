# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.resource_pool.list`` typed op -- the resource pools of one vCenter.

Reads the vSphere Automation REST API on the connector's own session, so it
works on a fresh install with no catalog ingest:

1. ``GET /vcenter/resource-pool`` lists every resource pool (moid and name).
   When the caller passes ``cluster``, ``host`` or ``parent_pool``, a second
   list call with the matching filter picks the pools to return. The
   optional ``name`` pattern is applied here, in MEHO. A failed list fails
   the operation.
2. ``GET /vcenter/resource-pool/{resource_pool}`` for every pool gives the
   CPU and memory settings and the ids of the child pools. The REST answer
   has no parent field, so the parent pool of a pool is the pool that names
   it as a child. A failed read of a returned pool leaves its settings
   ``null``; ``read_note`` says why.
3. A pool with no parent pool is the root pool of a cluster or of a
   standalone host. Only then MEHO reads the clusters
   (``GET /vcenter/cluster`` and ``GET /vcenter/cluster/{cluster}``, whose
   ``resource_pool`` is the root pool) and, for roots that are still open,
   the standalone hosts and their pools.

The rows are flat, so a long list rides a result handle like any other list.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
import structlog

from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "CLUSTER_INFO_PATH",
    "CLUSTER_LIST_PATH",
    "HOST_LIST_PATH",
    "RESOURCE_POOL_FILTER_PARAMS",
    "RESOURCE_POOL_INFO_PATH",
    "RESOURCE_POOL_LIST_GROUP_KEY",
    "RESOURCE_POOL_LIST_PATH",
    "RESOURCE_POOL_LIST_WHEN_TO_USE",
    "VMWARE_RESOURCE_POOL_LIST_OP",
    "resource_pool_list_impl",
]

#: Spec-relative REST paths; ``mount_op_path`` puts them on ``/api`` or ``/rest``.
RESOURCE_POOL_LIST_PATH = "/vcenter/resource-pool"
RESOURCE_POOL_INFO_PATH = "/vcenter/resource-pool/{resource_pool}"
CLUSTER_LIST_PATH = "/vcenter/cluster"
CLUSTER_INFO_PATH = "/vcenter/cluster/{cluster}"
HOST_LIST_PATH = "/vcenter/host"

#: Operation parameter -> the FilterSpec field of ``GET /vcenter/resource-pool``.
#: Sent in the legacy ``filter.`` form; ``adapt_op_query`` drops the prefix on ``/api``.
RESOURCE_POOL_FILTER_PARAMS: dict[str, str] = {
    "cluster": "clusters",
    "host": "hosts",
    "parent_pool": "parent_resource_pools",
}

RESOURCE_POOL_LIST_GROUP_KEY = "vmware-resource-pools"

RESOURCE_POOL_LIST_WHEN_TO_USE = (
    "Use to see the resource pools of a vCenter: their names and ids, their parent "
    "(a cluster, a standalone host or another pool) and their CPU and memory "
    "settings (reservation, limit, shares). Start here when you need a resource "
    "pool id for another call. Read-only."
)

_ALLOCATIONS = (("cpu", "cpu_allocation", "mhz"), ("memory", "memory_allocation", "mb"))


def _log() -> Any:
    """Get the logger per call, so ``structlog.testing.capture_logs`` sees every event."""
    return structlog.get_logger(__name__)


def _name_matches(name: Any, pattern: str | None) -> bool:
    """Match *name* against a ``*`` / ``?`` pattern; letter case does not matter."""
    if pattern is None:
        return True
    if not isinstance(name, str):
        return False
    return fnmatch.fnmatchcase(name.casefold(), pattern.casefold())


def _failure(path: str, exc: Exception) -> str:
    return f"GET {path} failed with {type(exc).__name__}: {exc}"


async def _get(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    path: str,
    query: dict[str, Any] | None = None,
) -> Any:
    """GET one REST path on the target's mount and drop a legacy ``value`` envelope."""
    mounted = await connector.mount_op_path(target, path, operator)
    wire_query = await connector.adapt_op_query(target, query, operator)
    return _unwrap_value(
        await connector._get_json(target, mounted, operator=operator, params=wire_query)
    )


async def _get_ids(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    path: str,
    id_field: str,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GET one REST list and map each entry's *id_field* to its ``name``; raise on a non-list."""
    answer = await _get(connector, operator, target, path, query)
    if not isinstance(answer, list):
        raise RuntimeError(
            f"vmware.resource_pool.list: expected a list from GET {path!r}, "
            f"got {type(answer).__name__}"
        )
    return {
        entry[id_field]: entry.get("name")
        for entry in answer
        if isinstance(entry, dict) and isinstance(entry.get(id_field), str)
    }


def _pool_path(pool_id: str) -> str:
    return RESOURCE_POOL_INFO_PATH.format(resource_pool=quote(pool_id, safe=""))


def _cluster_path(cluster_id: str) -> str:
    return CLUSTER_INFO_PATH.format(cluster=quote(cluster_id, safe=""))


async def _read_infos(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    pool_ids: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Read the info of every pool. Returns ``(infos, failures)`` keyed by pool id."""
    infos: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    for pool_id in pool_ids:
        try:
            info = await _get(connector, operator, target, _pool_path(pool_id))
        except (httpx.HTTPError, RuntimeError) as exc:
            failures[pool_id] = _failure(RESOURCE_POOL_INFO_PATH, exc)
            continue
        if isinstance(info, dict):
            infos[pool_id] = info
        else:
            failures[pool_id] = f"GET {RESOURCE_POOL_INFO_PATH} gave {type(info).__name__}"
    return infos, failures


def _parent_pools(infos: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Child pool id -> parent pool id, from each pool's ``resource_pools`` list."""
    parents: dict[str, str] = {}
    for pool_id, info in infos.items():
        children = info.get("resource_pools")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, str):
                    parents[child] = pool_id
    return parents


async def _standalone_host_roots(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    open_roots: set[str],
    cluster_ids: list[str],
    owners: dict[str, tuple[str, str]],
) -> None:
    """Add root pool id -> ``("host", host id)`` to ``owners`` for the standalone hosts.

    Writes into ``owners`` as it goes, so a failed call keeps the hosts found before it.
    """
    hosts = await _get_ids(connector, operator, target, HOST_LIST_PATH, "host")
    clustered: dict[str, Any] = {}
    if cluster_ids:
        query = {"filter.clusters": cluster_ids}
        clustered = await _get_ids(connector, operator, target, HOST_LIST_PATH, "host", query)
    for host_id in hosts.keys() - clustered.keys():
        query = {"filter.hosts": [host_id]}
        pools = await _get_ids(
            connector, operator, target, RESOURCE_POOL_LIST_PATH, "resource_pool", query
        )
        for pool_id in pools.keys() & open_roots:
            owners[pool_id] = ("host", host_id)


async def _root_owners(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    open_roots: set[str],
    *,
    check_hosts: bool,
) -> tuple[dict[str, tuple[str, str]], str | None]:
    """Root pool id -> ``(parent_type, parent_id)`` for the clusters and standalone hosts.

    Returns ``(owners, failure)``. A failed cluster or host call stops the lookup
    but keeps every parent found before it; ``failure`` then names the error.
    """
    owners: dict[str, tuple[str, str]] = {}
    try:
        clusters = await _get_ids(connector, operator, target, CLUSTER_LIST_PATH, "cluster")
        for cluster_id in clusters:
            info = await _get(connector, operator, target, _cluster_path(cluster_id))
            root = info.get("resource_pool") if isinstance(info, dict) else None
            if isinstance(root, str) and root in open_roots:
                owners[root] = ("cluster", cluster_id)
        still_open = open_roots - owners.keys()
        if still_open and check_hosts:
            await _standalone_host_roots(
                connector, operator, target, still_open, list(clusters), owners
            )
    except (httpx.HTTPError, RuntimeError) as exc:
        return owners, f"parent not read: {type(exc).__name__}: {exc}"
    return owners, None


def _settings(info: dict[str, Any] | None) -> dict[str, Any]:
    """The flat CPU and memory fields of one pool; ``None`` where the answer has no value."""
    out: dict[str, Any] = {}
    for prefix, key, unit in _ALLOCATIONS:
        raw = info.get(key) if info is not None else None
        alloc: dict[str, Any] = raw if isinstance(raw, dict) else {}
        raw_shares = alloc.get("shares")
        shares: dict[str, Any] = raw_shares if isinstance(raw_shares, dict) else {}
        out[f"{prefix}_reservation_{unit}"] = _int(alloc.get("reservation"))
        out[f"{prefix}_limit_{unit}"] = _int(alloc.get("limit"))
        expandable = alloc.get("expandable_reservation")
        out[f"{prefix}_expandable_reservation"] = (
            expandable if isinstance(expandable, bool) else None
        )
        level = shares.get("level")
        out[f"{prefix}_shares_level"] = level if isinstance(level, str) else None
        out[f"{prefix}_shares"] = _int(shares.get("shares"))
    return out


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parent(
    pool_id: str,
    parents: dict[str, str],
    owners: dict[str, tuple[str, str]],
) -> tuple[str | None, str | None]:
    if pool_id in parents:
        return "resource_pool", parents[pool_id]
    return owners.get(pool_id, (None, None))


async def _resolve_owners(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    open_roots: set[str],
    info_failures: dict[str, str],
) -> tuple[dict[str, tuple[str, str]], str | None]:
    """Find the cluster or host of each open root. Returns ``(owners, note)``."""
    if not open_roots:
        return {}, None
    # With a failed pool read a pool can look like a root by mistake, so
    # only the exact cluster check runs then.
    owners, failure = await _root_owners(
        connector, operator, target, open_roots, check_hosts=not info_failures
    )
    if failure is not None:
        return owners, failure
    if info_failures:
        return owners, f"parent not known: {len(info_failures)} pool(s) could not be read"
    return owners, "parent not found: no pool, cluster or standalone host holds this pool"


async def resource_pool_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.resource_pool.list``. Returns ``{"resource_pools": [...]}``."""
    raw_pattern = params.get("name")
    pattern = raw_pattern if isinstance(raw_pattern, str) else None
    rest_filter = {
        f"filter.{field}": [params[param]]
        for param, field in RESOURCE_POOL_FILTER_PARAMS.items()
        if isinstance(params.get(param), str)
    }
    every_pool = await _get_ids(
        connector, operator, target, RESOURCE_POOL_LIST_PATH, "resource_pool"
    )
    scoped = (
        await _get_ids(
            connector, operator, target, RESOURCE_POOL_LIST_PATH, "resource_pool", rest_filter
        )
        if rest_filter
        else every_pool
    )
    matches = {pid: name for pid, name in scoped.items() if _name_matches(name, pattern)}
    rows: list[dict[str, Any]] = []
    if matches:
        infos, failures = await _read_infos(
            connector, operator, target, [*every_pool, *(m for m in matches if m not in every_pool)]
        )
        parents = _parent_pools(infos)
        open_roots = set(matches) - parents.keys()
        owners, owner_note = await _resolve_owners(
            connector, operator, target, open_roots, failures
        )
        for pool_id, name in matches.items():
            parent_type, parent_id = _parent(pool_id, parents, owners)
            notes = [f"settings not read: {failures[pool_id]}"] if pool_id in failures else []
            if parent_type is None and owner_note is not None:
                notes.append(owner_note)
            rows.append(
                {
                    "id": pool_id,
                    "name": name if isinstance(name, str) else None,
                    "parent_type": parent_type,
                    "parent_id": parent_id,
                    **_settings(infos.get(pool_id)),
                    "read_note": "; ".join(notes) if notes else None,
                }
            )
    _log().info(
        "vmware_resource_pool_list_read",
        target=target.name,
        resource_pool_count=len(rows),
        filtered=bool(rest_filter) or pattern is not None,
    )
    return {"resource_pools": rows}


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_MOID_PARAM: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": 128}


def _allocation_properties(prefix: str, what: str, unit: str, unit_name: str) -> dict[str, Any]:
    return {
        f"{prefix}_reservation_{unit}": {
            "type": ["integer", "null"],
            "description": f"{what} kept free for this pool, in {unit_name}.",
        },
        f"{prefix}_limit_{unit}": {
            "type": ["integer", "null"],
            "description": f"Most {what} this pool may use, in {unit_name}. -1 means no limit.",
        },
        f"{prefix}_expandable_reservation": {
            "type": ["boolean", "null"],
            "description": f"True when the {what} reservation may grow from the parent.",
        },
        f"{prefix}_shares_level": {
            "type": ["string", "null"],
            "description": f"{what} shares level: LOW, NORMAL, HIGH or CUSTOM.",
        },
        f"{prefix}_shares": {
            "type": ["integer", "null"],
            "description": f"Number of {what} shares. Set by hand only when the level is CUSTOM.",
        },
    }


_RESOURCE_POOL_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Resource pool moid, for example 'resgroup-8'."},
        "name": {"type": ["string", "null"], "description": "Resource pool name."},
        "parent_type": {
            "type": ["string", "null"],
            "enum": ["resource_pool", "cluster", "host", None],
            "description": (
                "What holds this pool: another resource_pool, a cluster or a standalone "
                "host. cluster or host means this is the hidden root pool. Null when not "
                "known; read_note says why."
            ),
        },
        "parent_id": {
            "type": ["string", "null"],
            "description": "Moid of the parent pool, cluster or host. Null when not known.",
        },
        **_allocation_properties("cpu", "CPU", "mhz", "MHz"),
        **_allocation_properties("memory", "memory", "mb", "MB"),
        "read_note": {
            "type": ["string", "null"],
            "description": "Why the settings or the parent are null. Null when all was read.",
        },
    },
    "required": ["id", "name", "parent_type", "parent_id", "read_note"],
}

VMWARE_RESOURCE_POOL_LIST_OP = VmwareTypedOp(
    op_id="vmware.resource_pool.list",
    handler_attr="resource_pool_list",
    summary="List the resource pools of a vCenter, with parent and CPU and memory settings.",
    description=(
        "Reads GET /vcenter/resource-pool and the details of each pool. Returns one row "
        "per pool: id (moid), name, parent (pool, cluster or host) and the CPU and memory "
        "reservation, limit and shares. Filters: cluster, host, parent pool, name pattern. "
        "vCenter only. Needs no catalog ingest. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "cluster": {
                **_MOID_PARAM,
                "description": (
                    "Optional. Cluster moid, for example 'domain-c8'. Show only the "
                    "pools in this cluster."
                ),
            },
            "host": {
                **_MOID_PARAM,
                "description": (
                    "Optional. ESXi host moid, for example 'host-21'. Show only the "
                    "pools on this host."
                ),
            },
            "parent_pool": {
                **_MOID_PARAM,
                "description": (
                    "Optional. Resource pool moid, for example 'resgroup-8'. Show only "
                    "the pools inside this pool."
                ),
            },
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": (
                    "Optional. Show only pools whose name matches this pattern. Use * "
                    "for any text and ? for one character. Letter case does not matter."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{}, {"cluster": "<cluster-moid>", "name": "<pool-name>*"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "resource_pools": {
                "type": "array",
                "description": "One row per resource pool that matches the filters.",
                "items": _RESOURCE_POOL_ROW_SCHEMA,
            }
        },
        "required": ["resource_pools"],
    },
    group_key=RESOURCE_POOL_LIST_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "resource-pool", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which resource pools a vCenter has, to get a pool id for "
            "another call, or to check the CPU and memory reservation, limit and "
            "shares of a pool."
        ),
        "parameter_hints": {
            "cluster": "A cluster moid such as 'domain-c8'; omit for every cluster.",
            "host": "A host moid such as 'host-21'; omit for every host.",
            "parent_pool": "A pool moid such as 'resgroup-8'; omit for every pool.",
            "name": "Omit it for every pool, or pass a pattern such as 'prod*'.",
        },
        "output_shape": (
            "{resource_pools: [{id, name, parent_type, parent_id, cpu_reservation_mhz, "
            "cpu_limit_mhz, cpu_expandable_reservation, cpu_shares_level, cpu_shares, "
            "memory_reservation_mb, memory_limit_mb, memory_expandable_reservation, "
            "memory_shares_level, memory_shares, read_note}]}. A limit of -1 means no "
            "limit. parent_type cluster or host marks the hidden root pool, often "
            "named 'Resources'. A null field has a read_note."
        ),
        "next_step": (
            "Use id where another call needs a resource pool moid. To add a pool, call "
            "vmware.composite.resource_pool.create; to remove an empty one, call "
            "vmware.composite.resource_pool.delete."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_RESOURCE_POOL_LIST_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    RESOURCE_POOL_LIST_GROUP_KEY: RESOURCE_POOL_LIST_WHEN_TO_USE
}
