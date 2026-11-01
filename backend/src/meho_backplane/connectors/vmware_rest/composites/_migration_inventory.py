# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.migration.inventory`` -- one vCenter as a migration inventory.

A read-only multi-step operation. It returns one
:class:`~meho_backplane.connectors.inventory.contracts.InventoryEnvelope`
with one server record per VM, so the migration operations
(``inventory.export`` and the assessment) can load it by its handle.

Steps, all reads:

1. ``RetrieveServiceContent`` -- the root folder, the view manager, the
   performance manager and the vCenter instance UUID (the first part of
   every ``source_id``).
2. One paged ``RetrievePropertiesEx`` from the scope (the whole vCenter, one
   cluster or one VM folder) down to every VM: name and template flag. The
   walk goes through the hosts, or through a VM folder, its vApps and the
   vApps inside them.
3. vCenter's own count of the VMs in scope, read another way: a recursive
   ``ContainerView`` of ``VirtualMachine`` on the same root, which does not
   use the walk of step 2. That count is ``source_totals.server``.
4. Templates are left out and listed as omissions.
5. The other VMs in batches of :data:`BATCH_SIZE`: one ``RetrievePropertiesEx``
   for the record's properties, then their utilization. A batch that fails is
   read again one VM at a time; a VM that still fails becomes an omission,
   not a failed run.
6. The ESXi host and cluster names, read once for all hosts (best effort).
7. Duplicate detection (same BIOS UUID or instance UUID) and the count check:
   vCenter's count of step 3 against records plus omissions. A VM the walk
   missed shows as a difference. When the count read of step 3 failed, the
   result has no count check and one omission says so.

Like the other read multi-step operations of this connector (see
``_read.py``), the vCenter reads run on the connector's own session. The VCF
Operations reads are other connectors' operations, so they run as child
operations through ``dispatch_child``: each has its own policy check and
audit row on the VCF Operations target. Those two child operations are
declared with :func:`register_migration_inventory_dispatch_surface`, so the
start-up check of ``composite_invariant`` covers them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog
from pydantic import ValidationError

from meho_backplane.connectors.base import ConnectorResourceNotFoundError, ConnectorUpstreamError
from meho_backplane.connectors.inventory.contracts import (
    CountCheck,
    InventoryCounts,
    InventoryEnvelope,
    InventorySource,
    MissingReason,
    Omission,
    ServerRecord,
    find_identity_duplicates,
)
from meho_backplane.connectors.vmware_rest.composites._migration_inventory_map import (
    DETAIL_PROPERTIES,
    LIST_PROPERTIES,
    VmProps,
    as_dict,
    extract_objects,
    server_record,
)
from meho_backplane.connectors.vmware_rest.composites._migration_inventory_schemas import (
    MIGRATION_INVENTORY_OP_ID,
)
from meho_backplane.connectors.vmware_rest.composites._migration_inventory_usage import (
    VROPS_CHILD_OP_IDS,
    VROPS_CONNECTOR_ID,
    UsageResult,
    VcenterPerf,
    VmIdentity,
    VropsUsage,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    retrieve_properties_body,
    unwrap_vim_value,
    vim_moref,
)
from meho_backplane.connectors.vmware_rest.vim_retrieve import retrieve_all_pages
from meho_backplane.operations.composite_invariant import register_composite_dispatch_surface

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations.composite import DispatchChild

__all__ = [
    "BATCH_SIZE",
    "count_body",
    "migration_inventory_composite",
    "register_migration_inventory_dispatch_surface",
    "scope_body",
]

_log = structlog.get_logger(__name__)

#: VMs read in one ``RetrievePropertiesEx`` / ``QueryPerf``.
BATCH_SIZE: Final = 20

_SERVICE_CONTENT_PATH: Final = "/ServiceInstance/ServiceInstance/RetrieveServiceContent"
_CONTAINER_VIEW_TYPE: Final = "ContainerView"
#: The two view calls of the count check, in the form the vendor-spec check
#: reads (``POST:/<Type>/{moId}/<Method>``); :func:`_method_path` makes the
#: request path from them.
_OP_CREATE_CONTAINER_VIEW: Final = "POST:/ViewManager/{moId}/CreateContainerView"
_OP_DESTROY_VIEW: Final = "POST:/ContainerView/{moId}/DestroyView"
_VM_TYPE: Final = "VirtualMachine"
#: structlog keys the target resolver binds for the audit row. The VCF
#: Operations lookup must not change them for this operation's own row.
_TARGET_CONTEXT_KEYS: Final = ("target_id", "target_name", "audit_target_id")
#: Errors of one vCenter read that leave the rest of the run usable.
_READ_ERRORS: Final = (
    httpx.HTTPError,
    RuntimeError,
    ConnectorResourceNotFoundError,
    ConnectorUpstreamError,
)


def _selection(name: str) -> dict[str, Any]:
    return {VIM_TYPE_NAME_KEY: "SelectionSpec", "name": name}


def _traversal(name: str, mo_type: str, path: str, follow: Sequence[str]) -> dict[str, Any]:
    spec: dict[str, Any] = {
        VIM_TYPE_NAME_KEY: "TraversalSpec",
        "name": name,
        "type": mo_type,
        "path": path,
        "skip": False,
    }
    if follow:
        spec["selectSet"] = [_selection(step) for step in follow]
    return spec


#: From a folder, a datacenter or a cluster down to its VMs. VMs are reached
#: through the hosts (every registered VM, templates too, sits on a host) or,
#: in a VM folder, through the folder tree and its vApps. A vApp is a resource
#: pool: ``pool_vms`` reads its VMs and ``pool_children`` follows the vApps
#: (and pools) inside it, as deep as they go.
_TRAVERSAL: Final[tuple[dict[str, Any], ...]] = (
    _traversal(
        "folder_children",
        "Folder",
        "childEntity",
        (
            "folder_children",
            "datacenter_hosts",
            "compute_hosts",
            "host_vms",
            "pool_vms",
            "pool_children",
        ),
    ),
    _traversal("datacenter_hosts", "Datacenter", "hostFolder", ("folder_children",)),
    _traversal("compute_hosts", "ComputeResource", "host", ("host_vms",)),
    _traversal("host_vms", "HostSystem", "vm", ()),
    _traversal("pool_vms", "ResourcePool", "vm", ()),
    _traversal("pool_children", "ResourcePool", "resourcePool", ("pool_vms", "pool_children")),
)


def scope_body(root_type: str, root_moid: str, path_set: Sequence[str]) -> dict[str, Any]:
    """``RetrievePropertiesEx`` body that reads *path_set* of every VM below the root."""
    return {
        "specSet": [
            {
                VIM_TYPE_NAME_KEY: "PropertyFilterSpec",
                "propSet": [
                    {VIM_TYPE_NAME_KEY: "PropertySpec", "type": _VM_TYPE, "pathSet": list(path_set)}
                ],
                "objectSet": [
                    {
                        VIM_TYPE_NAME_KEY: "ObjectSpec",
                        "obj": vim_moref(root_type, root_moid),
                        "skip": False,
                        "selectSet": list(_TRAVERSAL),
                    }
                ],
            }
        ],
        "options": {VIM_TYPE_NAME_KEY: "RetrieveOptions"},
    }


def count_body(view_moid: str) -> dict[str, Any]:
    """``RetrievePropertiesEx`` body that names every VM of a ``ContainerView``."""
    return {
        "specSet": [
            {
                VIM_TYPE_NAME_KEY: "PropertyFilterSpec",
                "propSet": [
                    {VIM_TYPE_NAME_KEY: "PropertySpec", "type": _VM_TYPE, "pathSet": ["name"]}
                ],
                "objectSet": [
                    {
                        VIM_TYPE_NAME_KEY: "ObjectSpec",
                        "obj": vim_moref(_CONTAINER_VIEW_TYPE, view_moid),
                        "skip": True,
                        "selectSet": [_traversal("view", _CONTAINER_VIEW_TYPE, "view", ())],
                    }
                ],
            }
        ],
        "options": {VIM_TYPE_NAME_KEY: "RetrieveOptions"},
    }


def _method_path(op_id: str, moid: str) -> str:
    """The VI-JSON request path of *op_id* (``POST:/<Type>/{moId}/<Method>``) on *moid*."""
    return op_id.removeprefix("POST:").replace("{moId}", moid)


def _moref(value: Any) -> str | None:
    if isinstance(value, dict) and isinstance(value.get("value"), str) and value["value"]:
        return str(value["value"])
    return None


@dataclass(frozen=True)
class _ServiceContent:
    root_folder: str
    view_manager: str
    perf_manager: str
    vcenter_uuid: str | None


async def _service_content(
    connector: VmwareRestConnector, target: Any, operator: Operator
) -> _ServiceContent:
    raw = await connector._post_vmomi_json(
        target, _SERVICE_CONTENT_PATH, operator=operator, json={}
    )
    content = as_dict(unwrap_vim_value(raw))
    root = _moref(content.get("rootFolder"))
    if root is None:
        raise RuntimeError("vCenter's ServiceContent has no rootFolder")
    about = as_dict(content.get("about"))
    uuid = about.get("instanceUuid")
    return _ServiceContent(
        root_folder=root,
        view_manager=_moref(content.get("viewManager")) or "ViewManager",
        perf_manager=_moref(content.get("perfManager")) or "PerfMgr",
        vcenter_uuid=uuid.lower() if isinstance(uuid, str) and uuid else None,
    )


async def _count_in_scope(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    view_manager: str,
    root_type: str,
    root_moid: str,
) -> int | str:
    """vCenter's own count of the VMs below the root, or the error name when it failed.

    A recursive ``ContainerView`` of ``VirtualMachine`` is vCenter's own walk
    of the scope; it shares nothing with :data:`_TRAVERSAL`, so a VM that
    walk misses shows up as a difference in the count check.
    """
    view: str | None = None
    try:
        created = await connector._post_vmomi_json(
            target,
            _method_path(_OP_CREATE_CONTAINER_VIEW, view_manager),
            operator=operator,
            json={
                "container": vim_moref(root_type, root_moid),
                "type": [_VM_TYPE],
                "recursive": True,
            },
        )
        view = _moref(unwrap_vim_value(created))
        if view is None:
            raise RuntimeError("CreateContainerView returned no view")
        found = await retrieve_all_pages(connector, target, operator, count_body(view))
        return len(extract_objects(found))
    except _READ_ERRORS as exc:
        _log.info("migration_inventory_count_failed", error=type(exc).__name__)
        return type(exc).__name__
    finally:
        if view is not None:
            await _destroy_view(connector, target, operator, view)


async def _destroy_view(
    connector: VmwareRestConnector, target: Any, operator: Operator, view: str
) -> None:
    try:
        await connector._post_vmomi_json(
            target, _method_path(_OP_DESTROY_VIEW, view), operator=operator, json={}
        )
    except _READ_ERRORS as exc:
        # vCenter drops the view with the session; a failed destroy is not a problem of the run.
        _log.info("migration_inventory_view_not_destroyed", error=type(exc).__name__)


def _scope(params: dict[str, Any], root_folder: str) -> tuple[str, str]:
    if params.get("cluster"):
        return "ClusterComputeResource", str(params["cluster"])
    if params.get("folder"):
        return "Folder", str(params["folder"])
    return "Folder", root_folder


async def _read_batch(
    connector: VmwareRestConnector, target: Any, operator: Operator, moids: Sequence[str]
) -> dict[str, VmProps]:
    return extract_objects(
        await retrieve_all_pages(
            connector,
            target,
            operator,
            retrieve_properties_body(_VM_TYPE, moids, DETAIL_PROPERTIES),
        )
    )


async def _read_details(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    moids: Sequence[str],
    failures: dict[str, str],
) -> dict[str, VmProps]:
    """Detail properties of *moids*; a failed batch is read again one VM at a time."""
    try:
        return await _read_batch(connector, target, operator, moids)
    except _READ_ERRORS as exc:
        _log.info("migration_inventory_batch_failed", error=type(exc).__name__, vms=len(moids))
    found: dict[str, VmProps] = {}
    for moid in moids:
        try:
            found.update(await _read_batch(connector, target, operator, [moid]))
        except _READ_ERRORS as exc:
            failures[moid] = type(exc).__name__
    return found


async def _placement(
    connector: VmwareRestConnector, target: Any, operator: Operator, vms: Sequence[VmProps]
) -> tuple[dict[str, str], dict[str, str]]:
    """``({host moid: host name}, {host moid: cluster name})``; empty on failure."""
    hosts = sorted({h for h in (_moref(vm.get("runtime.host")) for vm in vms) if h})
    if not hosts:
        return {}, {}
    try:
        host_props = extract_objects(
            await retrieve_all_pages(
                connector,
                target,
                operator,
                retrieve_properties_body("HostSystem", hosts, ("name", "parent")),
            )
        )
        parents = {
            moid: ref
            for moid, props in host_props.items()
            if isinstance(ref := props.get("parent"), dict)
            and ref.get("type") == "ClusterComputeResource"
        }
        clusters = sorted({str(ref["value"]) for ref in parents.values() if ref.get("value")})
        cluster_props = (
            extract_objects(
                await retrieve_all_pages(
                    connector,
                    target,
                    operator,
                    retrieve_properties_body("ClusterComputeResource", clusters, ("name",)),
                )
            )
            if clusters
            else {}
        )
    except _READ_ERRORS as exc:
        _log.info("migration_inventory_placement_failed", error=type(exc).__name__)
        return {}, {}
    host_names = {
        m: str(p.get("name")) for m, p in host_props.items() if isinstance(p.get("name"), str)
    }
    cluster_names = {
        host: str(cluster_props[str(ref["value"])].get("name"))
        for host, ref in parents.items()
        if str(ref.get("value")) in cluster_props
        and isinstance(cluster_props[str(ref["value"])].get("name"), str)
    }
    return host_names, cluster_names


async def _resolve_vrops_target(operator: Operator, name: str) -> Any:
    """The caller's VCF Operations target named *name*, tenant-scoped."""
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.targets.resolver import (
        AmbiguousTargetError,
        TargetNotFoundError,
        resolve_target,
    )

    bound = structlog.contextvars.get_contextvars()
    saved = {key: bound[key] for key in _TARGET_CONTEXT_KEYS if key in bound}
    try:
        async with get_sessionmaker()() as session:
            row = await resolve_target(session, operator.tenant_id, name.strip())
    except TargetNotFoundError:
        raise ValueError(
            f"vcf_operations_target {name!r}: no such target in your tenant (list_targets)"
        ) from None
    except AmbiguousTargetError:
        raise ValueError(f"vcf_operations_target {name!r} names more than one target") from None
    finally:
        structlog.contextvars.unbind_contextvars(*_TARGET_CONTEXT_KEYS)
        structlog.contextvars.bind_contextvars(**saved)
    if getattr(row, "product", None) != "vrops":
        raise ValueError(
            f"vcf_operations_target {name!r} is a {getattr(row, 'product', None)!r} target, "
            "not a VCF Operations target"
        )
    return row


class _Collector:
    """Builds the records and omissions of one run."""

    def __init__(self, *, vcenter_id: str, perf: VcenterPerf, vrops: VropsUsage | None) -> None:
        self.vcenter_id = vcenter_id
        self.perf = perf
        self.vrops = vrops
        self.records: list[ServerRecord] = []
        self.omissions: list[Omission] = []
        self._instance_uuids: set[str] = set()

    def omit(self, moid: str, name: str | None, reason: MissingReason, detail: str) -> None:
        label = f"VM {name!r} ({moid})" if name else f"VM {moid}"
        self.omissions.append(
            Omission(
                what=label,
                reason=reason,
                detail=detail,
                kind="server",
                source_id=f"vmware:{self.vcenter_id}:{moid}",
            )
        )

    def source_id(self, vm: VmProps) -> str:
        instance = vm.get("config.instanceUuid")
        if isinstance(instance, str) and instance and instance.lower() not in self._instance_uuids:
            self._instance_uuids.add(instance.lower())
            return f"vmware:{self.vcenter_id}:{instance.lower()}"
        return f"vmware:{self.vcenter_id}:{vm.moid}"

    async def usage(self, vms: Sequence[VmProps]) -> UsageResult:
        if self.vrops is not None:
            return await self.vrops.read(
                VmIdentity(
                    moid=vm.moid,
                    name=vm.get("name") if isinstance(vm.get("name"), str) else None,
                    instance_uuid=vm.get("config.instanceUuid")
                    if isinstance(vm.get("config.instanceUuid"), str)
                    else None,
                    power_state=vm.get("runtime.powerState"),
                )
                for vm in vms
            )
        return await self.perf.read({vm.moid: vm.get("runtime.powerState") for vm in vms})

    def add(
        self,
        vm: VmProps,
        usage: UsageResult,
        placement: tuple[dict[str, str], dict[str, str]],
        collected_at: datetime,
    ) -> None:
        host = _moref(vm.get("runtime.host"))
        try:
            record = server_record(
                vm,
                source_id=self.source_id(vm),
                collected_at=collected_at,
                esxi_host=placement[0].get(host or ""),
                cluster=placement[1].get(host or ""),
                utilization=usage.by_vm.get(vm.moid),
                utilization_reason=usage.reasons.get(vm.moid),
            )
        except ValidationError as exc:
            # The values come from vCenter; keep them out of the message.
            self.omit(
                vm.moid,
                None,
                "collection_failed",
                f"vCenter's values do not fit the inventory shape ({exc.error_count()} errors)",
            )
            return
        self.records.append(record)


def _source_problems(collector: _Collector) -> list[Omission]:
    """One omission per utilization source that failed as a whole."""
    problems: list[Omission] = []
    if collector.vrops is not None and collector.vrops.problem:
        problems.append(
            Omission(
                what="utilization from VCF Operations",
                reason=collector.vrops.stopped or "collection_failed",
                detail=collector.vrops.problem,
            )
        )
    if collector.vrops is None and collector.perf.problem:
        problems.append(
            Omission(
                what="utilization from vCenter performance history (some VMs)",
                reason="collection_failed",
                detail=collector.perf.problem,
            )
        )
    return problems


def _sort_listing(listing: dict[str, VmProps], collector: _Collector) -> list[str]:
    """Omit the templates; return the moids of the other VMs, in vCenter's order."""
    candidates: list[str] = []
    for moid, vm in listing.items():
        name = vm.get("name") if isinstance(vm.get("name"), str) else None
        if vm.get("config.template") is True:
            collector.omit(moid, name, "not_supported", "a VM template, not a running server")
        else:
            candidates.append(moid)
    return candidates


async def _read_all_details(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    listing: dict[str, VmProps],
    candidates: Sequence[str],
    collector: _Collector,
) -> list[VmProps]:
    """Detail properties of every candidate, in batches; a VM not read is omitted."""
    details: list[VmProps] = []
    failures: dict[str, str] = {}
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start : start + BATCH_SIZE]
        found = await _read_details(connector, target, operator, batch, failures)
        details.extend(found[moid] for moid in batch if moid in found)
        for moid in batch:
            if moid not in found:
                name = listing[moid].get("name")
                collector.omit(
                    moid,
                    name if isinstance(name, str) else None,
                    "collection_failed",
                    f"the VM could not be read ({failures.get(moid, 'not found any more')})",
                )
    return details


def _count_problem(source_count: int | str) -> list[Omission]:
    if isinstance(source_count, int):
        return []
    return [
        Omission(
            what="vCenter's own VM count (ContainerView)",
            reason="collection_failed",
            detail=(
                f"the count read failed ({source_count}); there is no count check for "
                "this run, so a VM the listing missed cannot show"
            ),
        )
    ]


def _counts(source_count: int | str, collector: _Collector) -> InventoryCounts:
    """The count check: vCenter's own count against records plus omissions.

    When the count read failed there is no check: a check built on the listing
    count could only ever match, and would make a listing that missed VMs look
    verified. The omission from :func:`_count_problem` says so instead.
    """
    records = len(collector.records)
    if not isinstance(source_count, int):
        return InventoryCounts(server=records, application=0, kubernetes_workload=0)
    omitted = len(collector.omissions)
    difference = source_count - records - omitted
    return InventoryCounts(
        server=records,
        application=0,
        kubernetes_workload=0,
        source_totals={"server": source_count},
        reconciliation=[
            CountCheck(
                kind="server",
                source_total=source_count,
                records=records,
                omitted=omitted,
                difference=difference,
                matches=difference == 0,
            )
        ],
    )


def _envelope(
    connector: VmwareRestConnector,
    target: Any,
    source_count: int | str,
    collector: _Collector,
) -> InventoryEnvelope:
    """The inventory with its omissions, duplicates and count check."""
    return InventoryEnvelope(
        schema_version="1",
        source=InventorySource(
            connector_id=f"vmware-rest-{_connector_version(connector)}",
            target=str(getattr(target, "name", "unknown")),
            collected_at=datetime.now(UTC),
        ),
        records=list(collector.records),
        omissions=[
            *collector.omissions,
            *_count_problem(source_count),
            *_source_problems(collector),
        ],
        counts=_counts(source_count, collector),
        duplicates=find_identity_duplicates(collector.records),
    )


async def migration_inventory_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Read one vCenter as a migration inventory. See the module docstring.

    Op-id: ``vmware.migration.inventory``. Read-only.
    """
    now = datetime.now(UTC)
    days = int(params.get("utilization_days", 30))
    vrops_name = params.get("vcf_operations_target")
    vrops_target = await _resolve_vrops_target(operator, vrops_name) if vrops_name else None
    content = await _service_content(connector, target, operator)
    root_type, root_moid = _scope(params, content.root_folder)
    listing = extract_objects(
        await retrieve_all_pages(
            connector, target, operator, scope_body(root_type, root_moid, LIST_PROPERTIES)
        )
    )
    source_count = await _count_in_scope(
        connector, target, operator, content.view_manager, root_type, root_moid
    )
    vcenter_uuid = content.vcenter_uuid
    collector = _Collector(
        vcenter_id=vcenter_uuid or f"target-{getattr(target, 'name', 'unknown')}",
        perf=VcenterPerf(
            connector, target, operator, perf_manager=content.perf_manager, days=days, now=now
        ),
        vrops=(
            VropsUsage(dispatch_child, vrops_target, operator=operator, vcenter_uuid=vcenter_uuid)
            if vrops_target is not None
            else None
        ),
    )
    candidates = _sort_listing(listing, collector)
    details = await _read_all_details(connector, target, operator, listing, candidates, collector)
    placement = await _placement(connector, target, operator, details)
    for start in range(0, len(details), BATCH_SIZE):
        batch_vms = details[start : start + BATCH_SIZE]
        usage = await collector.usage(batch_vms)
        collected_at = datetime.now(UTC)
        for vm in batch_vms:
            collector.add(vm, usage, placement, collected_at)
    envelope = _envelope(connector, target, source_count, collector)
    _log.info(
        "migration_inventory_read",
        target=getattr(target, "name", None),
        vms=len(listing),
        source_count=source_count,
        records=len(collector.records),
        omissions=len(envelope.omissions),
        duplicates=len(envelope.duplicates),
    )
    return envelope.model_dump(mode="json")


def register_migration_inventory_dispatch_surface() -> None:
    """Declare the two VCF Operations child operations for the start-up check.

    Called by the vCenter composite registrars. The check in
    ``composite_invariant`` then fails the start closed if either child
    operation ever resolves to an ingested catalog row.
    """
    register_composite_dispatch_surface(
        composite_op_id=MIGRATION_INVENTORY_OP_ID,
        connector_id=VROPS_CONNECTOR_ID,
        sub_op_ids=VROPS_CHILD_OP_IDS,
    )


def _connector_version(connector: VmwareRestConnector) -> str:
    version = getattr(connector, "version", None)
    return version if isinstance(version, str) and version else "9.0"
