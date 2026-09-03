# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.host.storage_devices`` typed read op (#3332).

Enumerates a host's raw SCSI storage devices -- per-LUN ``uuid`` +
``canonical_name`` + ``ssd`` / ``local`` flags + capacity + model /
vendor -- the runtime input ``vmware.composite.host.disk_mark_flash``
needs. Before this op the only host storage reads were ``vsan_health`` /
``datastore.usage`` / ``network_uplinks``; none enumerate the raw device
set, so a caller that wants to flash-mark "every non-boot disk" could
not discover the devices through the backplane and had to pre-supply
UUIDs it could only get out-of-band.

In the :mod:`~meho_backplane.connectors.vmware_rest.typed_ops` mould
(``vmware.host.usage`` / ``.network_uplinks``): a ``source_kind="typed"``
bound method that reads directly on the connector session -- no
``dispatch_child``, no ingested descriptor -- so it works on a **fresh
boot with zero catalog ingest**. That property is load-bearing here: the
#3332 use case is a **standalone ESXi** host no vCenter manages yet (a
nested management-domain bring-up before the SDDC vCenter exists), where
no vCenter catalog can have been ingested.

Both target flavors are covered (#3332, via
:func:`~meho_backplane.connectors.vmware_rest.host_target.classify_host_target`):

* a **standalone ESXi** target -- fingerprint ``product=esxi`` -- has one
  host, the well-known ``ha-host``; the ``host`` param is ignored (the
  host is the target);
* a **vCenter** target resolves the ``host`` param (display name or
  moref) through ``GET:/vcenter/host`` (name-first, moref-fallback), the
  same ladder the host write composites use.

The device rows come from the Web-Services-API
``HostSystem.config.storageDevice.scsiLun`` property (the host-side
read mirror of ``HostStorageSystem.storageDeviceInfo.scsiLun`` -- the
same ``HostScsiDisk`` / ``ScsiLun`` array), read via the PropertyCollector
``RetrievePropertiesEx`` vi-json method routed through
:meth:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector._post_vmomi_json`
(mounted on the documented ``/sdk/vim25/{release}`` base). The response
is set-shaped, so the dispatcher JSONFlux-reduces it to a handle when
large -- the same auto-reduction the other typed reads rely on.

``ssd`` / ``local`` / ``capacity_bytes`` are ``HostScsiDisk`` fields (the
``deviceType="disk"`` subclass of ``ScsiLun``); a non-disk LUN (cdrom /
tape) carries ``null`` for them. ``is_boot`` is **best-effort**: the
``scsiLun`` property surface carries no boot flag (identifying the ESXi
boot device reliably needs the esxcli ``storage core device list`` "Is
Boot Device" datum, not a managed-object property), so it is ``null``
here and reserved for a future esxcli-backed enrichment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.host_target import (
    HOST_FLAVOR_ESXI,
    STANDALONE_ESXI_HOST_MOID,
    classify_host_target,
)
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.vim_body import (
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "HOST_STORAGE_DEVICES_GROUP_KEY",
    "HOST_STORAGE_DEVICES_WHEN_TO_USE",
    "VMWARE_HOST_STORAGE_DEVICES_OP",
    "build_host_storage_devices_retrieve_params",
    "host_storage_devices_impl",
]

_log = structlog.get_logger(__name__)

# vCenter Automation REST host listing (spec-relative; mounted onto
# /api or /rest per-target by mount_op_path).
_LIST_HOSTS_PATH = "/vcenter/host"
# PropertyCollector RetrievePropertiesEx against the singleton
# ``propertyCollector`` moId (carried in the path, so the body is only
# the method arguments).
_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_HOST_SYSTEM_MO_TYPE = "HostSystem"
# HostSystem-side mirror of HostStorageSystem.storageDeviceInfo.scsiLun
# (the same HostStorageDeviceInfo object, cached on config). Narrowed to
# the scsiLun leaf so the read does not pull the whole storageDevice
# object (HBAs / multipath / plug-store topology).
_PROP_SCSI_LUN = "config.storageDevice.scsiLun"
_STORAGE_DEVICE_PATH_SET = (_PROP_SCSI_LUN,)

HOST_STORAGE_DEVICES_GROUP_KEY = "vmware-host-storage-devices"


def _str_or_none(value: Any) -> str | None:
    """Return a trimmed string, or ``None`` for anything else.

    SCSI inquiry strings (``model`` / ``vendor``) arrive space-padded from
    the WS-API, so they are trimmed; identifiers (``uuid`` /
    ``canonicalName``) carry no padding and pass through the same trim
    harmlessly.
    """
    return value.strip() if isinstance(value, str) else None


def _bool_or_none(value: Any) -> bool | None:
    """Coerce a WS-API boolean to ``bool``; anything else -> ``None``.

    ``ssd`` / ``localDisk`` are ``HostScsiDisk``-only fields, absent on a
    non-disk ``ScsiLun`` -- their absence reads as ``None``.
    """
    return value if isinstance(value, bool) else None


def _int_or_none(value: Any) -> int | None:
    """Coerce a JSON number / numeric string to int; anything else -> ``None``.

    Rejects bools (``True`` is an ``int`` subclass and must not read as 1).
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


def _capacity_bytes(capacity: Any) -> int | None:
    """Compute a LUN's capacity in bytes from ``HostDiskDimensionsLba``.

    ``HostScsiDisk.capacity`` is ``{blockSize, block}`` (bytes-per-block x
    block count); the byte total is their product. Absent (non-disk LUN)
    or partial -> ``None``.
    """
    if not isinstance(capacity, dict):
        return None
    block_size = _int_or_none(capacity.get("blockSize"))
    block = _int_or_none(capacity.get("block"))
    if block_size is None or block is None:
        return None
    return block_size * block


def _map_scsi_lun(lun: dict[str, Any]) -> dict[str, Any]:
    """Flatten one WS-API ``ScsiLun`` / ``HostScsiDisk`` into an operator row.

    ``is_boot`` is best-effort ``None``: the ``scsiLun`` property surface
    exposes no boot flag (see the module docstring); it is reserved for a
    future esxcli-backed enrichment so a caller excluding the boot device
    has a stable field to read.
    """
    return {
        "uuid": _str_or_none(lun.get("uuid")),
        "canonical_name": _str_or_none(lun.get("canonicalName")),
        "device_type": _str_or_none(lun.get("deviceType")),
        "capacity_bytes": _capacity_bytes(lun.get("capacity")),
        "ssd": _bool_or_none(lun.get("ssd")),
        "local": _bool_or_none(lun.get("localDisk")),
        "model": _str_or_none(lun.get("model")),
        "vendor": _str_or_none(lun.get("vendor")),
        "is_boot": None,
    }


def _extract_scsi_luns(retrieve_result: Any) -> list[Any]:
    """Pull the ``config.storageDevice.scsiLun`` array from a RetrievePropertiesEx result.

    ``RetrievePropertiesEx`` returns a ``RetrieveResult`` whose ``objects``
    list carries one ``ObjectContent`` per queried object, each with a
    ``propSet`` list of ``{name, val}`` pairs. For the single-host query
    the first object's propSet holds the requested scsiLun array (boxed as
    ``ArrayOf*`` -- :func:`unwrap_vim_value` un-boxes it). Absent -> ``[]``.
    """
    payload = _unwrap_value(retrieve_result)
    if isinstance(payload, dict):
        objects = payload.get("objects", [])
    elif isinstance(payload, list):
        objects = payload
    else:
        objects = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and prop.get("name") == _PROP_SCSI_LUN:
                luns = unwrap_vim_value(prop.get("val"))
                return luns if isinstance(luns, list) else []
    return []


def build_host_storage_devices_retrieve_params(host_moid: str) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` request body for one host's scsiLun array.

    A single ``PropertyFilterSpec`` scoped directly to the ``HostSystem``
    object (no ContainerView / TraversalSpec) requesting the one
    ``config.storageDevice.scsiLun`` property path -- the VI-JSON
    ``RetrievePropertiesExRequestType`` shape the other typed reads send,
    ``_typeName``-annotated via the shared trio helper (#3103).
    """
    return retrieve_properties_body(_HOST_SYSTEM_MO_TYPE, [host_moid], _STORAGE_DEVICE_PATH_SET)


async def _list_host_moids(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    query: dict[str, Any],
) -> list[str]:
    """Return the ``host`` moids from one ``GET:/vcenter/host`` listing.

    Routes through :meth:`VmwareRestConnector.mount_op_path` (``/api``
    modern / ``/rest`` legacy) and keys the filter param off the mount
    flavor via :meth:`adapt_op_query` (#2298), the same way the sibling
    typed reads issue their host listing.
    """
    list_path = await connector.mount_op_path(target, _LIST_HOSTS_PATH, operator)
    listing_query = await connector.adapt_op_query(target, query, operator)
    listing = await connector._get_json(target, list_path, operator=operator, params=listing_query)
    rows = _unwrap_value(listing)
    if not isinstance(rows, list):
        return []
    return [
        row["host"] for row in rows if isinstance(row, dict) and isinstance(row.get("host"), str)
    ]


async def _resolve_host_moid(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    host: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve the target's host to a ``HostSystem`` moid, or a refusal envelope.

    Branches on the target fingerprint (:func:`classify_host_target`,
    #3332): a standalone-ESXi target resolves to the well-known
    :data:`STANDALONE_ESXI_HOST_MOID` (``host`` ignored); a vCenter target
    resolves ``host`` (display name first, moref fallback) via
    ``GET:/vcenter/host``; any other reachable product / a missing host /
    an unresolved host fails closed. Returns ``(moid, None)`` on success or
    ``(None, refusal)`` -- the refusal is the full op envelope (empty
    ``devices``).
    """
    flavor, refusal = classify_host_target(target)
    if refusal is not None:
        return None, {**refusal, "host": host, "devices": [], "device_count": 0}
    if flavor == HOST_FLAVOR_ESXI:
        return STANDALONE_ESXI_HOST_MOID, None
    if not host:
        return None, {
            "status": "host_required",
            "host": host,
            "devices": [],
            "device_count": 0,
            "guidance": "a vCenter target requires a host display name or moref",
        }
    by_name = await _list_host_moids(connector, operator, target, {"filter.names": [host]})
    if len(by_name) == 1:
        return by_name[0], None
    if len(by_name) > 1:
        return None, {
            "status": "ambiguous_host",
            "host": host,
            "candidate_hosts": by_name,
            "devices": [],
            "device_count": 0,
        }
    by_moref = await _list_host_moids(connector, operator, target, {"filter.hosts": [host]})
    if len(by_moref) == 1:
        return by_moref[0], None
    return None, {"status": "host_not_found", "host": host, "devices": [], "device_count": 0}


async def host_storage_devices_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.host.storage_devices`` -- per-host raw SCSI devices.

    Resolves the host (standalone-ESXi ``ha-host`` or a vCenter
    name/moref, #3332), then reads ``config.storageDevice.scsiLun`` off one
    PropertyCollector ``RetrievePropertiesEx`` on the connector session and
    maps each LUN to a row. The read is fail-closed: a VI-JSON transport /
    status failure (the seam unavailable, an ESXi that rejects the call)
    returns a typed ``status='storage_devices_unreadable'`` with an empty
    device set and a ``read_note`` rather than a bare stack trace.

    Returns ``{status, host, devices: [{uuid, canonical_name, device_type,
    capacity_bytes, ssd, local, model, vendor, is_boot}, ...], device_count}``.
    Set-shaped, so the dispatcher JSONFlux-reduces it to a handle when large.
    """
    host_moid, refusal = await _resolve_host_moid(connector, operator, target, params.get("host"))
    if refusal is not None:
        return refusal
    assert host_moid is not None  # refusal is None => the host resolved (mypy narrowing).
    try:
        props_result = await connector._post_vmomi_json(
            target,
            _RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=build_host_storage_devices_retrieve_params(host_moid),
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        return {
            "status": "storage_devices_unreadable",
            "host": host_moid,
            "devices": [],
            "device_count": 0,
            "read_note": (
                f"storage-device read failed: RetrievePropertiesEx for host "
                f"{host_moid!r} failed with {type(exc).__name__}: {exc}"
            ),
        }
    devices = [
        _map_scsi_lun(lun) for lun in _extract_scsi_luns(props_result) if isinstance(lun, dict)
    ]
    _log.info(
        "vmware_host_storage_devices_read",
        target=target.name,
        host=host_moid,
        device_count=len(devices),
    )
    return {"status": "ok", "host": host_moid, "devices": devices, "device_count": len(devices)}


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Host display name or moref (e.g. 'esxi-01' or 'host-15') to "
                "enumerate devices for. Resolved via GET:/vcenter/host — a "
                "display-name lookup first, moref fallback; ambiguous names "
                "return status='ambiguous_host'. Required on a vCenter target "
                "(else status='host_required'). **Optional / ignored on a "
                "standalone-ESXi target** (fingerprint product=esxi): that "
                "target has exactly one host (the well-known ha-host), so the "
                "host is the target and this param is not needed."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
}

_DEVICE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "uuid": {
            "type": ["string", "null"],
            "description": "ScsiLun.uuid — the stable device UUID disk_mark_flash takes.",
        },
        "canonical_name": {
            "type": ["string", "null"],
            "description": "ScsiLun.canonicalName (e.g. 'naa.…' / 'mpx.vmhba0:C0:T0:L0').",
        },
        "device_type": {
            "type": ["string", "null"],
            "description": "ScsiLun.deviceType (e.g. 'disk', 'cdrom').",
        },
        "capacity_bytes": {
            "type": ["integer", "null"],
            "description": (
                "Capacity in bytes (HostScsiDisk.capacity.blockSize x block); "
                "null on a non-disk LUN."
            ),
        },
        "ssd": {
            "type": ["boolean", "null"],
            "description": (
                "HostScsiDisk.ssd — true when the disk is presented as flash; "
                "null on a non-disk LUN. This is the flag disk_mark_flash toggles."
            ),
        },
        "local": {
            "type": ["boolean", "null"],
            "description": (
                "HostScsiDisk.localDisk — true when the disk is host-local; null on a non-disk LUN."
            ),
        },
        "model": {"type": ["string", "null"], "description": "ScsiLun.model (trimmed)."},
        "vendor": {"type": ["string", "null"], "description": "ScsiLun.vendor (trimmed)."},
        "is_boot": {
            "type": ["boolean", "null"],
            "description": (
                "Best-effort boot-device flag. The scsiLun property surface "
                "carries no boot marker, so this is null; reserved for a future "
                "esxcli-backed enrichment. Callers excluding the boot device "
                "today do so by the UUIDs they provisioned."
            ),
        },
    },
    "required": ["uuid", "canonical_name"],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "ok",
                "host_required",
                "host_not_found",
                "ambiguous_host",
                "unsupported_host_target",
                "storage_devices_unreadable",
            ],
            "description": (
                "'ok' — devices enumerated; the refusal statuses are reached "
                "before (host resolution) or in place of (VI-JSON read "
                "failure) the device read."
            ),
        },
        "host": {
            "type": ["string", "null"],
            "description": (
                "Resolved host moid (ha-host on a standalone-ESXi target), or the input on refusal."
            ),
        },
        "devices": {
            "type": "array",
            "items": _DEVICE_ITEM_SCHEMA,
            "description": "One row per SCSI LUN (empty on any non-'ok' status).",
        },
        "device_count": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Length of ``devices`` (the true count; the array may be JSONFlux-reduced)."
            ),
        },
        "candidate_hosts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Matched host moids on 'ambiguous_host'.",
        },
        "read_note": {
            "type": "string",
            "description": "On 'storage_devices_unreadable', the failing method + error.",
        },
        "guidance": {"type": ["string", "null"]},
    },
    "required": ["status", "host", "devices", "device_count"],
}

#: Curated ``when_to_use`` blurb for the host-storage-devices group.
HOST_STORAGE_DEVICES_WHEN_TO_USE = (
    "Use to enumerate the raw SCSI storage devices on an ESXi host — each "
    "LUN's uuid, canonical_name, device_type, capacity_bytes, and the "
    "ssd / local flags (plus model / vendor). The one read that surfaces "
    "the per-disk UUIDs and flash state vsan_health / datastore.usage / "
    "network_uplinks do not. The right op when the question is 'which "
    "disks does this host have?', 'which are flagged flash (ssd)?', or "
    "'what UUIDs do I feed host.disk_mark_flash?' — including on a "
    "standalone ESXi host that no vCenter manages yet (a pre-vCenter "
    "management-domain bring-up), where the host param is ignored and the "
    "host is the target. Read-only."
)

VMWARE_HOST_STORAGE_DEVICES_OP = VmwareTypedOp(
    op_id="vmware.host.storage_devices",
    handler_attr="host_storage_devices",
    summary=(
        "Per-host raw SCSI storage devices — per-LUN uuid, ssd/local flags, capacity, model/vendor."
    ),
    description=(
        "Returns one row per SCSI LUN on an ESXi host: uuid "
        "(ScsiLun.uuid — the id host.disk_mark_flash takes), canonical_name, "
        "device_type, capacity_bytes (bytes; null on non-disk LUNs), ssd "
        "(flash flag) and local (host-local flag) from HostScsiDisk, plus "
        "trimmed model / vendor. is_boot is a best-effort null (the scsiLun "
        "property surface exposes no boot flag). Reads "
        "HostSystem.config.storageDevice.scsiLun (the read mirror of "
        "HostStorageSystem.storageDeviceInfo.scsiLun) via a PropertyCollector "
        "RetrievePropertiesEx directly on the connector session, so it works "
        "with zero catalog ingest. Works on a vCenter target (with host "
        "name/moref resolution) and on a standalone ESXi target no vCenter "
        "manages yet (host param ignored — the host is the target, #3332). "
        "The device read is fail-closed: a VI-JSON failure returns "
        "status='storage_devices_unreadable' with an empty device set. "
        "Set-shaped (JSONFlux-reduced to a handle when large). "
        "safety_level=safe, read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=HOST_STORAGE_DEVICES_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "esxi", "host", "storage", "devices"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to discover a host's raw disks before flash-marking them — "
            "the per-LUN uuid + ssd flag host.disk_mark_flash needs, which the "
            "other host storage reads do not surface. Works on a standalone "
            "ESXi host (no managing vCenter) as well as through a vCenter."
        ),
        "parameter_hints": {
            "host": (
                "Host display name or moref on a vCenter target; omit on a "
                "standalone-ESXi target (the host is the target)."
            ),
        },
        "output_shape": (
            "{status, host, devices: [{uuid, canonical_name, device_type, "
            "capacity_bytes, ssd, local, model, vendor, is_boot}], "
            "device_count}. Refusals (host_required / host_not_found / "
            "ambiguous_host / unsupported_host_target / "
            "storage_devices_unreadable) carry an empty devices array."
        ),
    },
)
