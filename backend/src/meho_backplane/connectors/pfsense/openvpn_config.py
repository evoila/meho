# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read the OpenVPN servers and client overrides of a pfSense ``config.xml`` (#3993).

Shared by the ops that onboard a VPN user
(:mod:`~meho_backplane.connectors.pfsense.ops_openvpn_csc` and
:mod:`~meho_backplane.connectors.pfsense.ops_openvpn_user`). Everything
here is plain parsing: no SSH call except :func:`read_rendered_addresses`.

Two sources hold a client's fixed tunnel address, and they do not always
agree:

* the **client-specific overrides** in ``config.xml``
  (``<openvpn><openvpn-csc>``): the truth that survives a resync;
* the **rendered files** under ``/var/etc/openvpn/server<id>/csc/<name>``:
  what the running OpenVPN reads. An override that was removed in the
  web page, or written by hand as a file, can leave a file behind.

An address counts as used when either source holds it.

Secrets
-------

Only fixed, non-secret fields are read from an ``<openvpn-server>``
(``vpnid``, ``mode``, ``description``, ``tunnel_network``, ``disable``).
The TLS keys, shared keys and certificate references of the server are
never read.
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.pfsense.ops_config_reads import _parse_config_root
from meho_backplane.connectors.pfsense.ops_read import _xml_text

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "USER_AUTH_MODES",
    "CscEntry",
    "OpenVpnServer",
    "parse_csc_entries",
    "parse_openvpn_servers",
    "read_rendered_addresses",
    "used_addresses",
    "user_addresses",
]

#: The OpenVPN server modes where a person logs in with a local user name
#: and password (with or without a certificate check on top).
USER_AUTH_MODES: Final[frozenset[str]] = frozenset({"server_user", "server_tls_user"})

# The rendered override files that hold a fixed address. The first command
# stops at once when no OpenVPN folder exists (no server was ever started).
# ``grep`` exits 1 when a file has no such line: that is not an error. Any
# other non-zero exit is, and ``find`` then exits non-zero too.
RENDERED_COMMAND: Final[str] = (
    "[ -d /var/etc/openvpn ] || exit 0; "
    "find /var/etc/openvpn -type f -path '*/csc/*' "
    '-exec sh -c \'grep -H "^ifconfig-push " "$@" || [ $? -eq 1 ]\' meho {} +'
)

_RENDERED_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^/var/etc/openvpn/server(\d+)/csc/(.+?):ifconfig-push (\d{1,3}(?:\.\d{1,3}){3})(?:\s|$)"
)


class OpenVpnServer:
    """One ``<openvpn-server>``: the fields the onboarding ops need."""

    __slots__ = ("description", "disabled", "mode", "network", "vpnid")

    def __init__(
        self,
        *,
        vpnid: str,
        mode: str,
        description: str,
        disabled: bool,
        network: ipaddress.IPv4Network | None,
    ) -> None:
        self.vpnid = vpnid
        self.mode = mode
        self.description = description
        self.disabled = disabled
        self.network = network

    @property
    def user_auth(self) -> bool:
        return self.mode in USER_AUTH_MODES


class CscEntry:
    """One ``<openvpn-csc>`` client-specific override."""

    __slots__ = (
        "common_name",
        "description",
        "disabled",
        "routes",
        "server_list",
        "tunnel_interface",
        "tunnel_raw",
    )

    def __init__(
        self,
        *,
        common_name: str,
        server_list: list[str],
        tunnel_raw: str,
        routes: list[str],
        description: str,
        disabled: bool,
    ) -> None:
        self.common_name = common_name
        self.server_list = server_list
        self.tunnel_raw = tunnel_raw
        self.routes = routes
        self.description = description
        self.disabled = disabled
        self.tunnel_interface: ipaddress.IPv4Interface | None = None
        if tunnel_raw:
            try:
                self.tunnel_interface = ipaddress.IPv4Interface(tunnel_raw)
            except ValueError:
                # An alias name or an IPv6 value: no address we can count.
                self.tunnel_interface = None

    def applies_to(self, vpnid: str) -> bool:
        """``True`` when the override is valid for server *vpnid* (an empty list means all)."""
        return not self.server_list or vpnid in self.server_list


def _split_csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def parse_openvpn_servers(xml_text: str) -> list[OpenVpnServer]:
    """Return every ``<openvpn><openvpn-server>`` of *xml_text*.

    A server whose ``tunnel_network`` is not an IPv4 network has
    ``network=None``. Raises
    :class:`~meho_backplane.connectors.pfsense.ops_config_reads.PfSenseConfigParseError`
    when the file cannot be parsed.
    """
    root = _parse_config_root(xml_text)
    section = root.find("openvpn")
    if section is None:
        return []
    servers: list[OpenVpnServer] = []
    for item in section.findall("openvpn-server"):
        raw = (_xml_text(item, "tunnel_network") or "").strip()
        network: ipaddress.IPv4Network | None
        try:
            network = ipaddress.IPv4Network(raw, strict=False) if raw else None
        except ValueError:
            network = None
        servers.append(
            OpenVpnServer(
                vpnid=(_xml_text(item, "vpnid") or "").strip(),
                mode=(_xml_text(item, "mode") or "").strip(),
                description=(_xml_text(item, "description") or "").strip(),
                disabled=item.find("disable") is not None,
                network=network,
            )
        )
    return servers


def parse_csc_entries(xml_text: str) -> list[CscEntry]:
    """Return every ``<openvpn><openvpn-csc>`` of *xml_text*."""
    root = _parse_config_root(xml_text)
    section = root.find("openvpn")
    if section is None:
        return []
    return [
        CscEntry(
            common_name=(_xml_text(item, "common_name") or "").strip(),
            server_list=_split_csv(_xml_text(item, "server_list")),
            tunnel_raw=(_xml_text(item, "tunnel_network") or "").strip(),
            routes=_split_csv(_xml_text(item, "local_network")),
            description=(_xml_text(item, "description") or "").strip(),
            disabled=item.find("disable") is not None,
        )
        for item in section.findall("openvpn-csc")
    ]


async def read_rendered_addresses(
    connector: PfSenseConnector, target: Any, operator: Operator | None
) -> list[tuple[str, str, ipaddress.IPv4Address]]:
    """Return ``(vpnid, common_name, address)`` for each rendered override file.

    Raises :exc:`RuntimeError` when the read fails: a failed read is never
    an empty answer.
    """
    proc = await connector._run_command(target, RENDERED_COMMAND, operator=operator)
    if proc.exit_status != 0:
        raise RuntimeError(
            f"reading the rendered OpenVPN override files failed: exit {proc.exit_status}"
        )
    out = proc.stdout if isinstance(proc.stdout, str) else ""
    rows: list[tuple[str, str, ipaddress.IPv4Address]] = []
    for line in out.splitlines():
        match = _RENDERED_LINE_RE.match(line.strip())
        if match is None:
            continue
        try:
            rows.append((match.group(1), match.group(2), ipaddress.IPv4Address(match.group(3))))
        except ValueError:
            continue
    return rows


def used_addresses(
    server: OpenVpnServer,
    entries: list[CscEntry],
    rendered: list[tuple[str, str, ipaddress.IPv4Address]],
    *,
    ignore_name: str | None = None,
) -> dict[ipaddress.IPv4Address, set[str]]:
    """Return the addresses of *server* that are in use, each with the names that hold it.

    Both sources count. A config entry counts for *server* when its address
    lies in the server's network. A rendered file counts when it sits in
    this server's folder. *ignore_name* leaves out one user's own entries.
    """
    used: dict[ipaddress.IPv4Address, set[str]] = {}
    network = server.network
    if network is None:
        return used
    for entry in entries:
        if entry.common_name == ignore_name or entry.tunnel_interface is None:
            continue
        address = entry.tunnel_interface.ip
        if address in network:
            used.setdefault(address, set()).add(entry.common_name)
    for vpnid, name, address in rendered:
        if vpnid != server.vpnid or name == ignore_name:
            continue
        used.setdefault(address, set()).add(name)
    return used


def user_addresses(
    servers: list[OpenVpnServer], entries: list[CscEntry], name: str
) -> set[ipaddress.IPv4Address]:
    """Return the fixed tunnel addresses of user *name* on the servers that log users in by name.

    An address counts when an override of *name* applies to such a server
    and the address lies in that server's network.
    """
    found: set[ipaddress.IPv4Address] = set()
    for server in servers:
        if not server.user_auth or server.network is None:
            continue
        for entry in entries:
            if (
                entry.common_name == name
                and entry.tunnel_interface is not None
                and entry.applies_to(server.vpnid)
                and entry.tunnel_interface.ip in server.network
            ):
                found.add(entry.tunnel_interface.ip)
    return found
