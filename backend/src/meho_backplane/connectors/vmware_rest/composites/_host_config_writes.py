# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Three host changes: set NTP, set DNS and the host name, set the syslog targets.

* ``vmware.composite.host.ntp.set`` -- ``HostDateTimeSystem.UpdateDateTimeConfig``
  (the NTP server list), ``HostServiceSystem.UpdateServicePolicy`` (``ntpd``),
  then ``RestartService`` when ntpd runs or ``StartService`` when it does not.
* ``vmware.composite.host.dns.set`` -- ``HostNetworkSystem.UpdateDnsConfig``.
* ``vmware.composite.host.syslog.set`` -- ``OptionManager.UpdateOptions`` on
  ``Syslog.global.logHost``. The host applies the new value itself; the
  vSphere API has no separate syslog reload call.

All three are ``dangerous`` and wait for approval. Each one finds the host
(name or moid), reads the settings first, sends nothing when the host already
has them (``unchanged``), sends only the calls that change something, and
reads the settings back. The calls are synchronous (no vCenter task). A vim
fault (HTTP 500 with the fault object) becomes ``failed`` with the fault class
and text and an ``error`` issue, so the call is recorded as failed. Every
write goes through the governed VI-JSON seam
(:func:`._write._write_vmomi_sub_op`).

The registry rows and the approval previews are in :mod:`._host_config_ops`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._write import (
    _vsphere_fault_detail,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.host_config import (
    NTP_SERVICE_KEY,
    OP_QUERY_OPTIONS,
    OP_RETRIEVE_PROPERTIES,
    SYSLOG_LOG_HOST_KEY,
    dns_state,
    ntp_state,
    query_option,
    read_config_managers,
    read_property,
    resolve_host,
    syslog_state,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "OP_ID_DNS_SET",
    "OP_ID_NTP_SET",
    "OP_ID_SYSLOG_SET",
    "VIM_SUB_OPS_HOST_DNS_SET",
    "VIM_SUB_OPS_HOST_NTP_SET",
    "VIM_SUB_OPS_HOST_SYSLOG_SET",
    "host_dns_set_composite",
    "host_ntp_set_composite",
    "host_syslog_set_composite",
    "locate_host_managers",
]

OP_ID_NTP_SET: Final = "vmware.composite.host.ntp.set"
OP_ID_DNS_SET: Final = "vmware.composite.host.dns.set"
OP_ID_SYSLOG_SET: Final = "vmware.composite.host.syslog.set"

_OP_UPDATE_DATE_TIME_CONFIG: Final = "POST:/HostDateTimeSystem/{moId}/UpdateDateTimeConfig"
_OP_UPDATE_SERVICE_POLICY: Final = "POST:/HostServiceSystem/{moId}/UpdateServicePolicy"
_OP_START_SERVICE: Final = "POST:/HostServiceSystem/{moId}/StartService"
_OP_RESTART_SERVICE: Final = "POST:/HostServiceSystem/{moId}/RestartService"
_OP_UPDATE_DNS_CONFIG: Final = "POST:/HostNetworkSystem/{moId}/UpdateDnsConfig"
_OP_UPDATE_OPTIONS: Final = "POST:/OptionManager/{moId}/UpdateOptions"

#: Every vim call of each change (reads and writes). The reconcile test checks
#: them against the pinned vSphere API files of 8.0 and 9.0, and
#: ``_governed_subops`` lists them for the grant set.
VIM_SUB_OPS_HOST_NTP_SET: Final[tuple[str, ...]] = (
    OP_RETRIEVE_PROPERTIES,
    _OP_UPDATE_DATE_TIME_CONFIG,
    _OP_UPDATE_SERVICE_POLICY,
    _OP_START_SERVICE,
    _OP_RESTART_SERVICE,
)
VIM_SUB_OPS_HOST_DNS_SET: Final[tuple[str, ...]] = (OP_RETRIEVE_PROPERTIES, _OP_UPDATE_DNS_CONFIG)
VIM_SUB_OPS_HOST_SYSLOG_SET: Final[tuple[str, ...]] = (
    OP_RETRIEVE_PROPERTIES,
    OP_QUERY_OPTIONS,
    _OP_UPDATE_OPTIONS,
)

#: One write: (governance op id, concrete VI-JSON path, body, gate params).
_Step = tuple[str, str, dict[str, Any], dict[str, Any]]
_Reader = Callable[[], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Shared flow
# ---------------------------------------------------------------------------


async def locate_host_managers(
    connector: VmwareRestConnector,
    operator: Operator,
    target: Any,
    host: str | None,
    managers: tuple[str, ...],
) -> tuple[str | None, dict[str, str], dict[str, Any] | None]:
    """Host moid + the needed config managers, or a refusal envelope."""
    host_moid, refusal = await resolve_host(connector, operator, target, host)
    if refusal is not None:
        return None, {}, refusal
    assert host_moid is not None  # refusal is None => the host resolved (mypy narrowing).
    found = await read_config_managers(connector, operator, target, host_moid, managers)
    missing = [name for name in managers if name not in found]
    if missing:
        return (
            None,
            {},
            {
                "status": "config_manager_unreadable",
                "host": host_moid,
                "guidance": f"Could not read configManager.{missing[0]} of the host; "
                "nothing was changed.",
                "issues": [issue("connector", "error", "the host's config manager is unreadable")],
            },
        )
    return host_moid, found, None


async def _run_steps(
    connector: VmwareRestConnector,
    operator: Operator,
    target: Any,
    steps: list[_Step],
    envelope: dict[str, Any],
) -> OperationResult | dict[str, Any] | None:
    """Send *steps* in order through the governed seam.

    Returns ``None`` when all ran (their method names are in
    ``envelope["applied_steps"]``), the gate's :class:`OperationResult` when a
    step waits or is denied, or a ``failed`` envelope on a vim fault.
    """
    for op_id, path, body, params in steps:
        method = path.rsplit("/", 1)[-1]
        try:
            gate, _ = await _write_vmomi_sub_op(
                connector,
                target,
                operator,
                op_id=op_id,
                vmomi_path=path,
                body=body,
                params=params,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 500:
                raise
            detail = _vsphere_fault_detail(exc)
            done = ", ".join(envelope["applied_steps"]) or "none"
            return {
                **envelope,
                "status": "failed",
                "fault": detail,
                "guidance": f"vSphere refused {method}. Calls that ran before it: {done}.",
                "issues": [issue("connector", "error", f"{method} failed: {detail}")],
            }
        if gate is not None:
            return gate
        envelope["applied_steps"].append(method)
    return None


async def _finish(
    envelope: dict[str, Any],
    read: _Reader,
    matches: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    """Read the settings back and set ``applied`` or ``not_verified``."""
    try:
        after = await read()
    except (httpx.HTTPError, RuntimeError) as exc:
        return {
            **envelope,
            "status": "not_verified",
            "guidance": "The calls ran, but MEHO could not read the host again "
            f"({type(exc).__name__}). Check with vmware.host.config.show.",
            "issues": [issue("connector", "error", "the change could not be read back")],
        }
    if not matches(after):
        return {
            **envelope,
            "status": "not_verified",
            "after": after,
            "guidance": "The calls ran, but the host does not show the new settings.",
            "issues": [issue("connector", "error", "the host does not show the new settings")],
        }
    return {**envelope, "status": "applied", "after": after}


def _refused(refusal: dict[str, Any], requested: dict[str, Any]) -> dict[str, Any]:
    return {
        "requested": requested,
        "before": None,
        "after": None,
        "applied_steps": [],
        "fault": None,
        "guidance": None,
        **refusal,
    }


def _envelope(host_moid: str, requested: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": host_moid,
        "requested": requested,
        "before": before,
        "after": None,
        "applied_steps": [],
        "fault": None,
        "guidance": None,
    }


def _unchanged(envelope: dict[str, Any]) -> dict[str, Any]:
    return {
        **envelope,
        "status": "unchanged",
        "after": envelope["before"],
        "guidance": "The host already has these settings; nothing was sent.",
    }


# ---------------------------------------------------------------------------
# host.ntp.set
# ---------------------------------------------------------------------------


def _ntp_reader(
    connector: VmwareRestConnector, operator: Operator, target: Any, managers: dict[str, str]
) -> _Reader:
    async def read() -> dict[str, Any]:
        info = await read_property(
            connector,
            operator,
            target,
            "HostDateTimeSystem",
            managers["dateTimeSystem"],
            "dateTimeInfo",
        )
        services = await read_property(
            connector,
            operator,
            target,
            "HostServiceSystem",
            managers["serviceSystem"],
            "serviceInfo",
        )
        return ntp_state(info, services)

    return read


def _ntp_steps(
    host_moid: str,
    managers: dict[str, str],
    before: dict[str, Any],
    servers: list[str],
    policy: str,
) -> list[_Step]:
    dts, svc = managers["dateTimeSystem"], managers["serviceSystem"]
    steps: list[_Step] = []
    if before["servers"] != servers:
        steps.append(
            (
                _OP_UPDATE_DATE_TIME_CONFIG,
                f"/HostDateTimeSystem/{dts}/UpdateDateTimeConfig",
                {
                    "config": {
                        "_typeName": "HostDateTimeConfig",
                        "ntpConfig": {"_typeName": "HostNtpConfig", "server": servers},
                    }
                },
                {"host": host_moid, "ntp_servers": servers},
            )
        )
    if before["service_policy"] != policy:
        steps.append(
            (
                _OP_UPDATE_SERVICE_POLICY,
                f"/HostServiceSystem/{svc}/UpdateServicePolicy",
                {"id": NTP_SERVICE_KEY, "policy": policy},
                {"host": host_moid, "service": NTP_SERVICE_KEY, "policy": policy},
            )
        )
    run_op, run_path = (
        (_OP_RESTART_SERVICE, f"/HostServiceSystem/{svc}/RestartService")
        if before["service_running"]
        else (_OP_START_SERVICE, f"/HostServiceSystem/{svc}/StartService")
    )
    steps.append(
        (
            run_op,
            run_path,
            {"id": NTP_SERVICE_KEY},
            {"host": host_moid, "service": NTP_SERVICE_KEY},
        )
    )
    return steps


async def host_ntp_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Set a host's NTP servers and ntpd policy, then (re)start ntpd."""
    servers = list(params["servers"])
    policy = params.get("policy", "on")
    requested = {"servers": servers, "policy": policy}
    host_moid, managers, refusal = await locate_host_managers(
        connector, operator, target, params.get("host"), ("dateTimeSystem", "serviceSystem")
    )
    if refusal is not None:
        return _refused(refusal, requested)
    assert host_moid is not None
    read = _ntp_reader(connector, operator, target, managers)
    before = await read()
    envelope = _envelope(host_moid, requested, before)
    if (
        before["servers"] == servers
        and before["service_policy"] == policy
        and before["service_running"] is True
    ):
        return _unchanged(envelope)
    steps = _ntp_steps(host_moid, managers, before, servers, policy)
    stopped = await _run_steps(connector, operator, target, steps, envelope)
    if stopped is not None:
        return stopped
    return await _finish(
        envelope,
        read,
        lambda after: (
            after["servers"] == servers
            and after["service_policy"] == policy
            and after["service_running"] is not False
        ),
    )


# ---------------------------------------------------------------------------
# host.dns.set
# ---------------------------------------------------------------------------

_DNS_FIELDS: Final = ("servers", "search_domains", "host_name", "domain_name")


def _same_dns(state: dict[str, Any], wanted: dict[str, Any]) -> bool:
    """Compare the DNS fields; a missing value and an empty one are the same."""
    return all((state[key] or None) == (wanted[key] or None) for key in _DNS_FIELDS)


async def host_dns_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Set a host's DNS servers, search domains, host name and domain name."""
    requested: dict[str, Any] = {key: params[key] for key in _DNS_FIELDS if key in params}
    host_moid, managers, refusal = await locate_host_managers(
        connector, operator, target, params.get("host"), ("networkSystem",)
    )
    if refusal is not None:
        return _refused(refusal, requested)
    assert host_moid is not None
    network_system = managers["networkSystem"]

    async def read() -> dict[str, Any]:
        config = await read_property(
            connector, operator, target, "HostNetworkSystem", network_system, "dnsConfig"
        )
        return dns_state(config)

    before = await read()
    envelope = _envelope(host_moid, requested, before)
    if before["dhcp"]:
        return {
            **envelope,
            "status": "dhcp_in_use",
            "guidance": "The host gets DNS from DHCP, so these fields cannot be set. Switch "
            "the host to static DNS first. Nothing was sent.",
        }
    wanted = {key: requested.get(key, before[key]) for key in _DNS_FIELDS}
    if _same_dns(before, wanted):
        return _unchanged(envelope)
    config = {
        "_typeName": "HostDnsConfig",
        "dhcp": False,
        "hostName": wanted["host_name"] or "",
        "domainName": wanted["domain_name"] or "",
        "address": wanted["servers"],
        "searchDomain": wanted["search_domains"],
    }
    step: _Step = (
        _OP_UPDATE_DNS_CONFIG,
        f"/HostNetworkSystem/{network_system}/UpdateDnsConfig",
        {"config": config},
        {"host": host_moid, **wanted},
    )
    stopped = await _run_steps(connector, operator, target, [step], envelope)
    if stopped is not None:
        return stopped
    return await _finish(envelope, read, lambda after: _same_dns(after, wanted))


# ---------------------------------------------------------------------------
# host.syslog.set
# ---------------------------------------------------------------------------


async def host_syslog_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Set ``Syslog.global.logHost`` on one host."""
    targets = list(params["targets"])
    log_host = ",".join(targets)
    requested = {"targets": targets}
    host_moid, managers, refusal = await locate_host_managers(
        connector, operator, target, params.get("host"), ("advancedOption",)
    )
    if refusal is not None:
        return _refused(refusal, requested)
    assert host_moid is not None
    option_manager = managers["advancedOption"]

    async def read() -> dict[str, Any]:
        value = await query_option(connector, operator, target, option_manager, SYSLOG_LOG_HOST_KEY)
        return syslog_state(value)

    before = await read()
    envelope = _envelope(host_moid, requested, before)
    if before["targets"] == targets:
        return _unchanged(envelope)
    body = {
        "changedValue": [
            {
                "_typeName": "OptionValue",
                "key": SYSLOG_LOG_HOST_KEY,
                "value": {"_typeName": "string", "_value": log_host},
            }
        ]
    }
    step: _Step = (
        _OP_UPDATE_OPTIONS,
        f"/OptionManager/{option_manager}/UpdateOptions",
        body,
        {"host": host_moid, "key": SYSLOG_LOG_HOST_KEY, "value": log_host},
    )
    stopped = await _run_steps(connector, operator, target, [step], envelope)
    if stopped is not None:
        return stopped
    return await _finish(envelope, read, lambda after: after["targets"] == targets)
