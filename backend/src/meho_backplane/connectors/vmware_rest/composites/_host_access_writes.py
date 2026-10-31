# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Four host changes: lockdown mode, its exception users, SNMP and the core dump partition.

* ``vmware.composite.host.lockdown.set`` -- ``HostAccessManager.ChangeLockdownMode``.
* ``vmware.composite.host.lockdown.exceptions.set`` --
  ``HostAccessManager.UpdateLockdownExceptions`` (read with
  ``QueryLockdownExceptions``).
* ``vmware.composite.host.snmp.set`` -- ``HostSnmpSystem.ReconfigureSnmpAgent``.
  MEHO reads the current agent settings and changes only the fields the call
  names, so a field left out keeps its value. The community string comes from
  Vault (``community_secret_ref``) and goes only into the vCenter request
  body. The op is pinned ``credential_write`` (``broadcast/events.py``), so
  the flight recorder keeps no body and the broadcast keeps no params. The
  answer counts communities and never shows one.
* ``vmware.composite.host.coredump.set`` --
  ``HostDiagnosticSystem.SelectActivePartition``, only with a partition that
  ``QueryAvailablePartition`` lists as free.

All four are ``dangerous`` and wait for approval. The shared flow is in
:mod:`._host_security_flow`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._host_security_flow import (
    Step,
    envelope,
    finish,
    refused,
    run_steps,
    stop,
    unchanged,
)
from meho_backplane.connectors.vmware_rest.host_security import (
    OP_QUERY_AVAILABLE_PARTITION,
    OP_QUERY_LOCKDOWN_EXCEPTIONS,
    OP_RETRIEVE_PROPERTIES,
    call_query,
    locate,
    partition_row,
    read_property,
    snmp_state,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import (
    SecretValueError,
    read_secret_param,
)
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "LOCKDOWN_MODES",
    "OP_ID_COREDUMP_SET",
    "OP_ID_LOCKDOWN_EXCEPTIONS_SET",
    "OP_ID_LOCKDOWN_SET",
    "OP_ID_SNMP_SET",
    "VIM_SUB_OPS_COREDUMP_SET",
    "VIM_SUB_OPS_LOCKDOWN_EXCEPTIONS_SET",
    "VIM_SUB_OPS_LOCKDOWN_SET",
    "VIM_SUB_OPS_SNMP_SET",
    "host_coredump_set_composite",
    "host_lockdown_exceptions_set_composite",
    "host_lockdown_set_composite",
    "host_snmp_set_composite",
    "read_coredump",
    "read_exceptions",
    "read_lockdown",
    "read_snmp",
    "snmp_plan",
]

OP_ID_LOCKDOWN_SET: Final = "vmware.composite.host.lockdown.set"
OP_ID_LOCKDOWN_EXCEPTIONS_SET: Final = "vmware.composite.host.lockdown.exceptions.set"
OP_ID_SNMP_SET: Final = "vmware.composite.host.snmp.set"
OP_ID_COREDUMP_SET: Final = "vmware.composite.host.coredump.set"

_OP_CHANGE_LOCKDOWN_MODE: Final = "POST:/HostAccessManager/{moId}/ChangeLockdownMode"
_OP_UPDATE_LOCKDOWN_EXCEPTIONS: Final = "POST:/HostAccessManager/{moId}/UpdateLockdownExceptions"
_OP_RECONFIGURE_SNMP_AGENT: Final = "POST:/HostSnmpSystem/{moId}/ReconfigureSnmpAgent"
_OP_SELECT_ACTIVE_PARTITION: Final = "POST:/HostDiagnosticSystem/{moId}/SelectActivePartition"

VIM_SUB_OPS_LOCKDOWN_SET: Final[tuple[str, ...]] = (
    OP_RETRIEVE_PROPERTIES,
    _OP_CHANGE_LOCKDOWN_MODE,
)
VIM_SUB_OPS_LOCKDOWN_EXCEPTIONS_SET: Final[tuple[str, ...]] = (
    OP_RETRIEVE_PROPERTIES,
    OP_QUERY_LOCKDOWN_EXCEPTIONS,
    _OP_UPDATE_LOCKDOWN_EXCEPTIONS,
)
VIM_SUB_OPS_SNMP_SET: Final[tuple[str, ...]] = (
    OP_RETRIEVE_PROPERTIES,
    _OP_RECONFIGURE_SNMP_AGENT,
)
VIM_SUB_OPS_COREDUMP_SET: Final[tuple[str, ...]] = (
    OP_RETRIEVE_PROPERTIES,
    OP_QUERY_AVAILABLE_PARTITION,
    _OP_SELECT_ACTIVE_PARTITION,
)

#: Operator-facing lockdown mode -> the ``HostLockdownMode`` value.
LOCKDOWN_MODES: Final[dict[str, str]] = {
    "disabled": "lockdownDisabled",
    "normal": "lockdownNormal",
    "strict": "lockdownStrict",
}
_MODE_NAMES: Final = {value: key for key, value in LOCKDOWN_MODES.items()}
_DEFAULT_COMMUNITY_KEY: Final = "community"


# ---------------------------------------------------------------------------
# Readers (shared with the approval previews)
# ---------------------------------------------------------------------------


async def read_lockdown(
    connector: VmwareRestConnector, operator: Operator, target: Any, manager: str
) -> dict[str, Any]:
    """``{mode}``: disabled, normal or strict (or vSphere's raw value)."""
    raw = unwrap_vim_value(
        await read_property(
            connector, operator, target, "HostAccessManager", manager, "lockdownMode"
        )
    )
    return {"mode": _MODE_NAMES.get(raw, raw) if isinstance(raw, str) else None}


async def read_exceptions(
    connector: VmwareRestConnector, operator: Operator, target: Any, manager: str
) -> dict[str, Any]:
    """``{users}``: the lockdown exception users, sorted."""
    raw = await call_query(
        connector, operator, target, f"/HostAccessManager/{manager}/QueryLockdownExceptions"
    )
    users = raw if isinstance(raw, list) else []
    return {"users": sorted(u for u in users if isinstance(u, str))}


async def _raw_snmp(
    connector: VmwareRestConnector, operator: Operator, target: Any, manager: str
) -> dict[str, Any]:
    raw = unwrap_vim_value(
        await read_property(connector, operator, target, "HostSnmpSystem", manager, "configuration")
    )
    return raw if isinstance(raw, dict) else {}


async def read_snmp(
    connector: VmwareRestConnector, operator: Operator, target: Any, manager: str
) -> dict[str, Any]:
    """The SNMP agent settings; communities counted only."""
    return snmp_state(await _raw_snmp(connector, operator, target, manager))


async def read_coredump(
    connector: VmwareRestConnector, operator: Operator, target: Any, manager: str
) -> dict[str, Any]:
    """``{active, available}``: the active partition and the free ones."""
    active = await read_property(
        connector, operator, target, "HostDiagnosticSystem", manager, "activePartition"
    )
    free = await call_query(
        connector, operator, target, f"/HostDiagnosticSystem/{manager}/QueryAvailablePartition"
    )
    rows = [partition_row(p) for p in (free if isinstance(free, list) else [])]
    return {"active": partition_row(active), "available": [r for r in rows if r is not None]}


# ---------------------------------------------------------------------------
# host.lockdown.set and host.lockdown.exceptions.set
# ---------------------------------------------------------------------------


async def host_lockdown_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Set the lockdown mode of one host."""
    mode = params["mode"]
    requested = {"mode": mode}
    host_moid, manager, refusal = await locate(
        connector, operator, target, params.get("host"), "hostAccessManager"
    )
    if refusal is not None:
        return refused(refusal, requested)
    assert host_moid is not None and manager is not None

    async def read() -> dict[str, Any]:
        return await read_lockdown(connector, operator, target, manager)

    before = await read()
    base = envelope(host_moid, requested, before)
    if before["mode"] == mode:
        return unchanged(base)
    step: Step = (
        _OP_CHANGE_LOCKDOWN_MODE,
        f"/HostAccessManager/{manager}/ChangeLockdownMode",
        {"mode": LOCKDOWN_MODES[mode]},
        {"host": host_moid, "mode": mode},
    )
    stopped = await run_steps(connector, operator, target, [step], base)
    if stopped is not None:
        return stopped
    return await finish(base, read, lambda after: after["mode"] == mode)


async def host_lockdown_exceptions_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Set the users who keep access to one host in lockdown mode."""
    users = sorted(set(params["users"]))
    requested = {"users": users}
    host_moid, manager, refusal = await locate(
        connector, operator, target, params.get("host"), "hostAccessManager"
    )
    if refusal is not None:
        return refused(refusal, requested)
    assert host_moid is not None and manager is not None

    async def read() -> dict[str, Any]:
        return await read_exceptions(connector, operator, target, manager)

    before = await read()
    base = envelope(host_moid, requested, before)
    if before["users"] == users:
        return unchanged(base)
    step: Step = (
        _OP_UPDATE_LOCKDOWN_EXCEPTIONS,
        f"/HostAccessManager/{manager}/UpdateLockdownExceptions",
        {"users": users},
        {"host": host_moid, "users": users},
    )
    stopped = await run_steps(connector, operator, target, [step], base)
    if stopped is not None:
        return stopped
    return await finish(base, read, lambda after: after["users"] == users)


# ---------------------------------------------------------------------------
# host.snmp.set
# ---------------------------------------------------------------------------


def snmp_plan(params: dict[str, Any]) -> dict[str, Any]:
    """What the SNMP change asks for, without any community string."""
    plan: dict[str, Any] = {"enabled": params["enabled"]}
    for key in ("port", "trap_targets", "community_secret_ref"):
        if key in params:
            plan[key] = params[key]
    return plan


def _snmp_spec(
    current: dict[str, Any], params: dict[str, Any], community: str | None
) -> dict[str, Any]:
    """The full ``HostSnmpConfigSpec``: the current settings with the asked fields changed."""
    communities = current.get("readOnlyCommunities")
    current_communities = (
        [c for c in communities if isinstance(c, str) and c]
        if isinstance(communities, list)
        else []
    )
    trap_community = community or (current_communities[0] if current_communities else "")
    raw_traps = current.get("trapTargets")
    traps = [
        {
            "hostName": t.get("hostName"),
            "port": t.get("port"),
            "community": community or t.get("community"),
        }
        for t in (raw_traps if isinstance(raw_traps, list) else [])
        if isinstance(t, dict) and t.get("hostName")
    ]
    if "trap_targets" in params:
        traps = [
            {"hostName": t["host_name"], "port": t.get("port", 162), "community": trap_community}
            for t in params["trap_targets"]
        ]
    spec: dict[str, Any] = {
        "_typeName": "HostSnmpConfigSpec",
        "enabled": params["enabled"],
        "port": params.get("port", current.get("port") or 161),
        "readOnlyCommunities": [community] if community else current_communities,
        "trapTargets": [{"_typeName": "HostSnmpDestination", **t} for t in traps],
    }
    options = current.get("option")
    if isinstance(options, list) and options:
        spec["option"] = [
            {"_typeName": "KeyValue", "key": o.get("key"), "value": o.get("value")}
            for o in options
            if isinstance(o, dict)
        ]
    return spec


def _spec_communities(spec: dict[str, Any]) -> tuple[str, ...]:
    """Every community string the request body holds."""
    found = [*spec["readOnlyCommunities"], *(t.get("community") for t in spec["trapTargets"])]
    return tuple(dict.fromkeys(c for c in found if isinstance(c, str) and c))


def _same_communities(current: dict[str, Any], spec: dict[str, Any]) -> bool:
    raw_traps = current.get("trapTargets")
    current_traps = [
        t.get("community")
        for t in (raw_traps if isinstance(raw_traps, list) else [])
        if isinstance(t, dict) and t.get("hostName")
    ]
    return bool(
        current.get("readOnlyCommunities") == spec["readOnlyCommunities"]
        and current_traps == [t["community"] for t in spec["trapTargets"]]
    )


def _snmp_matches(state: dict[str, Any], spec: dict[str, Any]) -> bool:
    traps = [{"host_name": t["hostName"], "port": t["port"]} for t in spec["trapTargets"]]
    return bool(
        state["enabled"] == spec["enabled"]
        and state["port"] == spec["port"]
        and state["trap_targets"] == traps
        and state["read_only_community_count"] == len(spec["readOnlyCommunities"])
    )


async def host_snmp_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Set the SNMP agent of one host; the community comes from Vault."""
    requested = snmp_plan(params)
    host_moid, manager, refusal = await locate(
        connector, operator, target, params.get("host"), "snmpSystem"
    )
    if refusal is not None:
        return refused(refusal, requested)
    assert host_moid is not None and manager is not None
    current = await _raw_snmp(connector, operator, target, manager)
    base = envelope(host_moid, requested, snmp_state(current))
    community: str | None = None
    if params.get("community_secret_ref"):
        key_params = {**params}
        key_params.setdefault("community_secret_key", _DEFAULT_COMMUNITY_KEY)
        try:
            community = await read_secret_param(operator, target, key_params, prefix="community")
        except SecretValueError as exc:
            return stop(
                base, "community_secret_unusable", str(exc).removesuffix(" Nothing was sent.")
            )
    spec = _snmp_spec(current, params, community)
    if "trap_targets" in params and not spec["readOnlyCommunities"]:
        return stop(
            base, "community_required", "Trap targets need a community: set community_secret_ref."
        )
    same_community = community is None or _same_communities(current, spec)
    if same_community and _snmp_matches(base["before"], spec):
        return unchanged(base)
    step: Step = (
        _OP_RECONFIGURE_SNMP_AGENT,
        f"/HostSnmpSystem/{manager}/ReconfigureSnmpAgent",
        {"spec": spec},
        {"host": host_moid, **requested},
    )
    stopped = await run_steps(connector, operator, target, [step], base, _spec_communities(spec))
    if stopped is not None:
        return stopped

    async def read() -> dict[str, Any]:
        return await read_snmp(connector, operator, target, manager)

    return await finish(base, read, lambda after: _snmp_matches(after, spec))


# ---------------------------------------------------------------------------
# host.coredump.set
# ---------------------------------------------------------------------------


def _same_partition(row: dict[str, Any] | None, disk: str, number: int) -> bool:
    return row is not None and bool(row["disk_name"] == disk and row["partition"] == number)


async def host_coredump_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Make one free diagnostic partition the core dump partition of a host."""
    disk, number = params["disk_name"], params["partition"]
    requested = {"disk_name": disk, "partition": number}
    host_moid, manager, refusal = await locate(
        connector, operator, target, params.get("host"), "diagnosticSystem"
    )
    if refusal is not None:
        return refused(refusal, requested)
    assert host_moid is not None and manager is not None

    async def read() -> dict[str, Any]:
        return await read_coredump(connector, operator, target, manager)

    before = await read()
    base = envelope(host_moid, requested, before)
    if _same_partition(before["active"], disk, number):
        return unchanged(base)
    if not any(_same_partition(row, disk, number) for row in before["available"]):
        return stop(
            base,
            "partition_not_available",
            "This partition is not a free diagnostic partition of the host; pick one from "
            "before.available.",
        )
    step: Step = (
        _OP_SELECT_ACTIVE_PARTITION,
        f"/HostDiagnosticSystem/{manager}/SelectActivePartition",
        {
            "partition": {
                "_typeName": "HostScsiDiskPartition",
                "diskName": disk,
                "partition": number,
            }
        },
        {"host": host_moid, "disk_name": disk, "partition": number},
    )
    stopped = await run_steps(connector, operator, target, [step], base)
    if stopped is not None:
        return stopped
    return await finish(base, read, lambda after: _same_partition(after["active"], disk, number))
