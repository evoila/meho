# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed port-group teardown deletes (#3339) -- distributed and host standard.

* ``vmware.composite.network.portgroup.delete`` -- the inverse of
  ``network.portgroup.create``: ``DistributedVirtualPortgroup.Destroy_Task``
  (task-polled). Refuses the switch's uplink portgroup and any portgroup a VM
  NIC or another connectee (vmkernel adapter) still uses, naming them.
* ``vmware.composite.host.standard_portgroup.delete`` -- a port group on one
  host's standard vSwitch: ``HostNetworkSystem.RemovePortGroup`` (synchronous,
  204). Refuses while a VM registered on that host or a vmkernel adapter uses
  it, naming them.

Both follow the shared plan-then-act seam of :mod:`._teardown` (one read-only
plan feeds the park-time blast radius and the post-approval handler) and ride
the ``/sdk/vim25`` VI-JSON seam: there is no REST port-group delete, and the
ingested vim-object bindings 404 under ``/api`` (#3534). Moid inputs only (the
parameter schemas pin the ``dvportgroup-N`` / ``host-N`` shape), so preview and
call can never disagree about a display name (#3323).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._read import _extract_props_by_moid
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_DELETED,
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    STATUS_STILL_PRESENT,
    TeardownPlan,
    blast_radius,
    capped,
    destroy_outcome,
    destroy_task,
    envelope,
    moref_values,
    pre_write_outcome,
    read_object,
    timeout_envelope,
    vm_rows,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _DVPG_MO_TYPE,
    _HOST_SYSTEM_MO_TYPE,
    _OP_RETRIEVE_PROPERTIES,
    _VMOMI_RETRIEVE_PROPERTIES_PATH,
    _moref_value,
    _observed_vlan_identity,
    _unwrap_value,
    _vlan_identity_view,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "host_standard_portgroup_delete_composite",
    "host_standard_portgroup_delete_preview",
    "network_portgroup_delete_composite",
    "network_portgroup_delete_preview",
]

_OP_DESTROY_DVPORTGROUP_TASK: Final = "POST:/DistributedVirtualPortgroup/{moId}/Destroy_Task"
#: Read-only port enumeration on the owning switch (``System.Read``) -- names
#: the connected ports a portgroup delete would orphan. Un-gated, like the
#: ``RetrievePropertiesEx`` reads.
_OP_FETCH_DV_PORTS: Final = "POST:/DistributedVirtualSwitch/{moId}/FetchDVPorts"
#: Synchronous (204) port-group removal on the host's network config manager.
_OP_REMOVE_PORT_GROUP: Final = "POST:/HostNetworkSystem/{moId}/RemovePortGroup"

#: vi-json sub-op manifests (``_VIM_SUB_OPS_*`` namespace, so the vcenter.yaml
#: sweep skips them; the vi-json reconcile lane asserts every path exists).
_VIM_SUB_OPS_NETWORK_PORTGROUP_DELETE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_FETCH_DV_PORTS,
    _OP_DESTROY_DVPORTGROUP_TASK,
)
_VIM_SUB_OPS_HOST_STANDARD_PORTGROUP_DELETE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_REMOVE_PORT_GROUP,
)

_DVS_MO_TYPE: Final = "DistributedVirtualSwitch"
_VMWARE_DVS_MO_TYPE: Final = "VmwareDistributedVirtualSwitch"
_NETWORK_MO_TYPE: Final = "Network"
#: ``_typeName`` of the ``FetchDVPorts`` criteria DataObject (#3103 annotation).
_DVS_PORT_CRITERIA_TYPE: Final = "DistributedVirtualSwitchPortCriteria"

# ===========================================================================
# network.portgroup.delete -- DistributedVirtualPortgroup.Destroy_Task
# ===========================================================================

_PG_PROPS: Final = ["name", "key", "config", "vm"]


async def _fetch_connected_ports(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    dvs: tuple[str, str],
    portgroup_key: str,
) -> list[dict[str, Any]]:
    """The portgroup's ports that have a connectee (VM vNIC / vmkernel adapter)."""
    dvs_type, dvs_moid = dvs
    mo_type = dvs_type if dvs_type in (_DVS_MO_TYPE, _VMWARE_DVS_MO_TYPE) else _DVS_MO_TYPE
    raw = await connector._post_vmomi_json(
        target,
        f"/{mo_type}/{dvs_moid}/FetchDVPorts",
        operator=operator,
        json={
            "criteria": {
                VIM_TYPE_NAME_KEY: _DVS_PORT_CRITERIA_TYPE,
                "connected": True,
                "inside": True,
                "portgroupKey": [portgroup_key],
            }
        },
    )
    ports = unwrap_vim_value(_unwrap_value(raw))
    rows: list[dict[str, Any]] = []
    for port in ports if isinstance(ports, list) else []:
        connectee = port.get("connectee") if isinstance(port, dict) else None
        if not isinstance(connectee, dict):
            continue
        rows.append(
            {
                "kind": "port",
                "id": port.get("key"),
                "connectee_type": connectee.get("type"),
                "connected_entity": _moref_value(connectee.get("connectedEntity")),
                "nic": connectee.get("nicKey"),
            }
        )
    return rows


def _dvpg_identity(
    portgroup: str, props: dict[str, Any]
) -> tuple[dict[str, Any], tuple[str, str] | None]:
    """The portgroup's blast-radius identity + its ``(switch type, switch moid)``."""
    config = props.get("config")
    config = config if isinstance(config, dict) else {}
    dvs_raw = unwrap_vim_value(config.get("distributedVirtualSwitch"))
    dvs_moid = _moref_value(dvs_raw)
    dvs_type = dvs_raw.get("type") if isinstance(dvs_raw, dict) else None
    port_config = config.get("defaultPortConfig")
    vlan_raw = port_config.get("vlan") if isinstance(port_config, dict) else None
    obj: dict[str, Any] = {
        "kind": "distributed_portgroup",
        "moid": portgroup,
        "name": props.get("name") or config.get("name"),
        "dvs": dvs_moid,
        "vlan": _vlan_identity_view(_observed_vlan_identity(vlan_raw)),
        "num_ports": config.get("numPorts"),
        "port_binding": config.get("type"),
        "uplink": config.get("uplink") is True,
    }
    return obj, (str(dvs_type or ""), dvs_moid) if dvs_moid is not None else None


async def _other_connected_ports(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    obj: dict[str, Any],
    dvs: tuple[str, str] | None,
    key: Any,
    vm_moids: list[str],
) -> list[dict[str, Any]]:
    """Connected ports not already named by a VM row (vmkernel / host adapters).

    Best-effort: ``obj["port_check"]`` records whether the switch answered.
    vSphere itself refuses a ``Destroy_Task`` on a portgroup with connected
    ports (``ResourceInUse`` -> a raised task fault), so an unreadable port
    list never lets an in-use portgroup be deleted; this read only names them.
    """
    if dvs is None or not isinstance(key, str):
        obj["port_check"] = "unavailable (no switch / portgroup key)"
        return []
    try:
        ports = await _fetch_connected_ports(
            connector, target, operator, dvs=dvs, portgroup_key=key
        )
    except Exception as exc:
        obj["port_check"] = f"unavailable ({type(exc).__name__})"
        return []
    obj["port_check"] = "ok"
    return [
        row
        for row in ports
        if not (row["connectee_type"] == "vmVnic" and row["connected_entity"] in vm_moids)
    ]


async def plan_network_portgroup_delete(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> TeardownPlan:
    """Plan a distributed-portgroup delete (read-only)."""
    portgroup = params["portgroup"]
    props = await read_object(
        connector, target, operator, mo_type=_DVPG_MO_TYPE, moid=portgroup, props=_PG_PROPS
    )
    if props is None:
        return TeardownPlan(
            object={"kind": "distributed_portgroup", "moid": portgroup}, present=False
        )
    obj, dvs = _dvpg_identity(portgroup, props)
    plan = TeardownPlan(object=obj, present=True)
    if obj["uplink"]:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"portgroup {portgroup!r} is the switch's uplink portgroup; it is managed with "
            "the distributed switch and cannot be deleted on its own",
        )
        return plan
    vm_moids = [moid for _type, moid in moref_values(props.get("vm"))]
    vms = await vm_rows(connector, target, operator, vm_moids)
    ports = await _other_connected_ports(
        connector, target, operator, obj=obj, dvs=dvs, key=props.get("key"), vm_moids=vm_moids
    )
    plan.blockers = capped(vms + ports)
    if vms or ports:
        names = ", ".join(str(row.get("name") or row.get("moid")) for row in vms[:10])
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"portgroup {portgroup!r} is in use: {len(vms)} VM(s) [{names}] and "
            f"{len(ports)} other connected port(s) (vmkernel / host adapters). Repoint or "
            "remove every NIC first (e.g. vmware.composite.vm.nic.repoint); see 'blockers'",
        )
    return plan


async def network_portgroup_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Delete one distributed portgroup via ``Destroy_Task`` (#3339).

    Op-id: ``vmware.composite.network.portgroup.delete``. Refuses an uplink
    portgroup or one with any VM / connected port before any write; returns
    ``unchanged`` when the moid no longer exists; otherwise the gated
    ``Destroy_Task``, polled (a fault raises), then a read-back that must find
    it gone.
    """
    plan = await plan_network_portgroup_delete(connector, target, operator, params)
    early = pre_write_outcome(
        plan, absent_guidance="the portgroup does not exist; nothing was deleted"
    )
    if early is not None:
        return early
    portgroup = params["portgroup"]
    outcome = await destroy_task(
        connector,
        target,
        operator,
        op_id=_OP_DESTROY_DVPORTGROUP_TASK,
        mo_type=_DVPG_MO_TYPE,
        moid=portgroup,
        params={"portgroup": portgroup},
        label="network.portgroup.delete",
    )
    if isinstance(outcome, OperationResult):
        return outcome
    if outcome.timed_out:
        return timeout_envelope(
            plan, outcome, method="Destroy_Task", reread="re-run to confirm (absent = unchanged)"
        )
    after = await read_object(
        connector, target, operator, mo_type=_DVPG_MO_TYPE, moid=portgroup, props=["name"]
    )
    return destroy_outcome(plan, outcome, gone=after is None, what="portgroup")


async def network_portgroup_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``network.portgroup.delete`` (the handler's own plan)."""
    portgroup = ctx.params.get("portgroup")
    if not isinstance(portgroup, str) or ctx.connector_instance is None:
        return None
    plan = await plan_network_portgroup_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {"portgroup": portgroup},
    )
    return blast_radius(plan)


# ===========================================================================
# host.standard_portgroup.delete -- HostNetworkSystem.RemovePortGroup
# ===========================================================================

_PROP_NETWORK_SYSTEM: Final = "configManager.networkSystem"
_PROP_HOST_PORTGROUPS: Final = "config.network.portgroup"
_PROP_HOST_VNICS: Final = "config.network.vnic"
_HOST_PG_PROPS: Final = [
    "name",
    _PROP_NETWORK_SYSTEM,
    _PROP_HOST_PORTGROUPS,
    _PROP_HOST_VNICS,
    "vm",
    "network",
]

#: Standard-portgroup ``vlanId`` sentinels (``HostPortGroupSpec.vlanId``).
_VLAN_NONE: Final = 0
_VLAN_TRUNK_ALL: Final = 4095


def _standard_vlan_view(vlan_id: Any) -> dict[str, Any] | None:
    """Agent-facing view of a standard portgroup's single ``vlanId``."""
    if not isinstance(vlan_id, int) or isinstance(vlan_id, bool):
        return None
    if vlan_id == _VLAN_NONE:
        return {"mode": "untagged", "vlan_id": _VLAN_NONE}
    if vlan_id == _VLAN_TRUNK_ALL:
        return {"mode": "trunk_all", "vlan_id": _VLAN_TRUNK_ALL}
    return {"mode": "access", "vlan_id": vlan_id}


def _find_host_portgroup(raw: Any, name: str) -> dict[str, Any] | None:
    """The ``HostPortGroup`` whose ``spec.name`` equals *name*, else ``None``."""
    groups = unwrap_vim_value(raw)
    for group in groups if isinstance(groups, list) else []:
        spec = group.get("spec") if isinstance(group, dict) else None
        if isinstance(spec, dict) and spec.get("name") == name:
            return dict(group)
    return None


async def _host_vms_on_network(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    host_props: dict[str, Any],
    portgroup_name: str,
) -> list[str]:
    """VM moids **registered on this host** whose NICs use the named network.

    The standard port group's vCenter ``Network`` object is shared by name
    across hosts, so its ``vm`` list is intersected with ``HostSystem.vm``.
    Covers powered-off VMs (which hold no live port, so vSphere's own
    ``ResourceInUse`` check would not stop the removal). Load-bearing: a failing
    read propagates, never "no VMs".
    """
    networks = [
        moid
        for mo_type, moid in moref_values(host_props.get("network"))
        if mo_type == _NETWORK_MO_TYPE
    ]
    if not networks:
        return []
    result = await connector._post_vmomi_json(
        target,
        _VMOMI_RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=retrieve_properties_body(_NETWORK_MO_TYPE, networks, ["name", "vm"]),
    )
    host_vms = {moid for _type, moid in moref_values(host_props.get("vm"))}
    attached = {
        moid
        for props in _extract_props_by_moid(result).values()
        if props.get("name") == portgroup_name
        for _type, moid in moref_values(props.get("vm"))
        if moid in host_vms
    }
    return sorted(attached)


def _vmk_rows(raw_vnics: Any, name: str) -> list[dict[str, Any]]:
    """``[{kind: vmkernel_adapter, id: vmkN}]`` for the host vNICs on port group *name*."""
    vnics = unwrap_vim_value(raw_vnics)
    return [
        {"kind": "vmkernel_adapter", "id": vnic.get("device"), "name": None}
        for vnic in (vnics if isinstance(vnics, list) else [])
        if isinstance(vnic, dict) and vnic.get("portgroup") == name
    ]


async def plan_host_standard_portgroup_delete(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> TeardownPlan:
    """Plan a host standard-vSwitch port group delete (read-only)."""
    host = params["host"]
    name = params["portgroup_name"]
    obj: dict[str, Any] = {"kind": "host_standard_portgroup", "host": host, "name": name}
    props = await read_object(
        connector, target, operator, mo_type=_HOST_SYSTEM_MO_TYPE, moid=host, props=_HOST_PG_PROPS
    )
    if props is None:
        refusal = (STATUS_INVALID_REQUEST, f"host {host!r} not found; pass a HostSystem moid")
        return TeardownPlan(object=obj, present=False, refusal=refusal)
    obj["host_name"] = props.get("name")
    group = _find_host_portgroup(props.get(_PROP_HOST_PORTGROUPS), name)
    if group is None:
        return TeardownPlan(object=obj, present=False)
    spec = group.get("spec")
    spec = spec if isinstance(spec, dict) else {}
    ports = group.get("port")
    obj.update(
        vswitch=spec.get("vswitchName"),
        vlan=_standard_vlan_view(spec.get("vlanId")),
        active_ports=len(ports) if isinstance(ports, list) else 0,
    )
    network_system = _moref_value(unwrap_vim_value(props.get(_PROP_NETWORK_SYSTEM)))
    plan = TeardownPlan(object=obj, present=True, context={"network_system": network_system})
    if network_system is None:
        detail = f"host {host!r} exposes no configManager.networkSystem; cannot remove port groups"
        plan.refusal = (STATUS_PRECONDITION_FAILED, detail)
        return plan
    vm_moids = await _host_vms_on_network(
        connector, target, operator, host_props=props, portgroup_name=name
    )
    blockers = await vm_rows(connector, target, operator, vm_moids)
    blockers += _vmk_rows(props.get(_PROP_HOST_VNICS), name)
    plan.blockers = capped(blockers)
    if blockers:
        listed = ", ".join(
            str(row.get("name") or row.get("moid") or row.get("id")) for row in blockers[:10]
        )
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"port group {name!r} on host {host!r} is in use by {len(vm_moids)} VM(s) and "
            f"{len(blockers) - len(vm_moids)} vmkernel adapter(s) [{listed}]; repoint or "
            "remove them first (see 'blockers')",
        )
    return plan


async def _host_portgroup_present(
    connector: VmwareRestConnector, target: Any, operator: Operator, *, host: str, name: str
) -> bool:
    props = await read_object(
        connector,
        target,
        operator,
        mo_type=_HOST_SYSTEM_MO_TYPE,
        moid=host,
        props=[_PROP_HOST_PORTGROUPS],
    )
    if props is None:
        return False
    return _find_host_portgroup(props.get(_PROP_HOST_PORTGROUPS), name) is not None


async def host_standard_portgroup_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Remove one standard-vSwitch port group from one host (#3339).

    Op-id: ``vmware.composite.host.standard_portgroup.delete``. Refuses when a
    VM registered on the host or a vmkernel adapter uses the port group, before
    any write; ``unchanged`` when the host carries no port group of that name.
    ``RemovePortGroup`` is synchronous: a vim fault (``ResourceInUse`` /
    ``NotFound`` / ``HostConfigFault``) raises (``connector_error``, audited
    failed). A read-back must find the port group gone.
    """
    plan = await plan_host_standard_portgroup_delete(connector, target, operator, params)
    early = pre_write_outcome(
        plan, absent_guidance="the host has no port group of that name; nothing was deleted"
    )
    if early is not None:
        return early
    host = params["host"]
    name = params["portgroup_name"]
    gate, _payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_REMOVE_PORT_GROUP,
        vmomi_path=f"/HostNetworkSystem/{plan.context['network_system']}/RemovePortGroup",
        body={"pgName": name},
        params={"host": host, "portgroup_name": name},
    )
    if gate is not None:
        return gate
    if await _host_portgroup_present(connector, target, operator, host=host, name=name):
        return envelope(
            plan,
            STATUS_STILL_PRESENT,
            guidance="RemovePortGroup returned but the port group still reads back on the host",
        )
    return envelope(plan, STATUS_DELETED)


async def host_standard_portgroup_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``host.standard_portgroup.delete`` (the handler's own plan)."""
    host = ctx.params.get("host")
    name = ctx.params.get("portgroup_name")
    if not isinstance(host, str) or not isinstance(name, str) or ctx.connector_instance is None:
        return None
    plan = await plan_host_standard_portgroup_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {"host": host, "portgroup_name": name},
    )
    return blast_radius(plan)
