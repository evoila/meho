# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.host.storage_devices`` typed read op (#3332).

Enumerates a host's raw SCSI storage devices -- per-LUN ``uuid`` +
``canonical_name`` + ``ssd`` / ``local`` flags + capacity + model / vendor
+ a best-effort ``is_boot`` flag -- the runtime input
``vmware.composite.host.disk_mark_flash`` needs to flash-mark "every
non-boot disk" (the other host storage reads do not enumerate the raw
device set).

A ``source_kind="typed"`` bound method in the ``vmware.host.usage`` mould:
reads directly on the connector session (no ``dispatch_child``, no ingested
descriptor), so it works on a **fresh boot with zero catalog ingest** --
load-bearing here, since the #3332 case is a **standalone ESXi** host no
vCenter manages yet. Host resolution branches on the fingerprint
(:func:`~meho_backplane.connectors.vmware_rest.host_target.classify_host_target`):
a standalone-ESXi target (``product=esxi``) resolves the well-known
``ha-host`` (``host`` ignored); a vCenter target resolves the ``host``
param (name / moref) via ``GET:/vcenter/host``.

Device rows come from ``HostSystem.config.storageDevice.scsiLun`` (the
read mirror of ``HostStorageSystem.storageDeviceInfo.scsiLun``), read via
one PropertyCollector ``RetrievePropertiesEx`` on the VI-JSON
``/sdk/vim25/{release}`` seam; set-shaped, so JSONFlux-reduced when large.
``ssd`` / ``local`` / ``capacity_bytes`` are ``HostScsiDisk`` fields (the
``deviceType="disk"`` subclass), ``null`` on a non-disk LUN.

Boot-device identification needs **no esxcli seam**: the same read also
fetches ``configManager.bootDeviceSystem``, and when present the op calls
``HostBootDeviceSystem.QueryBootDevices`` (a vim query over the same seam)
and matches its ``currentBootDeviceKey`` -- which typically embeds the
device's canonical name / uuid -- against the rows (``is_boot`` true on
the match, false on the rest). **Fail-safe**: an absent config manager, an
unsupported / erroring query, or a missing key nulls every ``is_boot`` and
names the reason in ``boot_device_resolution`` / ``boot_device_note`` --
the listing is still returned.
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
# (cached on config), narrowed to the scsiLun leaf; paired with the
# bootDeviceSystem config-manager MoRef so one read backs both the device
# list and the boot-device query.
_PROP_SCSI_LUN = "config.storageDevice.scsiLun"
_PROP_BOOT_DEVICE_SYSTEM = "configManager.bootDeviceSystem"
_STORAGE_DEVICE_PATH_SET = (_PROP_SCSI_LUN, _PROP_BOOT_DEVICE_SYSTEM)
# HostBootDeviceSystem.QueryBootDevices vim method (the moId rides the
# path); posted through the same VI-JSON seam as RetrievePropertiesEx.
_QUERY_BOOT_DEVICES_PATH = "/HostBootDeviceSystem/{moid}/QueryBootDevices"
# Minimum identifier length trusted for a substring match against the boot
# key -- guards a short id from spuriously matching (fail-safe).
_MIN_BOOT_MATCH_LEN = 6

HOST_STORAGE_DEVICES_GROUP_KEY = "vmware-host-storage-devices"


def _str_or_none(value: Any) -> str | None:
    """Trimmed string (``model`` / ``vendor`` are space-padded), else ``None``."""
    return value.strip() if isinstance(value, str) else None


def _bool_or_none(value: Any) -> bool | None:
    """Coerce a WS-API boolean to ``bool``; anything else -> ``None`` (a non-disk LUN)."""
    return value if isinstance(value, bool) else None


def _int_or_none(value: Any) -> int | None:
    """Coerce a JSON number / numeric string to int; anything else (incl. bool) -> ``None``."""
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
    """Bytes from ``HostScsiDisk.capacity`` (``{blockSize, block}`` product); else ``None``."""
    if not isinstance(capacity, dict):
        return None
    block_size = _int_or_none(capacity.get("blockSize"))
    block = _int_or_none(capacity.get("block"))
    if block_size is None or block is None:
        return None
    return block_size * block


def _matches_boot(uuid: str | None, canonical_name: str | None, boot_key: str) -> bool:
    """Whether a scsiLun row is the current boot device.

    ``currentBootDeviceKey`` typically embeds the canonical name / uuid;
    match by containment either way, guarded to identifiers >=
    :data:`_MIN_BOOT_MATCH_LEN` chars (fail-safe: no match on a missing id).
    """
    key_l = boot_key.strip().lower()
    if not key_l:
        return False
    for ident in (canonical_name, uuid):
        if not ident:
            continue
        ident_l = ident.lower()
        if len(ident_l) >= _MIN_BOOT_MATCH_LEN and (ident_l in key_l or key_l in ident_l):
            return True
    return False


def _map_scsi_lun(lun: dict[str, Any], boot_key: str | None) -> dict[str, Any]:
    """Flatten one ``ScsiLun`` / ``HostScsiDisk`` row (``is_boot``: null when
    *boot_key* is None, else whether this LUN matches the boot key)."""
    uuid = _str_or_none(lun.get("uuid"))
    canonical_name = _str_or_none(lun.get("canonicalName"))
    return {
        "uuid": uuid,
        "canonical_name": canonical_name,
        "device_type": _str_or_none(lun.get("deviceType")),
        "capacity_bytes": _capacity_bytes(lun.get("capacity")),
        "ssd": _bool_or_none(lun.get("ssd")),
        "local": _bool_or_none(lun.get("localDisk")),
        "model": _str_or_none(lun.get("model")),
        "vendor": _str_or_none(lun.get("vendor")),
        "is_boot": None if boot_key is None else _matches_boot(uuid, canonical_name, boot_key),
    }


def _extract_host_props(retrieve_result: Any) -> dict[str, Any]:
    """Flatten a single-host ``RetrievePropertiesEx`` result to ``{name: val}``.

    Each ``val`` is un-boxed via :func:`unwrap_vim_value`.
    """
    payload = _unwrap_value(retrieve_result)
    if isinstance(payload, dict):
        objects = payload.get("objects", [])
    elif isinstance(payload, list):
        objects = payload
    else:
        objects = []
    props: dict[str, Any] = {}
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and isinstance(prop.get("name"), str):
                props[prop["name"]] = unwrap_vim_value(prop.get("val"))
    return props


def _moref_value(val: Any) -> str | None:
    """Extract the ``value`` moid from a ``ManagedObjectReference`` dict, else ``None``."""
    if isinstance(val, dict):
        value = val.get("value")
        return value if isinstance(value, str) and value else None
    return None


def build_host_storage_devices_retrieve_params(host_moid: str) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` request body for one host.

    One ``PropertyFilterSpec`` scoped to the ``HostSystem`` requesting
    ``config.storageDevice.scsiLun`` (the device array) +
    ``configManager.bootDeviceSystem`` (so the boot query needs no extra
    read) -- ``_typeName``-annotated via the shared trio helper (#3103).
    """
    return retrieve_properties_body(_HOST_SYSTEM_MO_TYPE, [host_moid], _STORAGE_DEVICE_PATH_SET)


async def _resolve_boot_device_key(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    boot_system_moid: str | None,
) -> tuple[str | None, str | None]:
    """Best-effort current-boot-device key via ``HostBootDeviceSystem.QueryBootDevices``.

    Returns ``(key, None)`` on success. **Fail-safe**: an absent config
    manager, a transport / status / unsupported-method failure, or a missing
    ``currentBootDeviceKey`` yields ``(None, reason)`` (``is_boot`` null, the
    listing still returned). ``QueryBootDevices`` is a read, so it rides the
    un-gated :meth:`VmwareRestConnector._post_vmomi_json` seam, not the write
    sub-op seam.
    """
    if not boot_system_moid:
        return None, "host exposes no configManager.bootDeviceSystem"
    path = _QUERY_BOOT_DEVICES_PATH.format(moid=boot_system_moid)
    try:
        payload = await connector._post_vmomi_json(target, path, operator=operator, json={})
    except (httpx.HTTPError, RuntimeError) as exc:
        return None, f"QueryBootDevices failed: {type(exc).__name__}: {exc}"
    info = unwrap_vim_value(_unwrap_value(payload))
    key = info.get("currentBootDeviceKey") if isinstance(info, dict) else None
    if isinstance(key, str) and key.strip():
        return key.strip(), None
    return None, "QueryBootDevices returned no currentBootDeviceKey"


async def _list_host_moids(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    query: dict[str, Any],
) -> list[str]:
    """Return the ``host`` moids from one ``GET:/vcenter/host`` listing.

    Mounted via :meth:`mount_op_path` and filter-adapted via
    :meth:`adapt_op_query` (#2298), like the sibling typed reads.
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

    Branches on the fingerprint (:func:`classify_host_target`, #3332): a
    standalone-ESXi target resolves the well-known
    :data:`STANDALONE_ESXI_HOST_MOID` (``host`` ignored); a vCenter target
    resolves ``host`` (name first, moref fallback) via ``GET:/vcenter/host``;
    any other reachable product / missing / unresolved host fails closed.
    Returns ``(moid, None)`` or ``(None, refusal)`` (the full op envelope).
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

    Resolves the host (#3332), reads ``config.storageDevice.scsiLun`` +
    ``configManager.bootDeviceSystem`` off one ``RetrievePropertiesEx``,
    best-effort resolves the boot device via ``QueryBootDevices``, and maps
    each LUN. The device read is **fail-closed**
    (``storage_devices_unreadable``); the boot resolution is **fail-safe**
    (nulls ``is_boot`` + names the reason without sinking the listing).
    Returns ``{status, host, devices[], device_count,
    current_boot_device_key, boot_device_resolution, boot_device_note}`` --
    set-shaped, JSONFlux-reduced when large.
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
    props = _extract_host_props(props_result)
    raw_luns = props.get(_PROP_SCSI_LUN)
    scsi_luns = raw_luns if isinstance(raw_luns, list) else []
    boot_system_moid = _moref_value(props.get(_PROP_BOOT_DEVICE_SYSTEM))
    boot_key, boot_note = await _resolve_boot_device_key(
        connector, operator, target, boot_system_moid
    )
    devices = [_map_scsi_lun(lun, boot_key) for lun in scsi_luns if isinstance(lun, dict)]
    if boot_key is None:
        boot_resolution = "unavailable"
    elif any(device["is_boot"] for device in devices):
        boot_resolution = "matched"
    else:
        boot_resolution = "no_match"
    _log.info(
        "vmware_host_storage_devices_read",
        target=target.name,
        host=host_moid,
        device_count=len(devices),
        boot_device_resolution=boot_resolution,
    )
    return {
        "status": "ok",
        "host": host_moid,
        "devices": devices,
        "device_count": len(devices),
        "current_boot_device_key": boot_key,
        "boot_device_resolution": boot_resolution,
        "boot_device_note": boot_note,
    }


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
                "Whether this LUN is the host's current boot device — matched "
                "from QueryBootDevices' currentBootDeviceKey against "
                "canonical_name / uuid. true = boot, false = not, null = boot "
                "resolution unavailable (see boot_device_resolution). Lets a "
                "caller flash-mark 'every non-boot disk' (is_boot != true)."
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
        "current_boot_device_key": {
            "type": ["string", "null"],
            "description": (
                "HostBootDeviceInfo.currentBootDeviceKey echoed when the boot "
                "query resolved; null when boot resolution was unavailable."
            ),
        },
        "boot_device_resolution": {
            "type": "string",
            "enum": ["matched", "no_match", "unavailable"],
            "description": (
                "'matched' — a LUN matched the boot key (its is_boot true); "
                "'no_match' — key resolved but matched no LUN (all false, key "
                "echoed); 'unavailable' — boot query could not resolve (all "
                "null; see boot_device_note). Only on 'ok'."
            ),
        },
        "boot_device_note": {
            "type": ["string", "null"],
            "description": (
                "On boot_device_resolution='unavailable', the reason (no "
                "bootDeviceSystem, QueryBootDevices failed, or no "
                "currentBootDeviceKey); null otherwise."
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
    "LUN's uuid, canonical_name, device_type, capacity_bytes, the ssd / local "
    "flags (plus model / vendor), and is_boot (the current boot device). The "
    "one read that surfaces the per-disk UUIDs, flash state, and boot-device "
    "flag vsan_health / datastore.usage / network_uplinks do not — the right "
    "op for 'which disks does this host have?', 'which are flash (ssd)?', or "
    "'which UUIDs do I feed host.disk_mark_flash to flash-mark every non-boot "
    "disk?', including on a standalone ESXi host no vCenter manages yet (host "
    "param ignored — the host is the target). Read-only."
)

VMWARE_HOST_STORAGE_DEVICES_OP = VmwareTypedOp(
    op_id="vmware.host.storage_devices",
    handler_attr="host_storage_devices",
    summary=(
        "Per-host raw SCSI storage devices — per-LUN uuid, ssd/local flags, "
        "capacity, model/vendor, and is_boot."
    ),
    description=(
        "Returns one row per SCSI LUN on an ESXi host: uuid (ScsiLun.uuid — "
        "the id host.disk_mark_flash takes), canonical_name, device_type, "
        "capacity_bytes (null on non-disk LUNs), ssd (flash) and local from "
        "HostScsiDisk, trimmed model / vendor, and is_boot (the current boot "
        "device — matched from HostBootDeviceSystem.QueryBootDevices over the "
        "same VI-JSON seam; null when unavailable, see boot_device_resolution). "
        "Reads config.storageDevice.scsiLun + configManager.bootDeviceSystem "
        "via a PropertyCollector RetrievePropertiesEx directly on the connector "
        "session (zero catalog ingest), on a vCenter target (host name/moref "
        "resolution) and a standalone ESXi target no vCenter manages yet (host "
        "param ignored — the host is the target, #3332). Device read fail-closed "
        "(storage_devices_unreadable); boot resolution fail-safe. Set-shaped, "
        "JSONFlux-reduced when large. safety_level=safe, read-only."
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
            "the per-LUN uuid + ssd flag + is_boot host.disk_mark_flash needs to "
            "flash-mark every non-boot disk, which the other host storage reads "
            "do not surface. Works on a standalone ESXi host (no managing "
            "vCenter) as well as through a vCenter."
        ),
        "parameter_hints": {
            "host": (
                "Host display name or moref on a vCenter target; omit on a "
                "standalone-ESXi target (the host is the target)."
            ),
        },
        "output_shape": (
            "{status, host, devices: [{uuid, canonical_name, device_type, "
            "capacity_bytes, ssd, local, model, vendor, is_boot}], device_count, "
            "current_boot_device_key, boot_device_resolution, boot_device_note}. "
            "To flash-mark non-boot disks, feed the uuids of rows where is_boot "
            "!= true to host.disk_mark_flash. Refusals (host_required / "
            "host_not_found / ambiguous_host / unsupported_host_target / "
            "storage_devices_unreadable) carry an empty devices array."
        ),
    },
)
