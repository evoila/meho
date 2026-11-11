# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: declarative op module (planner + PHP fragment + op metadata /
# JSON schemas / llm_instructions) in the single-module-per-op-group shape of its
# pfSense siblings; the functions are small and low-complexity.

"""pfSense OpenVPN onboarding ops: the next free tunnel address and the client overrides (#3993).

Two ops that help an operator give a person VPN access on a pfSense
firewall (the user itself is made by
:mod:`~meho_backplane.connectors.pfsense.ops_openvpn_user`):

* ``pfsense.openvpn.address.next_free`` (read) -- the lowest address offset
  that is free on EVERY OpenVPN server that logs users in with a user name
  and password. It counts the client-specific overrides in ``config.xml``
  AND the rendered files under ``/var/etc/openvpn/server*/csc/``, because
  the two do not always agree.
* ``pfsense.openvpn.csc.sync`` (change) -- creates or extends client-specific
  overrides (one per user and server: the fixed tunnel address and the
  routes pushed to the client). It has a compare-only mode.

How the sync works
------------------

1. Every wanted entry is checked first (server exists and logs users in,
   the address is inside the server's network and free, the existing entry
   has no other address). One refused entry stops the whole call: nothing
   is written, and outside compare-only mode the call fails with
   :class:`~meho_backplane.connectors.pfsense.refusal.PfSenseRefusedError`
   (code ``refused``) that names each refused entry.
2. In compare-only mode the call returns the plan and stops: which entries
   would be created, updated, or are already right.
3. Otherwise the handler saves a copy of ``config.xml`` (the rollback copy,
   mode 0600), plays back one fragment that edits only the named entries
   and calls ``write_config()``, then reads ``config.xml`` again. It
   reports success only when every named entry has the wanted values and
   nothing else in the file changed.

The plan is made from one read of ``config.xml``; the fragment runs
later. So the fragment first checks that the overrides are still as the
plan saw them: the same number of overrides, no override for a new
entry, and the same address and routes on each entry it updates. When
one differs (for example a route was added in the web page meanwhile),
it writes nothing and prints ``MEHO_CSC_CHANGED``; the handler then
fails and asks for a new run.

The sync never deletes an entry, never removes a route (a route list is
only added to), never changes the address of an existing entry (that is
reported as ``address_conflict``), and does not restart OpenVPN. The
fragment calls ``openvpn_resync_csc()`` for each edited entry, the same
call the pfSense web page makes: it writes that one user's file for the
running servers. The override lives in the configuration, so the next
resync keeps it.

Input safety
------------

User names pass the strict user-name allowlist. Addresses are parsed and
re-emitted through :mod:`ipaddress`. Every value reaches the PHP fragment
only through :func:`~meho_backplane.connectors.pfsense.ops_write._php_squote`
inside a quoted heredoc. No fragment line starts with ``!`` or ``=``.

Secrets
-------

The ops read and write no secret field. The result carries names,
addresses and routes only.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from defusedxml.ElementTree import ParseError, fromstring

from meho_backplane.connectors.pfsense.openvpn_config import (
    CscEntry,
    OpenVpnServer,
    parse_csc_entries,
    parse_openvpn_servers,
    read_rendered_addresses,
    used_addresses,
)
from meho_backplane.connectors.pfsense.ops import PfSenseOp
from meho_backplane.connectors.pfsense.ops_user_write import USER_NAME_RE, _validate_user_name
from meho_backplane.connectors.pfsense.ops_write import (
    _ALWAYS_REWRITTEN_SECTIONS,
    _apply_playback,
    _canonical,
    _is_empty_container,
    _php_squote,
    _read_config_xml,
    _validate_descr,
    serialized_config_write,
)
from meho_backplane.connectors.pfsense.refusal import PfSenseRefusedError
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "OPENVPN_OPS",
    "build_csc_sync_playback",
    "pfsense_openvpn_address_next_free",
    "pfsense_openvpn_csc_sync",
    "plan_csc_sync",
    "save_rollback_copy",
]

_VPNID_RE: Final = re.compile(r"^[0-9]{1,5}$")
_CHANGED_MARKER: Final = "MEHO_CSC_CHANGED="
_MAX_ENTRIES: Final = 100
_MAX_ROUTES: Final = 50

#: Offsets 0 (the network) and 1 (the server) are never given to a client.
_FIRST_CLIENT_OFFSET: Final = 2


def _user_auth_servers(xml_text: str) -> list[OpenVpnServer]:
    """Return the OpenVPN servers that log users in, or raise when one has no IPv4 network."""
    servers = [s for s in parse_openvpn_servers(xml_text) if s.user_auth]
    for server in servers:
        if not server.vpnid:
            raise RuntimeError("an OpenVPN user-auth server in config.xml has no vpnid")
        if server.network is None:
            raise RuntimeError(
                f"OpenVPN server {server.vpnid} has no IPv4 tunnel network in config.xml; "
                "cannot choose a client address for it"
            )
    return servers


# ---------------------------------------------------------------------------
# pfsense.openvpn.address.next_free
# ---------------------------------------------------------------------------


def _client_address(server: OpenVpnServer, offset: int) -> ipaddress.IPv4Address:
    assert server.network is not None
    return server.network.network_address + offset


def _max_offset(server: OpenVpnServer) -> int:
    assert server.network is not None
    return min(server.network.num_addresses - 2, 65534)


async def pfsense_openvpn_address_next_free(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.openvpn.address.next_free`` (lowest offset free everywhere)."""
    del params  # the op takes none
    xml_text = await _read_config_xml(self, target, operator)
    servers = _user_auth_servers(xml_text)
    entries = parse_csc_entries(xml_text)
    result: dict[str, Any] = {
        "op_class": "read",
        "resource": "openvpn_address",
        "status": "no_user_auth_server",
        "address_offset": None,
        "servers": [],
        "sources": {"config_entries": len(entries), "rendered_files": 0},
        "warnings": [],
    }
    if not servers:
        return result
    rendered = await read_rendered_addresses(self, target, operator)
    result["sources"]["rendered_files"] = len(rendered)
    result["warnings"] = [
        f"the override for {e.common_name!r} has a tunnel network that is not an IPv4 "
        "address (an alias?); it is not counted"
        for e in entries
        if e.tunnel_raw and e.tunnel_interface is None
    ]
    used = {s.vpnid: used_addresses(s, entries, rendered) for s in servers}
    limit = min(_max_offset(s) for s in servers)
    for offset in range(_FIRST_CLIENT_OFFSET, limit + 1):
        if all(_client_address(s, offset) not in used[s.vpnid] for s in servers):
            return {
                **result,
                "status": "found",
                "address_offset": offset,
                "servers": [_server_row(s, offset) for s in servers],
            }
    return {**result, "status": "exhausted"}


def _server_row(server: OpenVpnServer, offset: int) -> dict[str, Any]:
    assert server.network is not None
    address = _client_address(server, offset)
    return {
        "vpnid": server.vpnid,
        "description": server.description,
        "network": str(server.network),
        "address": str(address),
        "tunnel_network": f"{address}/{server.network.prefixlen}",
    }


# ---------------------------------------------------------------------------
# pfsense.openvpn.csc.sync -- input
# ---------------------------------------------------------------------------


def _validate_routes(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_ROUTES:
        raise ValueError(f"routes must be a list of at most {_MAX_ROUTES} IPv4 networks")
    routes: list[str] = []
    for item in value:
        try:
            net = ipaddress.IPv4Network(item, strict=False)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"routes must be IPv4 networks like '10.9.0.0/24'; got {item!r}"
            ) from exc
        if str(net) not in routes:
            routes.append(str(net))
    return routes


def _validate_entry(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("each entry must be an object")
    unknown = set(raw) - {"name", "server_vpnid", "tunnel_address", "routes", "description"}
    if unknown:
        raise ValueError(f"unknown entry field(s): {', '.join(sorted(map(str, unknown)))}")
    vpnid = raw.get("server_vpnid")
    if not isinstance(vpnid, str) or not _VPNID_RE.fullmatch(vpnid):
        raise ValueError(f"server_vpnid must be the number of an OpenVPN server; got {vpnid!r}")
    address: ipaddress.IPv4Address | None = None
    if raw.get("tunnel_address") is not None:
        try:
            address = ipaddress.IPv4Address(raw["tunnel_address"])
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"tunnel_address must be an IPv4 address; got {raw['tunnel_address']!r}"
            ) from exc
    name = _validate_user_name(raw.get("name"))
    return {
        "name": name,
        "server_vpnid": vpnid,
        "tunnel_address": address,
        "routes": _validate_routes(raw.get("routes")),
        "description": _validate_descr(raw.get("description"), f"meho: vpn user {name}"),
    }


def _validate_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > _MAX_ENTRIES:
        raise ValueError(f"entries must be a list of 1 to {_MAX_ENTRIES} entries")
    entries = [_validate_entry(raw) for raw in value]
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry["name"], entry["server_vpnid"])
        if key in seen:
            raise ValueError(
                f"entries holds {entry['name']!r} twice for server {entry['server_vpnid']}"
            )
        seen.add(key)
    return entries


# ---------------------------------------------------------------------------
# pfsense.openvpn.csc.sync -- the plan (pure)
# ---------------------------------------------------------------------------


def _canonical_route(route: str) -> str:
    try:
        return str(ipaddress.IPv4Network(route, strict=False))
    except ValueError:
        return route


def _address_refusal(
    server: OpenVpnServer,
    address: ipaddress.IPv4Address,
    name: str,
    used: dict[ipaddress.IPv4Address, set[str]],
    claimed: dict[tuple[str, ipaddress.IPv4Address], str],
) -> tuple[str, str] | None:
    """Return ``(code, detail)`` when *address* may not go to *name* on *server*."""
    assert server.network is not None
    if address not in server.network:
        return "address_outside_network", f"{address} is not inside {server.network}"
    offset = int(address) - int(server.network.network_address)
    if offset < _FIRST_CLIENT_OFFSET or offset > server.network.num_addresses - 2:
        return "address_reserved", f"{address} is the network, server or broadcast address"
    holders = used.get(address, set()) - {name}
    if holders:
        return "address_in_use", f"{address} is already used by {', '.join(sorted(holders))}"
    other = claimed.get((server.vpnid, address))
    if other is not None and other != name:
        return "address_repeated", f"{address} is also wanted for {other} in this call"
    return None


def _plan_one(
    entry: dict[str, Any],
    servers: dict[str, OpenVpnServer],
    csc: list[CscEntry],
    rendered: list[tuple[str, str, ipaddress.IPv4Address]],
    claimed: dict[tuple[str, ipaddress.IPv4Address], str],
) -> dict[str, Any]:
    name, vpnid, want = entry["name"], entry["server_vpnid"], entry["tunnel_address"]
    row: dict[str, Any] = {
        "name": name,
        "server_vpnid": vpnid,
        "action": "refused",
        "reason": None,
        "detail": None,
        "tunnel_network": None,
        "routes_added": [],
        "routes": [],
        "description": entry["description"],
    }

    def refuse(code: str, detail: str) -> dict[str, Any]:
        return {**row, "reason": code, "detail": detail}

    server = servers.get(vpnid)
    if server is None:
        return refuse("server_not_found", f"no OpenVPN server with vpnid {vpnid}")
    if not server.user_auth or server.network is None:
        return refuse("server_not_user_auth", f"server {vpnid} does not log users in by name")
    existing = [e for e in csc if e.common_name == name and e.applies_to(vpnid)]
    if len(existing) > 1:
        return refuse("ambiguous_entry", f"{len(existing)} overrides match {name!r} on {vpnid}")
    used = used_addresses(server, csc, rendered, ignore_name=name)
    prefix = server.network.prefixlen
    if not existing:
        if want is None:
            return refuse("tunnel_address_required", "a new entry needs tunnel_address")
        bad = _address_refusal(server, want, name, used, claimed)
        if bad is not None:
            return refuse(*bad)
        claimed[(vpnid, want)] = name
        return {
            **row,
            "_expect": {"tunnel": "", "routes": ""},
            "action": "create",
            "tunnel_network": f"{want}/{prefix}",
            "routes_added": list(entry["routes"]),
            "routes": list(entry["routes"]),
        }
    return _plan_existing(row, entry, server, existing[0], used, claimed, refuse)


def _plan_existing(
    row: dict[str, Any],
    entry: dict[str, Any],
    server: OpenVpnServer,
    current: CscEntry,
    used: dict[ipaddress.IPv4Address, set[str]],
    claimed: dict[tuple[str, ipaddress.IPv4Address], str],
    refuse: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any]:
    name, vpnid, want = entry["name"], entry["server_vpnid"], entry["tunnel_address"]
    prefix = server.network.prefixlen if server.network is not None else 0
    new_tunnel: str | None = None
    if want is not None:
        if current.tunnel_interface is not None and current.tunnel_interface.ip != want:
            return refuse(
                "address_conflict",
                f"{name!r} already has {current.tunnel_interface.ip} on server {vpnid}; "
                "the sync does not change an address",
            )
        if current.tunnel_interface is None and current.tunnel_raw:
            return refuse(
                "address_conflict", f"{name!r} has a tunnel network that is not an address"
            )
        if current.tunnel_interface is None:
            bad = _address_refusal(server, want, name, used, claimed)
            if bad is not None:
                return refuse(*bad)
            claimed[(vpnid, want)] = name
            new_tunnel = f"{want}/{prefix}"
    have = [_canonical_route(r) for r in current.routes]
    added = [r for r in entry["routes"] if r not in have]
    shown = {
        **row,
        "tunnel_network": new_tunnel or current.tunnel_raw or None,
        "routes_added": added,
        "routes": [*current.routes, *added],
    }
    if new_tunnel is None and not added:
        return {**shown, "action": "unchanged"}
    if current.server_list != [vpnid]:
        return refuse(
            "shared_entry",
            f"the override of {name!r} is shared by several servers or all servers; "
            "change it in the pfSense web page",
        )
    expect = {"tunnel": current.tunnel_raw, "routes": ",".join(current.routes)}
    return {**shown, "_expect": expect, "action": "update"}


def plan_csc_sync(
    entries: list[dict[str, Any]],
    servers: list[OpenVpnServer],
    csc: list[CscEntry],
    rendered: list[tuple[str, str, ipaddress.IPv4Address]],
) -> list[dict[str, Any]]:
    """Return one plan row per entry: create, update, unchanged or refused."""
    by_id = {s.vpnid: s for s in servers}
    claimed: dict[tuple[str, ipaddress.IPv4Address], str] = {}
    return [_plan_one(entry, by_id, csc, rendered, claimed) for entry in entries]


# ---------------------------------------------------------------------------
# PHP fragment
# ---------------------------------------------------------------------------


def _php_change(row: dict[str, Any]) -> str:
    create = "true" if row["action"] == "create" else "false"
    return (
        "  array("
        f"'name' => {_php_squote(row['name'])}, "
        f"'vpnid' => {_php_squote(row['server_vpnid'])}, "
        f"'tunnel' => {_php_squote(row['tunnel_network'] or '')}, "
        f"'routes' => {_php_squote(','.join(row['routes']))}, "
        f"'descr' => {_php_squote(row['description'])}, "
        f"'was_tunnel' => {_php_squote(row['_expect']['tunnel'])}, "
        f"'was_routes' => {_php_squote(row['_expect']['routes'])}, "
        f"'create' => {create}),"
    )


# The check of the fragment, before it writes: is each override still as
# the plan saw it? ``$meho_hits`` are the overrides of the name that apply
# to the server (an empty server list means all servers), the same rule as
# ``CscEntry.applies_to``. Values are compared the way the parser reads
# them: trimmed, and a route list split at commas.
_PHP_STALE_CHECK: Final = [
    "function meho_csv($meho_v) {",
    "  return implode(',', array_values(array_filter(array_map('trim',",
    "    explode(',', (string)$meho_v)), 'strlen')));",
    "}",
    "function meho_hits($meho_list, $meho_c) {",
    "  $meho_hits = array();",
    "  foreach ($meho_list as $meho_i => $meho_e) {",
    "    if (!isset($meho_e['common_name'])",
    "        || (string)$meho_e['common_name'] !== $meho_c['name']) {",
    "      continue;",
    "    }",
    "    $meho_list_ids = isset($meho_e['server_list']) ? meho_csv($meho_e['server_list']) : '';",
    "    if ($meho_list_ids === ''",
    "        || in_array($meho_c['vpnid'], explode(',', $meho_list_ids), true)) {",
    "      $meho_hits[] = $meho_i;",
    "    }",
    "  }",
    "  return $meho_hits;",
    "}",
    "$meho_stale = array();",
    "if (count($meho_csc) !== $meho_count) {",
    "  $meho_stale[] = 'count';",
    "}",
    "foreach ($meho_changes as $meho_c) {",
    "  $meho_hits = meho_hits($meho_csc, $meho_c);",
    "  $meho_ok = false;",
    "  if ($meho_c['create']) {",
    "    $meho_ok = count($meho_hits) === 0;",
    "  } elseif (count($meho_hits) === 1) {",
    "    $meho_e = $meho_csc[$meho_hits[0]];",
    "    $meho_ok = isset($meho_e['server_list'])",
    "      && meho_csv($meho_e['server_list']) === $meho_c['vpnid']",
    "      && $meho_c['was_tunnel'] === trim(isset($meho_e['tunnel_network'])",
    "        ? (string)$meho_e['tunnel_network'] : '')",
    "      && $meho_c['was_routes'] === meho_csv(isset($meho_e['local_network'])",
    "        ? $meho_e['local_network'] : '');",
    "  }",
    "  if (!$meho_ok) {",
    "    $meho_stale[] = $meho_c['name'] . '@' . $meho_c['vpnid'];",
    "  }",
    "}",
]


def build_csc_sync_playback(plan: list[dict[str, Any]], csc_count: int) -> str:
    """Return the raw-PHP fragment that creates or updates the planned overrides.

    Only rows with action ``create`` or ``update`` are written. First the
    fragment checks that the overrides are still as the plan saw them
    (*csc_count* overrides, see ``_PHP_STALE_CHECK``). When one is not, it
    prints ``MEHO_CSC_CHANGED=<name@vpnid ...>`` and writes nothing. Else it
    adds each new override and edits each planned one. It touches
    ``tunnel_network`` and ``local_network`` and, for a new entry,
    ``server_list``, ``common_name`` and ``description``. It calls
    ``openvpn_resync_csc()`` for each edited entry (writes that user's file)
    and ``write_config()`` once. It never deletes and restarts nothing.
    """
    rows = [r for r in plan if r["action"] in ("create", "update")]
    path = "$config['openvpn']['openvpn-csc']"
    lines = [
        "global $config;",
        "require_once('openvpn.inc');",
        "$meho_changes = array(",
        *[_php_change(r) for r in rows],
        ");",
        f"$meho_count = {int(csc_count)};",
        f"$meho_csc = (isset({path}) && is_array({path})) ? {path} : array();",
        *_PHP_STALE_CHECK,
        "if (count($meho_stale) > 0) {",
        f"  echo '{_CHANGED_MARKER}' . implode(' ', $meho_stale) . \"\\n\";",
        "  $meho_changes = array();",
        "}",
        "if (count($meho_changes) > 0 && (!isset($config['openvpn'])",
        "    || !is_array($config['openvpn']))) {",
        "  $config['openvpn'] = array();",
        "}",
        f"if (count($meho_changes) > 0 && (!isset({path}) || !is_array({path}))) {{",
        f"  {path} = array();",
        "}",
        "$meho_done = 0;",
        "foreach ($meho_changes as $meho_c) {",
        f"  $meho_hits = meho_hits({path}, $meho_c);",
        "  if ($meho_c['create'] && count($meho_hits) === 0) {",
        "    $meho_new = array('server_list' => $meho_c['vpnid'],",
        "      'common_name' => $meho_c['name'], 'description' => $meho_c['descr']);",
        "    if ($meho_c['tunnel'] !== '') {",
        "      $meho_new['tunnel_network'] = $meho_c['tunnel'];",
        "    }",
        "    if ($meho_c['routes'] !== '') {",
        "      $meho_new['local_network'] = $meho_c['routes'];",
        "    }",
        f"    {path}[] = $meho_new;",
        "    openvpn_resync_csc($meho_new);",
        "    $meho_done++;",
        "  } elseif (!$meho_c['create'] && count($meho_hits) === 1) {",
        "    $meho_k = $meho_hits[0];",
        "    if ($meho_c['tunnel'] !== '') {",
        f"      {path}[$meho_k]['tunnel_network'] = $meho_c['tunnel'];",
        "    }",
        "    if ($meho_c['routes'] !== '') {",
        f"      {path}[$meho_k]['local_network'] = $meho_c['routes'];",
        "    }",
        f"    openvpn_resync_csc({path}[$meho_k]);",
        "    $meho_done++;",
        "  }",
        "}",
        "if ($meho_done > 0) {",
        f"  write_config({_php_squote('meho: sync OpenVPN client overrides')});",
        "}",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _rest_of_config(xml_text: str, touched: set[tuple[str, str]]) -> dict[str, list[Any]]:
    """Return the canonical top-level sections minus the touched overrides."""
    try:
        root = fromstring(xml_text)
    except ParseError as exc:
        raise RuntimeError("config.xml could not be parsed for the read-back") from exc
    section = root.find("openvpn")
    if section is not None:
        for item in list(section.findall("openvpn-csc")):
            key = (
                (item.findtext("common_name") or "").strip(),
                (item.findtext("server_list") or "").strip(),
            )
            if key in touched:
                section.remove(item)
        if _is_empty_container(section):
            root.remove(section)
    rest: dict[str, list[Any]] = {}
    for child in root:
        if child.tag not in _ALWAYS_REWRITTEN_SECTIONS:
            rest.setdefault(child.tag, []).append(_canonical(child))
    return rest


def _verify_sync(before: str, after: str, plan: list[dict[str, Any]]) -> None:
    written = [r for r in plan if r["action"] in ("create", "update")]
    after_entries = parse_csc_entries(after)
    for row in written:
        hits = [
            e
            for e in after_entries
            if e.common_name == row["name"] and e.server_list == [row["server_vpnid"]]
        ]
        wanted_routes = {_canonical_route(r) for r in row["routes"]}
        if (
            len(hits) != 1
            or (row["tunnel_network"] and hits[0].tunnel_raw != row["tunnel_network"])
            or not wanted_routes <= {_canonical_route(r) for r in hits[0].routes}
        ):
            raise RuntimeError(
                f"openvpn.csc.sync verification failed for {row['name']!r} on server "
                f"{row['server_vpnid']}: config.xml does not hold the wanted override after "
                "write_config; the pfSsh.php playback did not persist"
            )
    touched = {(r["name"], r["server_vpnid"]) for r in written}
    if _rest_of_config(before, touched) != _rest_of_config(after, touched):
        raise RuntimeError(
            "openvpn.csc.sync: the change was saved, but other parts of config.xml also "
            "changed while the op ran. Check the pfSense config history before you run "
            "the op again."
        )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def save_rollback_copy(
    self: PfSenseConnector, target: Any, operator: Operator | None, label: str, op: str
) -> str:
    """Copy ``config.xml`` to ``/cf/conf/meho-rollback-<label>-<time>.xml`` (mode 0600).

    Returns the path. Raises when the copy cannot be made or differs.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    copy_path = f"/cf/conf/meho-rollback-{label}-{stamp}.xml"
    saved = await self._run_command(
        target,
        f"umask 077 && cp /cf/conf/config.xml {copy_path} "
        f"&& cmp -s /cf/conf/config.xml {copy_path}",
        operator=operator,
    )
    if saved.exit_status != 0:
        raise RuntimeError(
            f"{op}: could not save the rollback copy of config.xml; nothing was changed"
        )
    return copy_path


def _summary(plan: list[dict[str, Any]]) -> dict[str, int]:
    return {
        a: sum(1 for r in plan if r["action"] == a)
        for a in ("create", "update", "unchanged", "refused")
    }


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in ("description", "_expect")}


def _refused_guidance(plan: list[dict[str, Any]]) -> str:
    refused = "; ".join(
        f"{r['name']} on server {r['server_vpnid']}: {r['reason']} ({r['detail']})"
        for r in plan
        if r["action"] == "refused"
    )
    return (
        f"nothing was written. Refused entries: {refused}. Run with compare_only to see "
        "the whole plan."
    )


@serialized_config_write
async def pfsense_openvpn_csc_sync(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.openvpn.csc.sync`` -- create or extend client overrides."""
    entries = _validate_entries(params.get("entries"))
    compare_only = bool(params.get("compare_only", False))
    before = await _read_config_xml(self, target, operator)
    rendered = await read_rendered_addresses(self, target, operator)
    csc = parse_csc_entries(before)
    plan = plan_csc_sync(entries, parse_openvpn_servers(before), csc, rendered)
    counts = _summary(plan)
    result: dict[str, Any] = {
        "op_class": "write",
        "resource": "openvpn_csc",
        "status": "compared",
        "compare_only": compare_only,
        "applied": False,
        "verified": False,
        "counts": counts,
        "entries": [_public_row(r) for r in plan],
        "rollback_copy": None,
        "guidance": None,
    }
    if counts["refused"]:
        if not compare_only:
            raise PfSenseRefusedError(
                "pfsense.openvpn.csc.sync", "refused", _refused_guidance(plan)
            )
        return {
            **result,
            "status": "refused",
            "guidance": "at least one entry was refused; nothing was written (see entries)",
        }
    if counts["create"] + counts["update"] == 0:
        return {**result, "status": "nothing_to_do", "verified": True}
    if compare_only:
        return result

    copy_path = await save_rollback_copy(self, target, operator, "csc-sync", "openvpn.csc.sync")
    played = await _apply_playback(
        self,
        target,
        script_name=f"meho_openvpn_csc_sync_{secrets.token_hex(4)}",
        script_body=build_csc_sync_playback(plan, len(csc)),
        operator=operator,
    )
    stale = re.search(rf"^{_CHANGED_MARKER}(.*)$", played, re.MULTILINE)
    if stale is not None:
        raise RuntimeError(
            "openvpn.csc.sync: the client overrides changed after the plan was made "
            f"({stale.group(1).strip()}); nothing was written. Run the op again "
            f"(the rollback copy {copy_path} is not needed)."
        )
    _verify_sync(before, await _read_config_xml(self, target, operator), plan)
    return {
        **result,
        "status": "synced",
        "applied": True,
        "verified": True,
        "rollback_copy": copy_path,
    }


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_NEXT_FREE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op_class": {"type": "string", "enum": ["read"]},
        "resource": {"type": "string", "enum": ["openvpn_address"]},
        "status": {"type": "string", "enum": ["found", "exhausted", "no_user_auth_server"]},
        "address_offset": {"type": ["integer", "null"]},
        "servers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "vpnid": {"type": "string"},
                    "description": {"type": "string"},
                    "network": {"type": "string"},
                    "address": {"type": "string"},
                    "tunnel_network": {"type": "string"},
                },
                "required": ["vpnid", "description", "network", "address", "tunnel_network"],
                "additionalProperties": False,
            },
        },
        "sources": {
            "type": "object",
            "properties": {
                "config_entries": {"type": "integer"},
                "rendered_files": {"type": "integer"},
            },
            "required": ["config_entries", "rendered_files"],
            "additionalProperties": False,
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "op_class",
        "resource",
        "status",
        "address_offset",
        "servers",
        "sources",
        "warnings",
    ],
    "additionalProperties": False,
}

_CSC_ENTRY_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "pattern": USER_NAME_RE.pattern,
            "description": "The VPN user name (the override's common name).",
        },
        "server_vpnid": {
            "type": "string",
            "pattern": _VPNID_RE.pattern,
            "description": "Number of the OpenVPN server, as ``next_free`` shows it.",
        },
        "tunnel_address": {
            "type": "string",
            "description": (
                "Fixed IPv4 tunnel address of the user on this server. Needed for a new "
                "entry. For an existing entry it must match the address it has."
            ),
        },
        "routes": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": _MAX_ROUTES,
            "description": "IPv4 networks to push to the client. Added to the existing list.",
        },
        "description": {
            "type": "string",
            "description": "Optional text for a NEW entry (one line, at most 200 characters).",
        },
    },
    "required": ["name", "server_vpnid"],
    "additionalProperties": False,
}

_CSC_SYNC_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": _CSC_ENTRY_PARAMETER_SCHEMA,
            "minItems": 1,
            "maxItems": _MAX_ENTRIES,
            "description": "The overrides you want, one per user and server.",
        },
        "compare_only": {
            "type": "boolean",
            "description": "When true, return the plan and write nothing. Default false.",
        },
    },
    "required": ["entries"],
    "additionalProperties": False,
}

_CSC_SYNC_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op_class": {"type": "string", "enum": ["write"]},
        "resource": {"type": "string", "enum": ["openvpn_csc"]},
        "status": {
            "type": "string",
            "enum": ["compared", "synced", "nothing_to_do", "refused"],
        },
        "compare_only": {"type": "boolean"},
        "applied": {"type": "boolean"},
        "verified": {"type": "boolean"},
        "counts": {
            "type": "object",
            "properties": {
                "create": {"type": "integer"},
                "update": {"type": "integer"},
                "unchanged": {"type": "integer"},
                "refused": {"type": "integer"},
            },
            "required": ["create", "update", "unchanged", "refused"],
            "additionalProperties": False,
        },
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "server_vpnid": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "unchanged", "refused"],
                    },
                    "reason": {"type": ["string", "null"]},
                    "detail": {"type": ["string", "null"]},
                    "tunnel_network": {"type": ["string", "null"]},
                    "routes_added": {"type": "array", "items": {"type": "string"}},
                    "routes": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "name",
                    "server_vpnid",
                    "action",
                    "reason",
                    "detail",
                    "tunnel_network",
                    "routes_added",
                    "routes",
                ],
                "additionalProperties": False,
            },
        },
        "rollback_copy": {"type": ["string", "null"]},
        "guidance": {"type": ["string", "null"]},
    },
    "required": [
        "op_class",
        "resource",
        "status",
        "compare_only",
        "applied",
        "verified",
        "counts",
        "entries",
        "rollback_copy",
        "guidance",
    ],
    "additionalProperties": False,
}


def _guidance(texts: dict[str, Any]) -> dict[str, Any]:
    """Return *texts* plus ``when_to_use``, the older name of ``when_to_call``."""
    return {**texts, "when_to_use": texts["when_to_call"]}


OPENVPN_OPS: tuple[PfSenseOp, ...] = (
    PfSenseOp(
        op_id="pfsense.openvpn.address.next_free",
        handler_attr="openvpn_address_next_free",
        summary="Find the next VPN tunnel address that is free on every user-login OpenVPN server.",
        description=(
            "Reads config.xml and the rendered override files, and returns the lowest "
            "address offset that no client uses on any user-login OpenVPN server, with "
            "the address it gives on each server. Read only. A failed read is an error."
        ),
        parameter_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "examples": [{}],
        },
        response_schema=_NEXT_FREE_RESPONSE_SCHEMA,
        group_key="vpn",
        tags=("read-only", "vpn", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_guidance(
            {
                "when_to_call": (
                    "Call before you give a new person VPN access. It shows the tunnel "
                    "address to use on each server. Both sources of used addresses count "
                    "(the overrides in config.xml and the rendered files), because they do "
                    "not always agree."
                ),
                "parameter_hints": {},
                "output_shape": (
                    "``{status, address_offset, servers: [{vpnid, description, network, "
                    "address, tunnel_network}], sources, warnings}``. ``status`` is "
                    "``found``, ``exhausted`` or ``no_user_auth_server``."
                ),
                "next_step": (
                    "Create the user with ``pfsense.openvpn.user.create``, then pass each "
                    "server's ``address`` as ``tunnel_address`` to "
                    "``pfsense.openvpn.csc.sync`` (use ``compare_only`` first)."
                ),
            }
        ),
    ),
    PfSenseOp(
        op_id="pfsense.openvpn.csc.sync",
        handler_attr="openvpn_csc_sync",
        summary="Create or extend OpenVPN client overrides; can compare only.",
        description=(
            "Creates or extends client-specific overrides: one per user and server, with "
            "the fixed tunnel address and the pushed routes. Checks every entry before it "
            "writes. Never deletes, never removes a route, saves a rollback copy first, "
            "and does not restart OpenVPN."
        ),
        parameter_schema={
            **_CSC_SYNC_PARAMETER_SCHEMA,
            "examples": [
                {
                    "entries": [
                        {
                            "name": "<vpn-user>",
                            "server_vpnid": "1",
                            "tunnel_address": "198.51.100.2",
                            "routes": ["192.0.2.0/24"],
                        }
                    ],
                    "compare_only": True,
                }
            ],
        },
        response_schema=_CSC_SYNC_RESPONSE_SCHEMA,
        group_key="vpn",
        tags=("write", "vpn", "pfsense"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions=_guidance(
            {
                "when_to_call": (
                    "Use to give a VPN user a fixed address and routes on each server, or to "
                    "add a route to an existing user. Safe to run twice: an entry that is "
                    "already right is ``unchanged``. The approval card shows the plan."
                ),
                "preconditions": (
                    "The user exists (``pfsense.openvpn.user.create``). Get each address "
                    "from ``pfsense.openvpn.address.next_free``. Run with ``compare_only: "
                    "true`` first and read the counts."
                ),
                "parameter_hints": {
                    "entries": "Required. One object per user and server.",
                    "compare_only": "Optional. true returns the plan and writes nothing.",
                },
                "output_shape": (
                    "``{status, compare_only, applied, verified, counts, entries, "
                    "rollback_copy, guidance}``. ``status`` is ``compared``, ``synced``, "
                    "``nothing_to_do`` or, with ``compare_only``, ``refused`` (``entries`` "
                    "then says why). Without ``compare_only`` a refused entry fails the call "
                    "and the error names each refused entry."
                ),
                "next_step": (
                    "Check the user with ``pfsense.user.list``. Add the per-user firewall "
                    "rules with ``pfsense.openvpn.rule.sync``."
                ),
            }
        ),
    ),
)


# ---------------------------------------------------------------------------
# Approval preview -- the plan
# ---------------------------------------------------------------------------


async def _csc_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    try:
        entries = _validate_entries(ctx.params.get("entries"))
    except ValueError:
        return None
    preview: dict[str, Any] = {
        "entries_requested": len(entries),
        "compare_only": bool(ctx.params.get("compare_only", False)),
        "counts": None,
        "entries": None,
    }
    if ctx.connector_instance is None:
        return preview
    connector: PfSenseConnector = ctx.connector_instance  # type: ignore[assignment]
    before = await _read_config_xml(connector, ctx.target, ctx.operator)
    rendered = await read_rendered_addresses(connector, ctx.target, ctx.operator)
    plan = plan_csc_sync(
        entries, parse_openvpn_servers(before), parse_csc_entries(before), rendered
    )
    return {**preview, "counts": _summary(plan), "entries": [_public_row(r) for r in plan]}


register_preview_builder("pfsense.openvpn.csc.sync", _csc_preview)
