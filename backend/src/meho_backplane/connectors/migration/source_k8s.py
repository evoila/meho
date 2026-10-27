# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.migration.inventory`` -- the workloads and nodes of one Kubernetes cluster.

Child reads, all on the Kubernetes connector (``k8s-1.x``):

1. ``k8s.node.list`` -- one server record per node. A node carries its BIOS
   UUID (``nodeInfo.systemUUID``) as identity hint, so a node that is also
   a VM matches the VM's record from the hypervisor and the machine is not
   counted twice when the inventories are merged.
2. ``k8s.workload.list`` for Deployments, StatefulSets and DaemonSets, all
   namespaces, page by page -- one ``kubernetes_workload`` record each.
3. ``k8s.persistentvolumeclaim.list`` -- the size and storage class of each
   claim a workload mounts.
4. ``k8s.pod.list`` per workload with its pod selector -- the nodes its pods
   run on. The workload's ``nodes`` field holds the ``source_id`` of those
   node records, so a workload links to its nodes.

CPU and memory of a workload are the sums over its containers, per
replica. A sum is only given when every container sets the value;
otherwise it is unknown, because a sum of some containers would look
smaller than the workload is.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from meho_backplane.connectors.migration.adapter_kit import (
    ChildReadError,
    InventoryBuilder,
    gather_limited,
    read_child,
    record_with_reasons,
    rows_of,
    store_inventory,
    target_name,
)
from meho_backplane.connectors.migration.contracts import (
    IdentityHints,
    KubernetesWorkloadRecord,
    MissingReason,
    OperatingSystem,
    ServerRecord,
    WorkloadVolume,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.operations.composite import DispatchChild

__all__ = [
    "K8S_CONNECTOR_ID",
    "K8S_INVENTORY_OP_ID",
    "build_node_record",
    "build_workload_record",
    "k8s_migration_inventory",
    "parse_quantity",
]

K8S_CONNECTOR_ID: Final[str] = "k8s-1.x"
K8S_INVENTORY_OP_ID: Final[str] = "k8s.migration.inventory"
WORKLOAD_KINDS: Final[tuple[str, ...]] = ("Deployment", "StatefulSet", "DaemonSet")
_PAGE: Final[int] = 500
#: Most pages one list may take; a cluster bigger than this is cut and says so.
_MAX_PAGES: Final[int] = 40

_SUFFIX: Final[dict[str, float]] = {
    "": 1,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
}
_FAMILIES: Final[dict[str, Literal["windows", "linux", "other"]]] = {
    "linux": "linux",
    "windows": "windows",
}
_QUANTITY: Final[re.Pattern[str]] = re.compile(
    r"^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)([a-zA-Z]*)$"
)


def parse_quantity(text: Any) -> float | None:
    """A Kubernetes quantity (``250m``, ``2``, ``512Mi``, ``1e9``) as a plain number."""
    match = _QUANTITY.match(str(text).strip()) if text is not None else None
    if not match or match.group(2) not in _SUFFIX:
        return None
    return float(match.group(1)) * _SUFFIX[match.group(2)]


def _sum_over_containers(
    containers: list[dict[str, Any]], part: str, resource: str
) -> float | None:
    values = [parse_quantity((c.get(part) or {}).get(resource)) for c in containers]
    if not values or any(v is None for v in values):
        return None
    return sum(v for v in values if v is not None)


def node_source_id(cluster: str, node: str) -> str:
    """``source_id`` of a node record."""
    return f"k8s:{cluster}:node:{node}"


def build_node_record(row: dict[str, Any], *, cluster: str, collected_at: datetime) -> ServerRecord:
    """One cluster node as a server record."""
    reasons: dict[str, MissingReason] = {
        "utilization": "not_measured",
        "disks": "not_supported",
        "nics": "not_supported",
        "snapshot_count": "not_supported",
        "memory_assigned_mb": "not_supported",
        "boot_type": "not_supported",
        "secure_boot": "not_supported",
    }
    cpu = parse_quantity(row.get("cpu_capacity"))
    memory = parse_quantity(row.get("memory_capacity"))
    family = _FAMILIES.get(str(row.get("os") or "").lower(), "other") if row.get("os") else None
    name = str(row["name"])
    fields: dict[str, Any] = {
        "source_id": node_source_id(cluster, name),
        "collected_at": collected_at,
        "identity_hints": IdentityHints(bios_uuid=row.get("system_uuid") or None, host_name=name),
        "name": name,
        "cores": int(cpu) if cpu and cpu >= 1 else None,
        "memory_configured_mb": int(memory // 1024**2) if memory and memory >= 1024**2 else None,
        "os": OperatingSystem(
            name=row.get("os_image") or None,
            family=family,
            architecture=row.get("architecture") or None,
        ),
        "power_state": "on" if row.get("status") == "Ready" else None,
    }
    return record_with_reasons(ServerRecord, fields, reasons=reasons)


def _volumes(
    row: dict[str, Any], claims: dict[tuple[str, str], dict[str, Any]] | MissingReason
) -> tuple[list[WorkloadVolume], dict[str, MissingReason]]:
    namespace = str(row.get("namespace") or "")
    volumes: list[WorkloadVolume] = []
    reasons: dict[str, MissingReason] = {}
    for volume in row.get("volumes") or []:
        index = len(volumes)
        size = None
        storage_class = None
        claim_name = volume.get("claim_name")
        if claim_name and isinstance(claims, dict):
            claim = claims.get((namespace, claim_name), {})
            capacity = parse_quantity(claim.get("capacity"))
            size = round(capacity / 1024**3, 2) if capacity is not None else None
            storage_class = claim.get("storage_class")
        elif claim_name:
            reasons[f"volumes.{index}"] = claims if isinstance(claims, str) else "collection_failed"
        else:
            reasons[f"volumes.{index}"] = "not_supported"
        volumes.append(
            WorkloadVolume(
                name=str(volume.get("name") or "volume"),
                type=str(volume.get("type") or "other"),
                size_gb=size,
                storage_class=storage_class,
            )
        )
    for template in row.get("volume_claim_templates") or []:
        request = parse_quantity(template.get("storage_request"))
        volumes.append(
            WorkloadVolume(
                name=str(template.get("name") or "claim"),
                type="volumeClaimTemplate",
                size_gb=round(request / 1024**3, 2) if request is not None else None,
                storage_class=template.get("storage_class"),
            )
        )
    return volumes, reasons


def build_workload_record(
    row: dict[str, Any],
    *,
    cluster: str,
    claims: dict[tuple[str, str], dict[str, Any]] | MissingReason,
    nodes: list[str] | MissingReason,
    collected_at: datetime,
) -> KubernetesWorkloadRecord:
    """One Deployment, StatefulSet or DaemonSet as a workload record."""
    containers = [c for c in row.get("containers") or [] if isinstance(c, dict)]
    volumes, reasons = _volumes(row, claims)
    if isinstance(nodes, str):
        reasons["nodes"] = nodes
    cpu_request = _sum_over_containers(containers, "requests", "cpu")
    cpu_limit = _sum_over_containers(containers, "limits", "cpu")
    mem_request = _sum_over_containers(containers, "requests", "memory")
    mem_limit = _sum_over_containers(containers, "limits", "memory")
    namespace = str(row.get("namespace"))
    fields: dict[str, Any] = {
        "source_id": f"k8s:{cluster}:{namespace}:{row.get('kind')}:{row.get('name')}",
        "collected_at": collected_at,
        "cluster": cluster,
        "namespace": namespace,
        "workload_kind": str(row.get("kind")),
        "name": str(row.get("name")),
        "replicas": row.get("replicas"),
        "cpu_request_millicores": round(cpu_request * 1000, 1) if cpu_request is not None else None,
        "cpu_limit_millicores": round(cpu_limit * 1000, 1) if cpu_limit is not None else None,
        "memory_request_mb": round(mem_request / 1024**2, 1) if mem_request is not None else None,
        "memory_limit_mb": round(mem_limit / 1024**2, 1) if mem_limit is not None else None,
        "images": sorted({str(c["image"]) for c in containers if c.get("image")}),
        "volumes": volumes,
        "nodes": nodes if isinstance(nodes, list) else None,
    }
    return record_with_reasons(KubernetesWorkloadRecord, fields, reasons=reasons)


async def _list_kind(dispatch_child: DispatchChild, kind: str) -> tuple[list[dict[str, Any]], bool]:
    """Every workload of *kind*, page by page. The flag is ``True`` when the list was cut."""
    rows: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {"kind": kind, "all_namespaces": True, "limit": _PAGE}
        if token:
            params["continue_token"] = token
        answer = await read_child(
            dispatch_child, connector_id=K8S_CONNECTOR_ID, op_id="k8s.workload.list", params=params
        )
        rows.extend(rows_of(answer))
        token = answer.get("next_continue") if isinstance(answer, dict) else None
        if not token:
            return rows, False
    return rows, True


async def _pod_nodes(
    dispatch_child: DispatchChild, cluster: str, row: dict[str, Any]
) -> list[str] | MissingReason:
    selector = row.get("pod_selector")
    if not selector:
        return "not_reported_by_source"
    try:
        answer = await read_child(
            dispatch_child,
            connector_id=K8S_CONNECTOR_ID,
            op_id="k8s.pod.list",
            params={"namespace": row.get("namespace"), "label_selector": selector, "limit": _PAGE},
        )
    except ChildReadError as exc:
        return exc.reason
    names = {str(pod["node"]) for pod in rows_of(answer) if pod.get("node")}
    return [node_source_id(cluster, name) for name in sorted(names)]


async def _claims(
    dispatch_child: DispatchChild,
) -> dict[tuple[str, str], dict[str, Any]] | MissingReason:
    try:
        answer = await read_child(
            dispatch_child,
            connector_id=K8S_CONNECTOR_ID,
            op_id="k8s.persistentvolumeclaim.list",
            params={"all_namespaces": True},
        )
    except ChildReadError as exc:
        return exc.reason
    return {(str(r.get("namespace")), str(r.get("name"))): r for r in rows_of(answer)}


async def _collect_nodes(
    dispatch_child: DispatchChild,
    builder: InventoryBuilder,
    *,
    cluster: str,
    collected_at: datetime,
) -> None:
    try:
        nodes = rows_of(
            await read_child(dispatch_child, connector_id=K8S_CONNECTOR_ID, op_id="k8s.node.list")
        )
    except ChildReadError as exc:
        builder.omit("cluster nodes", exc.reason, detail=exc.op_id)
        return
    builder.source_totals["server"] = len(nodes)
    for node in nodes:
        if node.get("name"):
            builder.add(build_node_record(node, cluster=cluster, collected_at=collected_at))
        else:
            builder.omit("a node without a name", "not_reported_by_source", item_kind="server")


async def _collect_workload_rows(
    dispatch_child: DispatchChild, builder: InventoryBuilder
) -> list[dict[str, Any]]:
    workloads: list[dict[str, Any]] = []
    complete = True
    for kind in WORKLOAD_KINDS:
        try:
            rows, cut = await _list_kind(dispatch_child, kind)
        except ChildReadError as exc:
            builder.omit(f"all {kind} workloads", exc.reason, detail=exc.op_id)
            complete = False
            continue
        if cut:
            builder.omit(f"{kind} workloads past {_PAGE * _MAX_PAGES}", "not_supported")
            complete = False
        workloads.extend(row for row in rows if row.get("name") and row.get("namespace"))
    if complete:
        builder.source_totals["kubernetes_workload"] = len(workloads)
    return workloads


async def k8s_migration_inventory(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Collect the nodes and workloads of one cluster and store them as one inventory."""
    del params
    cluster = target_name(target)
    builder = InventoryBuilder(connector_id=K8S_CONNECTOR_ID, target=cluster)
    collected_at = datetime.now(UTC)
    await _collect_nodes(dispatch_child, builder, cluster=cluster, collected_at=collected_at)
    workloads = await _collect_workload_rows(dispatch_child, builder)
    claims = await _claims(dispatch_child)
    if isinstance(claims, str):
        builder.omit("persistent volume claim sizes", claims)
    placements = await gather_limited(
        workloads, lambda row: _pod_nodes(dispatch_child, cluster, row)
    )
    for row, nodes_of in zip(workloads, placements, strict=True):
        if isinstance(nodes_of, str) and nodes_of != "not_reported_by_source":
            builder.omit(
                f"pods of {row.get('kind')} {row.get('namespace')}/{row.get('name')}",
                cast("MissingReason", nodes_of),
            )
        builder.add(
            build_workload_record(
                row, cluster=cluster, claims=claims, nodes=nodes_of, collected_at=collected_at
            )
        )
    return await store_inventory(operator, builder, op_id=K8S_INVENTORY_OP_ID)
