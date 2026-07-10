# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.vm.info`` typed op (#2300).

Phase-1 "incident survival" read: the single-VM hung-appliance tell the
adopter reaches for first -- power state, guest IP, VMware Tools status /
running status, guest heartbeat, and per-datastore storage usage -- for
one VM addressed by managed-object id **or** name. It is a
``source_kind="typed"`` bound method on :class:`VmwareRestConnector` in
the :mod:`~meho_backplane.connectors.vmware_rest.typed_ops` mould
established by ``vmware.host.usage`` (#2257): it reads directly on the
connector session (no ``dispatch_child``, no ingested descriptor), so it
works on a fresh boot with **zero catalog ingest**.

Why not the plain Automation REST ``GET /vcenter/vm``
------------------------------------------------------

``GET /api/vcenter/vm/{vm}`` returns configuration (cpu / memory / disks /
nics), not the live guest signals an operator triages a stuck VM with.
Those live on the ``VirtualMachine`` managed object and are reached via
the PropertyCollector ``RetrievePropertiesEx`` vi-json method:

* ``runtime.powerState`` (:class:`VirtualMachinePowerState`) -- the raw
  vim enum ``poweredOn`` / ``poweredOff`` / ``suspended``. Passed through
  verbatim so the "poweredOn but no guest IP" hung-appliance shape is
  representable in one call.
* ``guest.ipAddress`` -- primary guest IP, or ``None`` when VMware Tools
  has not reported one (the #1 hung-appliance tell).
* ``guest.hostName`` -- guest OS hostname when Tools reports it.
* ``guest.toolsStatus`` / ``guest.toolsRunningStatus`` -- Tools install +
  running status.
* ``guestHeartbeatStatus`` (:class:`ManagedEntityStatus`) -- the
  gray / red / yellow / green guest-liveness colour.
* ``storage.perDatastoreUsage`` -- a list of
  :class:`VirtualMachineUsageOnDatastore` (``committed`` /
  ``uncommitted`` / ``unshared`` bytes per datastore).

Field names + units are the vim25 / VI-JSON wire contract (camelCase);
values are passed through without re-mapping the vim enums.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "VMWARE_VM_INFO_OP",
    "VM_INFO_GROUP_KEY",
    "VM_INFO_WHEN_TO_USE",
    "build_vm_info_retrieve_params",
    "vm_info_impl",
]

_log = structlog.get_logger(__name__)

# vCenter Automation REST VM listing (spec-relative; mounted onto /api or
# /rest per-target by mount_op_path). Used only to resolve a name -> moid
# when the caller addresses the VM by name.
_LIST_VMS_PATH = "/vcenter/vm"
# PropertyCollector RetrievePropertiesEx against the singleton
# ``propertyCollector`` moId (carried in the path, so the body is only
# the method arguments).
_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_VIRTUAL_MACHINE_MO_TYPE = "VirtualMachine"

_PROP_NAME = "name"
_PROP_POWER_STATE = "runtime.powerState"
_PROP_GUEST_IP = "guest.ipAddress"
_PROP_GUEST_HOSTNAME = "guest.hostName"
_PROP_TOOLS_STATUS = "guest.toolsStatus"
_PROP_TOOLS_RUNNING = "guest.toolsRunningStatus"
_PROP_HEARTBEAT = "guestHeartbeatStatus"
_PROP_PER_DATASTORE = "storage.perDatastoreUsage"
_VM_INFO_PATH_SET = (
    _PROP_NAME,
    _PROP_POWER_STATE,
    _PROP_GUEST_IP,
    _PROP_GUEST_HOSTNAME,
    _PROP_TOOLS_STATUS,
    _PROP_TOOLS_RUNNING,
    _PROP_HEARTBEAT,
    _PROP_PER_DATASTORE,
)

VM_INFO_GROUP_KEY = "vmware-vm-info"


def build_vm_info_retrieve_params(vm_moid: str) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` request body for one VM.

    A single ``PropertyFilterSpec`` scoped directly to the VirtualMachine
    object (no ContainerView / TraversalSpec) requesting the guest /
    runtime / storage property paths. The singleton ``propertyCollector``
    moId rides the path (:data:`_RETRIEVE_PROPERTIES_PATH`), so the body
    is just the ``specSet`` + ``options`` method arguments -- the VI-JSON
    ``RetrievePropertiesExRequestType`` shape ``vmware.host.usage`` sends.
    """
    return {
        "specSet": [
            {
                "propSet": [
                    {
                        "type": _VIRTUAL_MACHINE_MO_TYPE,
                        "pathSet": list(_VM_INFO_PATH_SET),
                    }
                ],
                "objectSet": [{"obj": {"type": _VIRTUAL_MACHINE_MO_TYPE, "value": vm_moid}}],
            }
        ],
        "options": {},
    }


def _extract_vm_props(retrieve_result: Any) -> dict[str, Any]:
    """Flatten a single-VM ``RetrievePropertiesEx`` result to name->val.

    ``RetrievePropertiesEx`` returns a ``RetrieveResult`` whose ``objects``
    list carries one ``ObjectContent`` per queried object, each with a
    ``propSet`` list of ``{name, val}`` pairs. For the single-VM query the
    first object's propSet holds the requested paths. A bare list (some
    simulators / the legacy ``RetrieveProperties`` shape) is tolerated.
    """
    payload = _unwrap_value(retrieve_result)
    if isinstance(payload, dict):
        objects = payload.get("objects", [])
    elif isinstance(payload, list):
        objects = payload
    else:
        objects = []
    prop_by_name: dict[str, Any] = {}
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and isinstance(prop.get("name"), str):
                prop_by_name[prop["name"]] = prop.get("val")
    return prop_by_name


def _moref_value(ref: Any) -> str | None:
    """Return the ``value`` moid from a VI-JSON MoRef, else ``None``.

    A VI-JSON managed-object reference serialises as
    ``{"type": ..., "value": "datastore-12"}``. Some intermediaries hand
    back a bare moid string; accept that too.
    """
    if isinstance(ref, dict):
        value = ref.get("value")
        return value if isinstance(value, str) else None
    return ref if isinstance(ref, str) else None


def _int_or_none(value: Any) -> int | None:
    """Coerce a JSON number / numeric-string to int; reject bools + junk.

    The ``VirtualMachineUsageOnDatastore`` byte counters are ``xsd:long``;
    some intermediaries render a 64-bit value as a JSON string. ``True``
    is an ``int`` subclass in Python and must not read as ``1``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _parse_per_datastore_usage(raw: Any) -> list[dict[str, Any]]:
    """Flatten ``storage.perDatastoreUsage`` into operator-facing rows.

    Each element is a ``VirtualMachineUsageOnDatastore``: a ``datastore``
    MoRef plus ``committed`` / ``uncommitted`` / ``unshared`` byte counts.
    The datastore MoRef is surfaced as its moid string.
    """
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for usage in raw:
        if not isinstance(usage, dict):
            continue
        rows.append(
            {
                "datastore": _moref_value(usage.get("datastore")),
                "committed_bytes": _int_or_none(usage.get("committed")),
                "uncommitted_bytes": _int_or_none(usage.get("uncommitted")),
                "unshared_bytes": _int_or_none(usage.get("unshared")),
            }
        )
    return rows


async def _resolve_vm_moid_by_name(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    name: str,
) -> str:
    """Resolve a VM name to its moid via ``GET /vcenter/vm`` (name filter).

    The listing filter param is keyed off the target's live mount flavor
    (#2298): modern ``/api`` vCenter 8.x wants the bare ``names`` name and
    400s the legacy ``filter.names`` prefixed form; the ``/rest`` mount
    (and ``vmware/vcsim``) requires the prefix.

    Raises :exc:`RuntimeError` when the name resolves to zero VMs
    (unknown) or more than one (ambiguous) -- the caller must then
    re-issue with the specific ``vm`` moid. Mirrors the resolution
    contract the composite listing enrichment uses.
    """
    list_path = await connector.mount_op_path(target, _LIST_VMS_PATH, operator)
    # Key the ``filter.names`` param off the mount flavor (#2298): the
    # session is warm from the mount_op_path call above, so adapt_op_query
    # resolves the flavor without an extra round-trip.
    listing_query = await connector.adapt_op_query(target, {"filter.names": [name]}, operator)
    listing = await connector._get_json(
        target, list_path, operator=operator, params=listing_query
    )
    entries = _unwrap_value(listing)
    if not isinstance(entries, list):
        raise RuntimeError(
            f"vmware.vm.info: expected a list of VMs from GET {_LIST_VMS_PATH!r}, "
            f"got {type(entries).__name__}"
        )
    moids: list[str] = [
        moid
        for entry in entries
        if isinstance(entry, dict)
        for moid in [entry.get("vm")]
        if isinstance(moid, str)
    ]
    if not moids:
        raise RuntimeError(f"vmware.vm.info: no VM named {name!r}")
    if len(moids) > 1:
        raise RuntimeError(
            f"vmware.vm.info: name {name!r} is ambiguous -- matches {len(moids)} VMs "
            f"({', '.join(moids)}); re-issue with the specific 'vm' moid"
        )
    return moids[0]


async def vm_info_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vm.info`` -- single-VM incident-triage read.

    Reads, directly on the connector session (no ``dispatch_child``, no
    ingested descriptor):

    1. When addressed by ``name``: ``GET /vcenter/vm`` (mounted) with the
       name filter param keyed off the mount flavor (bare ``names`` on
       modern ``/api``, ``filter.names`` on legacy ``/rest``; #2298) to
       resolve the name to a moid. Skipped when the caller supplies ``vm``
       directly.
    2. ``POST .../PropertyCollector/propertyCollector/RetrievePropertiesEx``
       (mounted) reading the VirtualMachine's runtime power state, guest
       IP / hostname / Tools status, heartbeat, and per-datastore usage.
       Load-bearing: a failure here raises and the dispatcher records it
       as a ``connector_error`` -- unlike the per-host best-effort reads,
       this single-object read *is* the op.

    Both calls route through :meth:`VmwareRestConnector.mount_op_path`, so
    the op lands on the ``/api`` (modern) or ``/rest`` (legacy / vcsim)
    mount the target's session selected.

    Returns a single flat row (see :data:`VMWARE_VM_INFO_OP`'s response
    schema).
    """
    vm_moid = params.get("vm")
    if not isinstance(vm_moid, str):
        vm_moid = await _resolve_vm_moid_by_name(connector, operator, target, params["name"])

    props_path = await connector.mount_op_path(target, _RETRIEVE_PROPERTIES_PATH, operator)
    props_result = await connector._post_json(
        target,
        props_path,
        operator=operator,
        json=build_vm_info_retrieve_params(vm_moid),
    )
    props = _extract_vm_props(props_result)

    row: dict[str, Any] = {
        "vm": vm_moid,
        "name": props.get(_PROP_NAME),
        "power_state": props.get(_PROP_POWER_STATE),
        "guest_ip": props.get(_PROP_GUEST_IP),
        "guest_hostname": props.get(_PROP_GUEST_HOSTNAME),
        "tools_status": props.get(_PROP_TOOLS_STATUS),
        "tools_running_status": props.get(_PROP_TOOLS_RUNNING),
        "heartbeat_status": props.get(_PROP_HEARTBEAT),
        "per_datastore_usage": _parse_per_datastore_usage(props.get(_PROP_PER_DATASTORE)),
    }
    _log.info(
        "vmware_vm_info_read",
        target=target.name,
        vm=vm_moid,
        power_state=row["power_state"],
        has_guest_ip=row["guest_ip"] is not None,
    )
    return row


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Managed-object ID of the VM (e.g. 'vm-42'). Supply exactly one of 'vm' or 'name'."
            ),
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name of the VM. Resolved to a moid via the VM listing; an "
                "unknown or ambiguous name is a structured error. Supply "
                "exactly one of 'vm' or 'name'."
            ),
        },
    },
    "oneOf": [
        {"required": ["vm"]},
        {"required": ["name"]},
    ],
    "additionalProperties": False,
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "description": "Managed-object ID of the VM read."},
        "name": {"type": ["string", "null"], "description": "VM name (``name`` property)."},
        "power_state": {
            "type": ["string", "null"],
            "description": (
                "Raw vim ``runtime.powerState`` enum: 'poweredOn' / 'poweredOff' / 'suspended'."
            ),
        },
        "guest_ip": {
            "type": ["string", "null"],
            "description": (
                "Primary guest IP from ``guest.ipAddress``; ``null`` when "
                "VMware Tools has not reported one (the hung-appliance tell)."
            ),
        },
        "guest_hostname": {
            "type": ["string", "null"],
            "description": "Guest OS hostname from ``guest.hostName``.",
        },
        "tools_status": {
            "type": ["string", "null"],
            "description": "VMware Tools status from ``guest.toolsStatus``.",
        },
        "tools_running_status": {
            "type": ["string", "null"],
            "description": "VMware Tools running status from ``guest.toolsRunningStatus``.",
        },
        "heartbeat_status": {
            "type": ["string", "null"],
            "description": (
                "Guest heartbeat colour from ``guestHeartbeatStatus`` "
                "(gray / red / yellow / green)."
            ),
        },
        "per_datastore_usage": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "datastore": {
                        "type": ["string", "null"],
                        "description": "Datastore moid the usage is reported for.",
                    },
                    "committed_bytes": {
                        "type": ["integer", "null"],
                        "description": "Bytes actually used by the VM on this datastore.",
                    },
                    "uncommitted_bytes": {
                        "type": ["integer", "null"],
                        "description": "Additional bytes potentially used on this datastore.",
                    },
                    "unshared_bytes": {
                        "type": ["integer", "null"],
                        "description": "Bytes on this datastore not shared with other VMs.",
                    },
                },
                "required": ["datastore"],
            },
            "description": "Per-datastore usage from ``storage.perDatastoreUsage``.",
        },
    },
    "required": ["vm"],
}

#: Curated ``when_to_use`` blurb for the vm-info group.
VM_INFO_WHEN_TO_USE = (
    "Use to triage a single VM's live state: power state (raw vim enum), "
    "guest IP, VMware Tools status + running status, guest heartbeat "
    "colour, and per-datastore storage usage -- addressed by moid or "
    "name. The #1 hung-appliance read: a VM that is 'poweredOn' but has "
    "no guest_ip and a red/gray heartbeat is stuck. Reads the "
    "VirtualMachine managed object's runtime.powerState, guest.*, "
    "guestHeartbeatStatus, and storage.perDatastoreUsage via "
    "PropertyCollector directly on the connector session, so it works "
    "with zero catalog ingest -- the plain vCenter REST VM detail reports "
    "configuration, not these live guest signals. Read-only."
)

VMWARE_VM_INFO_OP = VmwareTypedOp(
    op_id="vmware.vm.info",
    handler_attr="vm_info",
    summary="Single VM: power, guest IP, Tools status, heartbeat, per-datastore usage.",
    description=(
        "Returns one VM's live incident-triage signals addressed by moid "
        "('vm') or 'name': power_state (raw vim runtime.powerState enum), "
        "guest_ip (guest.ipAddress), guest_hostname, tools_status + "
        "tools_running_status, heartbeat_status (guestHeartbeatStatus "
        "colour), and per_datastore_usage (committed / uncommitted / "
        "unshared bytes per datastore). The #1 hung-appliance tell: a VM "
        "'poweredOn' with no guest_ip is representable in one call. Reads "
        "the VirtualMachine managed object via PropertyCollector directly "
        "on the connector session, so it works with zero catalog ingest -- "
        "the plain vCenter REST VM detail reports configuration, not these "
        "live guest signals. An unknown / ambiguous 'name' is a structured "
        "error. safety_level=safe, read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=VM_INFO_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vm", "guest", "incident"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator asks whether a VM is up, has an IP, has "
            "VMware Tools running, has a healthy heartbeat, or how much "
            "datastore space it uses -- especially to diagnose a hung / "
            "unreachable appliance. Address the VM by 'vm' moid or 'name'."
        ),
        "parameter_hints": {
            "vm": "VM moid (e.g. 'vm-42'); supply exactly one of vm / name.",
            "name": "VM name; resolved to a moid, unknown/ambiguous is an error.",
        },
        "output_shape": (
            "{vm, name, power_state, guest_ip, guest_hostname, tools_status, "
            "tools_running_status, heartbeat_status, per_datastore_usage: "
            "[{datastore, committed_bytes, uncommitted_bytes, unshared_bytes}]}."
        ),
    },
)
