# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Turn FortiOS answers into the flat rows the device reads return.

Pure functions: they get the cleaned ``results`` of an answer and return
plain dicts. The connector makes the calls. Field names come from public
FortiOS answers (Fortinet community articles, Fortinet's Ansible collection
and open monitoring tools); unknown or missing fields become ``None`` rather
than an error, because field sets differ a little between releases.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

__all__ = [
    "FORTIGATE_MODEL_NAME",
    "checksums",
    "device_identity",
    "fortimanager_state",
    "ha_in_sync",
    "ha_members",
    "ha_settings",
    "is_fortigate",
    "licence_rows",
    "platform_of",
    "resource_usage",
    "status_fields",
    "vdom_rows",
]

#: ``results.model_name`` of every FortiGate, hardware or VM.
FORTIGATE_MODEL_NAME: Final[str] = "FortiGate"

#: The resource counters ``monitor/system/resource/usage`` reports, in the
#: order the read lists them.
_USAGE_KEYS: Final[tuple[str, ...]] = (
    "cpu",
    "mem",
    "disk",
    "session",
    "session6",
    "setuprate",
    "setuprate6",
    "disk_lograte",
    "faz_lograte",
    "forticloud_lograte",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def is_fortigate(status_results: Any) -> bool:
    """Return ``True`` when ``monitor/system/status`` describes a FortiGate."""
    return _mapping(status_results).get("model_name") == FORTIGATE_MODEL_NAME


def platform_of(status_results: Any) -> str | None:
    """``vm`` for a FortiGate-VM, ``hardware`` for an appliance, ``None`` when unknown."""
    status = _mapping(status_results)
    model = _str(status.get("model")) or ""
    number = _str(status.get("model_number")) or ""
    if not model and not number:
        return None
    if model.upper().startswith("FGVM") or number.upper().startswith("VM"):
        return "vm"
    return "hardware"


def status_fields(status_results: Any, firmware_results: Any) -> dict[str, Any]:
    """Model, host name and firmware fields of ``monitor/system/status`` and ``.../firmware``."""
    status = _mapping(status_results)
    current = _mapping(_mapping(firmware_results).get("current"))
    return {
        "hostname": _str(status.get("hostname")),
        "model": _str(status.get("model")),
        "model_name": _str(status.get("model_name")),
        "model_number": _str(status.get("model_number")),
        "platform": platform_of(status),
        "log_disk_status": _str(status.get("log_disk_status")),
        "firmware_version": _str(current.get("version")),
        "firmware_build": _int(current.get("build")),
    }


def ha_members(peer_results: Any) -> list[dict[str, Any]]:
    """Rows of ``monitor/system/ha-peer``: one per HA member, primary first.

    Releases name the primary flag ``primary`` or ``master``.
    """
    members: list[dict[str, Any]] = []
    for row in _rows(peer_results):
        primary = bool(row.get("primary") or row.get("master"))
        members.append(
            {
                "serial": _str(row.get("serial_no")),
                "hostname": _str(row.get("hostname")),
                "priority": _int(row.get("priority")),
                "vcluster_id": _int(row.get("vcluster_id")),
                "role": "primary" if primary else "secondary",
            }
        )
    members.sort(key=lambda m: (m["role"] != "primary", m["serial"] or ""))
    return members


def checksums(checksum_results: Any) -> list[dict[str, Any]]:
    """Rows of ``monitor/system/ha-checksums``: each member's configuration checksums."""
    rows: list[dict[str, Any]] = []
    for row in _rows(checksum_results):
        values = _mapping(row.get("checksum"))
        rows.append(
            {
                "serial": _str(row.get("serial_no")),
                "checksums": {str(k): v for k, v in sorted(values.items()) if isinstance(v, str)},
            }
        )
    rows.sort(key=lambda r: r["serial"] or "")
    return rows


def ha_in_sync(checksum_rows: list[dict[str, Any]]) -> bool | None:
    """``True`` when all members have the same checksums; ``None`` with fewer than two."""
    if len(checksum_rows) < 2:
        return None
    first = checksum_rows[0]["checksums"]
    return bool(first) and all(row["checksums"] == first for row in checksum_rows[1:])


def ha_settings(ha_results: Any) -> dict[str, Any]:
    """HA mode and group of ``cmdb/system/ha`` (nothing else; the answer holds a password)."""
    row = next(iter(_rows(ha_results)), {})
    return {"mode": _str(row.get("mode")), "group_name": _str(row.get("group-name"))}


def device_identity(members: list[dict[str, Any]], answering_serial: str | None) -> str | None:
    """One id for all targets of one device: the HA members, or the one unit.

    An HA pair keeps its id through a failover, because the id lists every
    member serial, sorted.
    """
    serials = sorted({m["serial"] for m in members if m.get("serial")})
    if len(serials) >= 2:
        return "ha:" + ",".join(serials)
    serial = serials[0] if serials else answering_serial
    return f"serial:{serial}" if serial else None


def vdom_rows(vdom_results: Any) -> list[dict[str, Any]]:
    """Rows of ``cmdb/system/vdom``: one per VDOM, sorted by name."""
    rows = [
        {
            "name": _str(row.get("name")),
            "short_name": _str(row.get("short-name")),
            "vcluster_id": _int(row.get("vcluster-id")),
        }
        for row in _rows(vdom_results)
    ]
    rows.sort(key=lambda r: r["name"] or "")
    return rows


def fortimanager_state(results: Any) -> dict[str, Any]:
    """Central management state of ``monitor/system/central-management/status``.

    FortiManager controls the device when the mode is ``normal`` and a
    FortiManager server is set. In ``backup`` mode FortiManager only keeps
    copies of the configuration, so direct changes stay allowed.
    """
    state = _mapping(results)
    mode = _str(state.get("mode"))
    server = _str(state.get("server"))
    controls = mode == "normal" and server is not None and server != "0.0.0.0"
    return {
        "fortimanager_controls": controls,
        "mode": mode,
        "server": server,
        "status": _str(state.get("status")),
        "registration_status": _str(state.get("registration_status")),
    }


def _iso(epoch: Any) -> str | None:
    seconds = _int(epoch)
    if seconds is None or seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def licence_rows(results: Any) -> list[dict[str, Any]]:
    """Rows of ``monitor/license/status``: one per licence or contract, sorted.

    Account names, company names and addresses in the answer are left out.
    Support contracts of FortiCare become rows named ``forticare.<kind>``.
    """
    rows: list[dict[str, Any]] = []
    for service, raw in sorted(_mapping(results).items()):
        entry = _mapping(raw)
        rows.append(
            {
                "service": str(service),
                "type": _str(entry.get("type")),
                "status": _str(entry.get("status")),
                "expires": _iso(entry.get("expires")),
                "used": _int(entry.get("used")),
                "max": _int(entry.get("max")),
            }
        )
        for kind, contract in sorted(_mapping(entry.get("support")).items()):
            support = _mapping(contract)
            rows.append(
                {
                    "service": f"{service}.{kind}",
                    "type": "support",
                    "status": _str(support.get("status")),
                    "expires": _iso(support.get("expires")),
                    "used": None,
                    "max": None,
                }
            )
    return rows


def _current(value: Any) -> int | float | None:
    first = next(iter(_rows(value)), {})
    current = first.get("current")
    if isinstance(current, bool) or not isinstance(current, int | float):
        return None
    return current


def resource_usage(results: Any) -> dict[str, int | float | None]:
    """The current value of each counter of ``monitor/system/resource/usage``."""
    usage = _mapping(results)
    return {key: _current(usage.get(key)) for key in _USAGE_KEYS}
