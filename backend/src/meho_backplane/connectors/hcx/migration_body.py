# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vendor bodies and answer rows of the HCX migration operations.

:func:`group_item` turns the MEHO ``group`` input (``hcx.migration.start``,
``hcx.migration.validate``, ``hcx.mobility_group.set``) into one vendor
mobility group. The field names come from the vendor reference "HCX
Workload Migration APIs" (``mobilityGroup``, ``siteDetails``,
``transferParams``, ``switchoverParams``). :func:`schedule_window` turns an
ISO 8601 window into the vendor epoch milliseconds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

__all__ = [
    "MIGRATION_TYPES",
    "group_item",
    "group_row",
    "migration_outcome",
    "migration_row",
    "nested",
    "object_field",
    "schedule_errors",
    "schedule_window",
]

#: MEHO migration type -> (vendor ``transferType``, vendor ``switchoverType``).
#: The switchover type is also the migration's ``migrationType``.
MIGRATION_TYPES: Final[dict[str, tuple[str, str]]] = {
    "bulk": ("vSphereReplication", "vSphereReplication"),
    "vmotion": ("NO_OP", "vMotion"),
    "rav": ("vSphereReplication", "RAV"),
    "cold": ("NO_OP", "ColdMigration"),
}

#: Vendor states of a migration that ended, when the row has no progress flags.
_ENDED_STATES: Final[dict[str, str]] = {
    "MIGRATED": "succeeded",
    "MIGRATE_FAILED": "failed",
    "MIGRATE_CANCELED": "cancelled",
    "MIGRATE_CANCELLED": "cancelled",
}
_SITE_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("endpoint_id", "endpointId"),
    ("endpoint_name", "endpointName"),
    ("endpoint_type", "endpointType"),
    ("resource_id", "resourceId"),
    ("resource_name", "resourceName"),
    ("resource_type", "resourceType"),
    ("compute_resource_id", "computeResourceId"),
)
_NETWORK_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("source_network_id", "srcNetworkId"),
    ("source_network_name", "srcNetworkName"),
    ("source_network_type", "srcNetworkType"),
    ("destination_network_id", "destNetworkId"),
    ("destination_network_name", "destNetworkName"),
    ("destination_network_type", "destNetworkType"),
)
_SWITCHOVER_OPTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("retain_mac", "retainMac"),
    ("force_power_off_vm", "forcePowerOffVm"),
    ("upgrade_hardware", "upgradeHardware"),
    ("upgrade_vmtools", "upgradeVMTools"),
    ("remove_snapshots", "removeSnapshots"),
    ("remove_isos", "removeISOs"),
)
#: Site type HCX uses for a vCenter instance; used when the caller names none.
_DEFAULT_SITE_TYPE: Final = "VC"


def object_field(row: dict[str, Any], key: str) -> dict[str, Any]:
    """``row[key]`` when it is an object, else an empty dict."""
    value = row.get(key)
    return value if isinstance(value, dict) else {}


def _mapped(source: dict[str, Any], fields: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    return {vendor: source[name] for name, vendor in fields if source.get(name) is not None}


def _site(site: dict[str, Any]) -> dict[str, Any]:
    vendor = _mapped(site, _SITE_FIELDS)
    vendor.setdefault("endpointType", _DEFAULT_SITE_TYPE)
    vendor.setdefault("resourceType", _DEFAULT_SITE_TYPE)
    return vendor


def group_item(group: dict[str, Any]) -> dict[str, Any]:
    """Build one vendor mobility group (``items[0]`` of the create, change or validate body)."""
    transfer_type, switchover_type = MIGRATION_TYPES[group["migration_type"]]
    defaults: dict[str, Any] = {
        "migrationType": switchover_type,
        "source": _site(group["source"]),
        "destination": _site(group["destination"]),
        "transferParams": {"transferType": transfer_type},
        "switchoverParams": {
            "switchoverType": switchover_type,
            "options": _mapped(group.get("switchover_options") or {}, _SWITCHOVER_OPTIONS),
        },
        "placement": [
            {key: value for key, value in place.items() if value is not None}
            for place in group.get("placement") or []
        ],
        "networkParams": {
            "defaultMappings": [
                _mapped(mapping, _NETWORK_FIELDS) for mapping in group.get("network_mappings") or []
            ]
        },
    }
    if group.get("service_mesh_id"):
        defaults["servicemeshId"] = group["service_mesh_id"]
    storage = group.get("storage")
    if storage:
        default_storage: dict[str, Any] = {"id": storage["datastore_id"], "type": "datastore"}
        if storage.get("datastore_name"):
            default_storage["name"] = storage["datastore_name"]
        if storage.get("disk_provision_type"):
            default_storage["diskProvisionType"] = storage["disk_provision_type"]
        defaults["storage"] = {"defaultStorage": default_storage}
    migrations = [
        {
            "migrationType": switchover_type,
            "entity": {
                key: value
                for key, value in (
                    ("entityId", vm["vm_id"]),
                    ("entityType", "VirtualMachine"),
                    ("entityName", vm.get("vm_name")),
                )
                if value is not None
            },
        }
        for vm in group["vms"]
    ]
    return {"name": group["name"], "groupDefaults": defaults, "migrations": migrations}


def group_row(item: dict[str, Any]) -> dict[str, Any]:
    """One flat mobility group row from the vendor ``{info, summary}`` object."""
    info = object_field(item, "info")
    summary = object_field(item, "summary")
    return {
        "mobility_group_id": info.get("id"),
        "name": info.get("name"),
        "state": summary.get("state"),
        "configuration_status": summary.get("configurationStatus"),
        "is_archived": summary.get("isArchived"),
        "migration_wave_id": info.get("migrationWaveId"),
        "migration_wave_name": info.get("migrationWaveName"),
        "pairing_id": info.get("pairingId"),
        "service_mesh_id": info.get("servicemeshId"),
        "created": info.get("createdTimestamp"),
        "last_updated": info.get("lastUpdatedTimestamp"),
        "migration_started": info.get("migrationStartTimestamp"),
        "username": info.get("username"),
    }


def _epoch_ms(kind: str, text: str) -> int:
    """An ISO 8601 time with a time zone -> epoch milliseconds, or ``ValueError``."""
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"the {kind} {text!r} is not an ISO 8601 time") from exc
    if moment.tzinfo is None:
        raise ValueError(f"the {kind} {text!r} must name a time zone, for example 'Z'")
    return int(moment.timestamp() * 1000)


def schedule_window(kind: str, window: dict[str, Any]) -> dict[str, Any]:
    """The vendor window: ``{"clearSchedule": true}``, or start and expiry in epoch ms."""
    if window.get("clear"):
        return {"clearSchedule": True}
    start = _epoch_ms(f"{kind} start_time", window["start_time"])
    end = _epoch_ms(f"{kind} end_time", window["end_time"])
    if end <= start:
        raise ValueError(f"the {kind} end_time must be after its start_time")
    return {"startTime": start, "expiryTime": end}


def schedule_errors(errors: Any) -> list[dict[str, Any]]:
    """Flatten the vendor schedule ``errors`` object into ``[{scope, id, code, message}]``."""
    if not isinstance(errors, dict):
        return []
    holders: list[tuple[str, Any]] = [("group", errors.get("mobilityGroupDefaults"))]
    holders += [("migration", entry) for entry in errors.get("migrations") or []]
    flat: list[dict[str, Any]] = []
    for scope, holder in holders:
        if not isinstance(holder, dict):
            continue
        for error in holder.get("errors") or []:
            if isinstance(error, dict):
                flat.append(
                    {
                        "scope": scope,
                        "id": holder.get("id"),
                        "code": error.get("code"),
                        "message": error.get("message"),
                    }
                )
    return flat


def migration_outcome(row: dict[str, Any]) -> str:
    """The outcome of one migration row: running, succeeded, failed, cancelled or unknown."""
    progress = object_field(row, "progress")
    if progress.get("isCancelled") is True:
        return "cancelled"
    if progress.get("didFail") is True:
        return "failed"
    if progress.get("isDone") is True:
        return "succeeded"
    if progress.get("isDone") is False:
        return "running"
    state = row.get("state")
    return _ENDED_STATES.get(state.upper(), "unknown") if isinstance(state, str) else "unknown"


def nested(value: Any, *keys: str) -> Any:
    """``value[k1][k2]...``, or ``None`` when a step is missing or not an object."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def migration_row(row: dict[str, Any]) -> dict[str, Any]:
    """Add flat id, VM, outcome, message and time fields to a vendor migration row."""
    progress = object_field(row, "progress")
    flat = dict(row)
    flat["migration_id"] = row.get("migrationId") or row.get("id")
    flat["mobility_group_id"] = row.get("migrationGroupId")
    flat["vm_id"] = nested(row, "entity", "entityId") or row.get("entityId")
    flat["vm_name"] = nested(row, "entity", "entityName")
    flat["outcome"] = migration_outcome(row)
    flat["status_message"] = nested(progress, "state", "message") or progress.get("message")
    flat["error_message"] = nested(progress, "state", "errorMessage") or nested(
        progress, "error", "message"
    )
    flat["start_time"] = progress.get("startTime")
    flat["end_time"] = progress.get("endTime")
    return flat
