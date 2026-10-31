# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Appliance setup reads and the network and time changes of an SRM appliance.

* ``srm.appliance.show`` -- version, registration with vCenter, network,
  time and the server certificate, from five reads of the setup API.
* ``srm.appliance.task.get`` -- one task of the setup API: the status read
  of ``srm.appliance.register`` and ``srm.appliance.certificate.set``.
* ``srm.appliance.network.set`` -- DNS and host name, and the IPv4 setup
  of one network interface.
* ``srm.appliance.time.set`` -- time zone and time sync.

All of them log in to the setup API with the appliance's admin user
(:mod:`meho_backplane.connectors.srm.setup_api`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from meho_backplane.connectors.srm.setup_api import (
    SetupApi,
    SetupApiError,
    SetupApiUnreachableError,
    Steps,
    as_dict,
    now_iso,
    parse_ref,
    task_view,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.srm.connector import SrmConnector
    from meho_backplane.connectors.vmware_shared.dr_rest import DrTargetLike

__all__ = [
    "appliance_network_set",
    "appliance_show",
    "appliance_task_get",
    "appliance_time_set",
    "setup_api_for",
]

_APPLIANCE_FIELDS = (
    "hostname",
    "product_name",
    "appliance_version",
    "appliance_build_number",
    "product_build_number",
)
_REGISTRATION_FIELDS = (
    "site_name",
    "admin_email",
    "host_name",
    "extension_key",
    "organization",
    "local_server_uuid",
)
_CERT_FIELDS = (
    "subject_dn",
    "issuer_dn",
    "serial_number",
    "not_before",
    "not_after",
    "thumbprint",
    "subject_alternative_names",
    "trusted",
)
_IPV4_FIELDS = ("assignment_mode", "address", "prefix", "default_gateway")
#: Answers of a proxy in front of a service that is down (for example during
#: an upgrade): the appliance is not reachable, the task has not failed.
_GATEWAY_DOWN = frozenset({502, 503, 504})


def setup_api_for(
    connector: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> SetupApi:
    """The setup API of *target*, logged in with ``params["appliance_secret_ref"]``."""
    return SetupApi(connector, operator, target, parse_ref(params, "appliance_secret_ref"))


def _pick(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    data = source if isinstance(source, dict) else {}
    return {key: data.get(key) for key in keys}


def _registration(config: dict[str, Any] | None) -> dict[str, Any]:
    if config is None:
        return {"state": "UNCONFIGURED", **dict.fromkeys(_REGISTRATION_FIELDS), "services": []}
    connection = as_dict(config.get("connection"))
    services = config.get("services_configuration") or []
    return {
        "state": config.get("overall_configuration_state"),
        **_pick(config, _REGISTRATION_FIELDS),
        "psc_uri": connection.get("psc_uri"),
        "psc_thumbprint": connection.get("psc_thumbprint"),
        "vc_instance_id": connection.get("vc_instance_id"),
        "services": [
            {"name": svc.get("name"), "state": svc.get("state")}
            for svc in services
            if isinstance(svc, dict)
        ],
    }


def _interface(row: Any) -> dict[str, Any]:
    data = row if isinstance(row, dict) else {}
    ipv6 = data.get("ipv6") if isinstance(data.get("ipv6"), dict) else None
    return {
        "name": data.get("name"),
        "status": data.get("status"),
        "mac": data.get("mac"),
        "ipv4": _pick(data.get("ipv4"), _IPV4_FIELDS),
        "ipv6": ipv6,
    }


def network_view(payload: Any) -> dict[str, Any]:
    """Reduce ``GET /appliance/settings/network`` to DNS and the interface rows."""
    data = payload if isinstance(payload, dict) else {}
    return {
        "dns": _pick(data.get("dns"), ("mode", "hostname", "servers")),
        "interfaces": [_interface(row) for row in data.get("interfaces") or []],
    }


def _time_view(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    sync = as_dict(data.get("synchronization"))
    return {
        "zone": data.get("zone"),
        "current_time": data.get("current_time"),
        "sync_mode": sync.get("mode"),
        "ntp_servers": sync.get("ntp_servers") or [],
    }


async def appliance_show(
    connector: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.appliance.show``: five setup API reads, reduced."""
    api = setup_api_for(connector, operator, target, params)
    info = await api.call("GET", "/appliance")
    try:
        config = await api.call("GET", "/appliance/configuration-manager/configuration")
    except SetupApiError as exc:
        if exc.status != 404:
            raise
        config = None  # the vendor answers 404 when the appliance is not registered
    network = await api.call("GET", "/appliance/settings/network")
    time = await api.call("GET", "/appliance/settings/time")
    cert = await api.call("GET", "/appliance/certificates/server")
    return {
        "appliance": _pick(info, _APPLIANCE_FIELDS),
        "registration": _registration(config if isinstance(config, dict) else None),
        "network": network_view(network),
        "time": _time_view(time),
        "certificate": _pick(cert, _CERT_FIELDS),
    }


def _task_id(params: dict[str, Any]) -> str:
    task_id = params.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip() or "/" in task_id:
        raise ValueError("task_id must be the id of a setup API task")
    return task_id.strip()


async def appliance_task_get(
    connector: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.appliance.task.get``: the vendor's state of one task.

    When the appliance does not answer (no connection, or HTTP 502, 503 or
    504), the answer is ``state: not_reachable`` with the task id and the
    time; it never says ``failed`` for that.
    """
    task_id = _task_id(params)
    api = setup_api_for(connector, operator, target, params)
    checked_at = now_iso()
    try:
        task = await api.call("GET", f"/tasks/{quote(task_id, safe='')}")
    except SetupApiError as exc:
        if not isinstance(exc, SetupApiUnreachableError) and exc.status not in _GATEWAY_DOWN:
            raise
        view = task_view({"id": task_id})
        view.update(
            state="not_reachable",
            reachable=False,
            checked_at=checked_at,
            unreachable_reason=str(exc),
        )
        return view
    view = task_view(task)
    view["task_id"] = view["task_id"] or task_id
    view.update(reachable=True, checked_at=checked_at, unreachable_reason=None)
    return view


def _interface_input(params: dict[str, Any]) -> dict[str, Any] | None:
    raw = params.get("interface")
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        raise ValueError("interface must name the interface and give its ipv4 settings")
    ipv4 = raw.get("ipv4")
    if not isinstance(ipv4, dict) or ipv4.get("assignment_mode") not in ("DHCP", "STATIC"):
        raise ValueError("interface.ipv4.assignment_mode must be DHCP or STATIC")
    if ipv4["assignment_mode"] == "STATIC" and not (ipv4.get("address") and ipv4.get("prefix")):
        raise ValueError("a STATIC interface needs interface.ipv4.address and prefix")
    return {"name": raw["name"], "ipv4": {k: ipv4[k] for k in _IPV4_FIELDS if k in ipv4}}


def _address_changes(current: dict[str, Any], wanted: dict[str, Any]) -> bool:
    if wanted["assignment_mode"] == "DHCP":
        return current.get("assignment_mode") != "DHCP"
    return bool(wanted.get("address") != current.get("address"))


def _meho_may_use(name: str, interfaces: list[dict[str, Any]], target_host: str) -> bool:
    """True unless the target address is on another interface. Unknown counts as used."""
    return not any(
        row["name"] != name and row["ipv4"].get("address") == target_host for row in interfaces
    )


def _check_interface(
    wanted: dict[str, Any], network: dict[str, Any], target: DrTargetLike, allow: bool
) -> bool:
    """Refuse a bad interface change before anything runs; say whether MEHO's address changes."""
    rows = network["interfaces"]
    current = next((row for row in rows if row["name"] == wanted["name"]), None)
    if current is None:
        names = ", ".join(str(row["name"]) for row in rows) or "none"
        raise ValueError(f"the appliance has no interface {wanted['name']!r} (it has: {names})")
    ends_session = _address_changes(current["ipv4"], wanted["ipv4"]) and _meho_may_use(
        wanted["name"], rows, str(target.host)
    )
    if ends_session and not allow:
        raise ValueError(
            f"the new address on {wanted['name']} ends MEHO's connection to the appliance; "
            "set allow_address_change to true, and change the target's host after the call"
        )
    return ends_session


async def _set_interface(api: SetupApi, wanted: dict[str, Any], *, ends_session: bool) -> str:
    path = f"/appliance/settings/network/interfaces/{quote(wanted['name'], safe='')}"
    try:
        await api.call("POST", path, json={"ipv4": wanted["ipv4"]})
    except SetupApiUnreachableError:
        if not ends_session:
            raise
        # The vendor ends the connection when the address changes.
        return "sent; the connection ended, as expected for a new address"
    return "set"


async def appliance_network_set(
    connector: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.appliance.network.set``: DNS first, then the interface.

    Returns the steps; a failed step carries the vendor's error and stops
    the steps after it. Nothing that ran is undone.
    """
    dns = params.get("dns")
    wanted = _interface_input(params)
    if dns is None and wanted is None:
        raise ValueError("give dns, interface or both")
    api = setup_api_for(connector, operator, target, params)
    network = network_view(await api.call("GET", "/appliance/settings/network"))
    ends_session = (
        False
        if wanted is None
        else _check_interface(wanted, network, target, params.get("allow_address_change") is True)
    )
    steps = Steps(("set_dns", "set_interface"))
    if dns is None:
        steps.skip("set_dns")
    if wanted is None:
        steps.skip("set_interface")
    answer: dict[str, Any] = {
        "outcome": "done",
        "steps": [],
        "session_ended": False,
        "target_must_change": None,
    }
    step = "set_dns"
    try:
        if dns is not None:
            await api.call("PUT", "/appliance/settings/network/dns", json=dns)
            steps.done(step)
        if wanted is not None:
            step = "set_interface"
            steps.done(step, note=await _set_interface(api, wanted, ends_session=ends_session))
    except SetupApiError as exc:
        steps.failed(step, str(exc))
        answer["outcome"] = "failed"
    if ends_session and _ran(steps, "set_interface"):
        await api.forget_session()
        new_address = wanted["ipv4"].get("address") if wanted else None
        answer["session_ended"] = True
        answer["target_must_change"] = {
            "new_address": new_address,
            "what_to_do": (
                "Change the host of this target to the new address (or to a name that "
                "points to it), then read srm.appliance.show to check the result."
            ),
        }
    answer["steps"] = steps.as_list()
    return answer


def _ran(steps: Steps, name: str) -> bool:
    return any(row["step"] == name and row["status"] == "done" for row in steps.as_list())


def _time_body(params: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    zone = params.get("zone")
    if zone is not None:
        body["zone"] = zone
    mode = params.get("sync_mode")
    servers = params.get("ntp_servers") or []
    if mode is not None:
        if mode == "NTP" and not servers:
            raise ValueError("sync_mode NTP needs at least one entry in ntp_servers")
        body["synchronization"] = {"mode": mode, "ntp_servers": servers if mode == "NTP" else []}
    elif servers:
        raise ValueError("ntp_servers needs sync_mode NTP")
    if not body:
        raise ValueError("give zone, sync_mode or both")
    return body


async def appliance_time_set(
    connector: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.appliance.time.set``: ``PUT /appliance/settings/time``, then read it back."""
    body = _time_body(params)
    api = setup_api_for(connector, operator, target, params)
    await api.call("PUT", "/appliance/settings/time", json=body)
    return {
        "outcome": "done",
        "time": _time_view(await api.call("GET", "/appliance/settings/time")),
    }
