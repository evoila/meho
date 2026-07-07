# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""pfSense read ops -- 7 read ops for G3.7-T2 (#847).

Adds the following typed ops to :class:`PfSenseConnector`:

* ``pfsense.version`` -- ``cat /etc/version``, structured output.
* ``pfsense.firewall.rules`` -- ``pfctl -sr`` parsed into rule rows.
* ``pfsense.firewall.state`` -- ``pfctl -ss`` parsed into state rows;
  returns rows inline as ``{rows, total}``.
* ``pfsense.nat.rules`` -- ``pfctl -sn`` parsed into NAT rule rows.
* ``pfsense.interface.list`` -- ``ifconfig -a`` parsed.
* ``pfsense.gateway.list`` -- ``/cf/conf/config.xml`` ``<gateways>``
  block parsed.
* ``pfsense.config.show`` -- full ``/cf/conf/config.xml`` content
  returned as a structured envelope.

Pure parsers vs handler thin layer
-----------------------------------

Following the :mod:`~meho_backplane.connectors.bind9.ops_zone`
convention, the heavy lifting lives in pure functions
(:func:`parse_pfctl_rules`, :func:`parse_pfctl_states`,
:func:`parse_pfctl_nat`, :func:`parse_ifconfig`,
:func:`parse_gateways_xml`) that accept captured stdout / file text
and return Python data. The bound-method handlers are the thin SSH-
call + parse + shape layer. The unit suite pins the parsers directly
against fixture text without booting an event loop.

JSONFlux handle pattern -- deferred to the reducer
----------------------------------------------------

All handlers return rows inline. Mirroring the bind9
(:mod:`~meho_backplane.connectors.bind9.ops_zone`) precedent, the
handle is the **reducer's** responsibility — not the connector's.
Setting handle creation here would couple every connector to the
reducer's calibration threshold and bypass the reducer's
audit / TTL / store-routing logic.

The handler ships ``rows`` + ``total`` so the dispatcher's default
reducer can pull both signals (inlined sample size + total) to drive its
threshold check, exactly as the bind9 sibling does. The default
:class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
wraps the payload in a ``ResultHandle`` once the set exceeds its
threshold; smaller payloads pass through unchanged.

References
----------

* Task: G3.7-T2 (#847).
* Parent initiative: G3.7 (#370).
* Bind9 precedent: :mod:`meho_backplane.connectors.bind9.ops_zone`.
* K8s precedent: :mod:`meho_backplane.connectors.kubernetes.ops_core`.
* pfctl man page: https://man.freebsd.org/cgi/man.cgi?pfctl
* asyncssh ``_run_command``:
  :mod:`meho_backplane.connectors.adapters.ssh` (L145).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from defusedxml.ElementTree import ParseError, fromstring

from meho_backplane.connectors.pfsense.ops import PfSenseOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "READ_OPS",
    "parse_gateways_xml",
    "parse_ifconfig",
    "parse_pfctl_nat",
    "parse_pfctl_rules",
    "parse_pfctl_states",
    "pfsense_config_show",
    "pfsense_firewall_rules",
    "pfsense_firewall_state",
    "pfsense_gateway_list",
    "pfsense_interface_list",
    "pfsense_nat_rules",
    "pfsense_version",
]


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------

# ``pfctl -sr`` emits one rule per line in the form:
#   pass in quick on em0 inet from any to any
#   block drop in log quick on em0 proto tcp from <table> to any port = 22
# The first token is the action; after optional sub-action qualifiers
# (``drop``, ``return``, ``return-rst``, ``return-icmp``) comes the
# optional direction (``in``/``out``/``in/out``).
_PFCTL_RULE_ACTION_RE = re.compile(
    r"^(?P<action>pass|block|match|anchor)"
    r"(?:\s+(?:drop|return(?:-rst|-icmp(?:6)?)?))?"  # optional sub-action qualifier
    r"(?:\s+(?P<direction>in/out|in(?!\w)|out(?!\w)))?"
    r"(?P<rest>.*?)$",
)


def parse_pfctl_rules(output: str) -> list[dict[str, Any]]:
    """Parse ``pfctl -sr`` stdout into a list of rule dicts.

    Returns one dict per non-empty, non-comment line:

    .. code-block:: python

        {"action": "pass", "direction": "in", "rule": "<full line>"}

    ``action`` is the opening token (``pass`` / ``block`` / ``match`` /
    ``anchor``). ``direction`` is ``"in"`` / ``"out"`` / ``"in/out"`` /
    ``None`` when absent. ``rule`` is the full unparsed line so callers
    can render the original pfctl output verbatim.

    Comment lines (``#``) and blank lines are skipped. Unrecognised
    lines (e.g. ``@anchor ...`` continuation tokens) are included with
    ``action=None`` and ``direction=None``.

    >>> rows = parse_pfctl_rules("pass in quick on em0 all\\nblock drop out all\\n")
    >>> rows[0]["action"]
    'pass'
    >>> rows[1]["action"]
    'block'
    >>> rows[1]["direction"]
    'out'
    """
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PFCTL_RULE_ACTION_RE.match(line)
        if m:
            rows.append(
                {
                    "action": m.group("action"),
                    "direction": m.group("direction") or None,
                    "rule": line,
                }
            )
        else:
            rows.append({"action": None, "direction": None, "rule": line})
    return rows


# ``pfctl -ss`` emits one state per line in the forms:
#   <proto> <interface> <src> -> <dst>: <state>/<state> Pkts: N Bytes: N
#   <proto> <interface> <src> <-> <dst>: <state>/<state> ...
# The exact format varies by pfSense version. We extract the common fields.
_STATE_PROTO_RE = re.compile(
    r"^(?P<proto>\S+)\s+"
    r"(?P<iface>\S+)\s+"
    r"(?P<src>[^\s<>-]+)\s+"
    r"(?P<direction><->|->|<-)\s+"
    r"(?P<dst>\S+?)(?=:\s|\s|$)"  # dst ends at ': STATE' boundary or whitespace
    r"(?P<rest>.*)?$",
)


def parse_pfctl_states(output: str) -> list[dict[str, Any]]:
    """Parse ``pfctl -ss`` stdout into a list of state dicts.

    Returns one dict per non-empty, non-comment line:

    .. code-block:: python

        {
            "proto": "tcp",
            "iface": "em0",
            "src": "10.0.0.1:50234",
            "direction": "->",
            "dst": "93.184.216.34:443",
            "state": "<full line>",
        }

    ``state`` carries the full unparsed line for forward compatibility.
    Lines that do not match the proto/iface/src/dir/dst pattern are
    included with ``proto=None``, ``iface=None``, ``src=None``,
    ``direction=None``, ``dst=None``, and ``state=<full line>``.

    The ``total`` of the returned list is the caller's responsibility
    (``len(rows)``).

    >>> rows = parse_pfctl_states("tcp em0 10.0.0.1:1234 -> 1.2.3.4:443: ESTABLISHED\\n")
    >>> rows[0]["proto"]
    'tcp'
    >>> rows[0]["dst"]
    '1.2.3.4:443'
    """
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _STATE_PROTO_RE.match(line)
        if m:
            rows.append(
                {
                    "proto": m.group("proto"),
                    "iface": m.group("iface"),
                    "src": m.group("src"),
                    "direction": m.group("direction"),
                    "dst": m.group("dst"),
                    "state": line,
                }
            )
        else:
            rows.append(
                {
                    "proto": None,
                    "iface": None,
                    "src": None,
                    "direction": None,
                    "dst": None,
                    "state": line,
                }
            )
    return rows


# ``pfctl -sn`` emits NAT rules in the form:
#   nat on em0 inet from <net> to any -> <nat_addr>
#   rdr on em0 proto tcp from any to any port = 80 -> <rdr_addr> port 80
_NAT_RULE_RE = re.compile(
    r"^(?P<action>nat|rdr|binat|no\s+nat|no\s+rdr)\s+"
    r"(?P<direction>in|out|in/out)?\s*"
    r"(?P<rest>.*?)$",
)


def parse_pfctl_nat(output: str) -> list[dict[str, Any]]:
    """Parse ``pfctl -sn`` stdout into a list of NAT rule dicts.

    Returns one dict per non-empty, non-comment line:

    .. code-block:: python

        {"action": "nat", "direction": None, "rule": "<full line>"}

    ``action`` is the leading token (``nat`` / ``rdr`` / ``binat`` /
    ``no nat`` / ``no rdr``). Unrecognised lines are included verbatim
    with ``action=None``.

    >>> rows = parse_pfctl_nat("nat on em0 from 192.168.1.0/24 to any -> 1.2.3.4\\n")
    >>> rows[0]["action"]
    'nat'
    """
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _NAT_RULE_RE.match(line)
        if m:
            rows.append(
                {
                    "action": m.group("action").replace(" ", "_"),
                    "direction": m.group("direction") or None,
                    "rule": line,
                }
            )
        else:
            rows.append({"action": None, "direction": None, "rule": line})
    return rows


# ``ifconfig -a`` groups interface blocks. Each block starts with
# ``<name>: flags=...`` and continues with indented detail lines.
_IFCONFIG_HEADER_RE = re.compile(
    r"^(?P<name>\S+?):\s+flags=\S+\s+metric\s+(?P<metric>\d+)\s+mtu\s+(?P<mtu>\d+)",
)
_IFCONFIG_INET_RE = re.compile(
    r"^\s+inet\s+(?P<addr>\S+)\s+netmask\s+(?P<netmask>\S+)"
    r"(?:\s+broadcast\s+(?P<broadcast>\S+))?",
)
_IFCONFIG_INET6_RE = re.compile(
    r"^\s+inet6\s+(?P<addr6>[a-fA-F0-9:%]+/?\d*)",
)
_IFCONFIG_STATUS_RE = re.compile(r"^\s+status:\s+(?P<status>\S+)")
_IFCONFIG_MEDIA_RE = re.compile(r"^\s+media:\s+(?P<media>.+)")
_IFCONFIG_ETHER_RE = re.compile(r"^\s+ether\s+(?P<ether>[0-9A-Fa-f:]+)")


def parse_ifconfig(output: str) -> list[dict[str, Any]]:
    """Parse ``ifconfig -a`` stdout into a list of interface dicts.

    Returns one dict per interface block:

    .. code-block:: python

        {
            "name": "em0",
            "metric": 0,
            "mtu": 1500,
            "inet": ["192.168.1.1/24"],
            "inet6": [],
            "ether": "00:11:22:33:44:55",
            "status": "active",
            "media": "Ethernet autoselect (1000baseT <full-duplex>)",
        }

    ``inet`` is a list of IPv4 addr/prefix strings. ``inet6`` is a list
    of IPv6 addresses. ``ether`` and ``status`` and ``media`` are the
    first encountered values for each interface (most interfaces have at
    most one). ``None`` when not present. The ``metric`` and ``mtu``
    fields are integers; absent means the line didn't match the header
    regex and the interface record is excluded.

    >>> output = (
    ...     "em0: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> metric 0 mtu 1500\\n"
    ...     "\\tinet 10.0.0.1 netmask 0xffffff00 broadcast 10.0.0.255\\n"
    ...     "\\tstatus: active\\n"
    ... )
    >>> ifaces = parse_ifconfig(output)
    >>> ifaces[0]["name"]
    'em0'
    >>> ifaces[0]["inet"]
    ['10.0.0.1/24']
    """
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in output.splitlines():
        m_hdr = _IFCONFIG_HEADER_RE.match(line)
        if m_hdr:
            if current is not None:
                rows.append(current)
            current = {
                "name": m_hdr.group("name"),
                "metric": int(m_hdr.group("metric")),
                "mtu": int(m_hdr.group("mtu")),
                "inet": [],
                "inet6": [],
                "ether": None,
                "status": None,
                "media": None,
            }
            continue
        if current is None:
            continue
        m_inet = _IFCONFIG_INET_RE.match(line)
        if m_inet:
            addr = m_inet.group("addr")
            netmask_hex = m_inet.group("netmask")
            cidr = _netmask_to_cidr(netmask_hex)
            current["inet"].append(f"{addr}/{cidr}" if cidr else addr)
            continue
        m_inet6 = _IFCONFIG_INET6_RE.match(line)
        if m_inet6:
            current["inet6"].append(m_inet6.group("addr6"))
            continue
        if current["ether"] is None:
            m_ether = _IFCONFIG_ETHER_RE.match(line)
            if m_ether:
                current["ether"] = m_ether.group("ether")
                continue
        if current["status"] is None:
            m_status = _IFCONFIG_STATUS_RE.match(line)
            if m_status:
                current["status"] = m_status.group("status")
                continue
        if current["media"] is None:
            m_media = _IFCONFIG_MEDIA_RE.match(line)
            if m_media:
                current["media"] = m_media.group("media").strip()
                continue
    if current is not None:
        rows.append(current)
    return rows


def _netmask_to_cidr(netmask: str) -> int | None:
    """Convert a hex netmask (``0xffffff00``) or dotted decimal to CIDR prefix.

    Returns ``None`` when the format is unrecognised.

    >>> _netmask_to_cidr("0xffffff00")
    24
    >>> _netmask_to_cidr("255.255.255.0")
    24
    >>> _netmask_to_cidr("garbage")
    """
    try:
        if netmask.startswith("0x"):
            val = int(netmask, 16)
        else:
            parts = netmask.split(".")
            if len(parts) != 4:
                return None
            val = 0
            for part in parts:
                val = (val << 8) | int(part)
        # Count leading 1-bits.
        return bin(val).count("1")
    except (ValueError, OverflowError):
        return None


def parse_gateways_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse the ``<gateways>`` block from ``/cf/conf/config.xml``.

    Accepts either the full ``config.xml`` text or a ``<gateways>`` snippet.
    Returns one dict per ``<gateway_item>`` child:

    .. code-block:: python

        {
            "name": "WAN_DHCP",
            "interface": "wan",
            "gateway": "192.168.0.1",
            "monitor": "192.168.0.1",
            "descr": "Interface WAN Gateway",
            "defaultgw": True,
        }

    Missing optional tags produce ``None`` for that key. ``defaultgw``
    is ``True`` when the ``<defaultgw/>`` element is present.
    Returns an empty list on any parse failure.

    >>> xml = (
    ...     "<gateways><gateway_item>"
    ...     "<name>WAN_GW</name><gateway>1.2.3.4</gateway>"
    ...     "</gateway_item></gateways>"
    ... )
    >>> gws = parse_gateways_xml(xml)
    >>> gws[0]["name"]
    'WAN_GW'
    >>> gws[0]["gateway"]
    '1.2.3.4'
    """
    if not xml_text.strip():
        return []
    try:
        root = fromstring(xml_text)
    except ParseError:
        return []
    gw_root = root if root.tag == "gateways" else root.find(".//gateways")
    if gw_root is None:
        return []
    rows: list[dict[str, Any]] = []
    for item in gw_root.findall("gateway_item"):
        rows.append(
            {
                "name": _xml_text(item, "name"),
                "interface": _xml_text(item, "interface"),
                "gateway": _xml_text(item, "gateway"),
                "monitor": _xml_text(item, "monitor"),
                "descr": _xml_text(item, "descr"),
                "defaultgw": item.find("defaultgw") is not None,
            }
        )
    return rows


def _xml_text(element: Any, tag: str) -> str | None:
    """Return the text of *tag* under *element*, or ``None``.

    *element* is a ``defusedxml``-parsed ElementTree node; defusedxml ships
    no type stubs, so it surfaces as ``Any`` (annotating it with the stdlib
    ``Element`` type would re-introduce the ``xml.etree`` import that Semgrep
    flags as an XXE vector).
    """
    child = element.find(tag)
    return child.text if child is not None else None


# ---------------------------------------------------------------------------
# Handler functions (bound-method shims on PfSenseConnector)
# ---------------------------------------------------------------------------


async def pfsense_version(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Return pfSense version details from ``/etc/version``.

    Op-id: ``pfsense.version``. Runs the same ``cat /etc/version``
    command as :meth:`PfSenseConnector.fingerprint` but returns the
    structured version payload directly (without the full
    :class:`FingerprintResult` envelope) so agent prompts get a
    clean JSON response.
    """
    del params  # declared empty; intentionally ignored
    from meho_backplane.connectors.pfsense.connector import parse_pfsense_version

    proc = await self._run_command(target, "cat /etc/version", operator=operator)
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    content = stdout if isinstance(stdout, str) else ""
    if not content.strip() or proc.exit_status != 0:
        return {"version": None, "build": None, "kernel": None, "error": "empty output"}
    parsed = parse_pfsense_version(content)
    return {
        "version": parsed["version"],
        "build": parsed["build"],
        "kernel": parsed["kernel"],
    }


async def pfsense_firewall_rules(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Return the active pfSense firewall ruleset from ``pfctl -sr``.

    Op-id: ``pfsense.firewall.rules``. Parses pfctl's filter rule dump
    into structured row dicts. Each row carries the leading action token,
    optional direction, and the full unparsed rule line.
    """
    del params  # declared empty; intentionally ignored
    proc = await self._run_command(target, "pfctl -sr", operator=operator)
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    content = stdout if isinstance(stdout, str) else ""
    if proc.exit_status != 0 and not content.strip():
        return {"rows": [], "total": 0, "error": f"pfctl exit {proc.exit_status}"}
    rows = parse_pfctl_rules(content)
    return {"rows": rows, "total": len(rows)}


async def pfsense_firewall_state(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Return the active pfSense state table from ``pfctl -ss``.

    Op-id: ``pfsense.firewall.state``. Active connection state tables
    on busy firewalls can contain thousands of rows; this handler ships
    the full parsed list plus a ``total`` count so the dispatcher's
    default reducer can spill out-of-band when the row count exceeds its
    threshold (key ``pfsense_firewall_state``). The default
    :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
    wraps large state tables in a ``ResultHandle``; smaller payloads pass
    through unchanged.
    """
    del params  # declared empty; intentionally ignored
    proc = await self._run_command(target, "pfctl -ss", operator=operator)
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    content = stdout if isinstance(stdout, str) else ""
    if proc.exit_status != 0 and not content.strip():
        return {"rows": [], "total": 0, "error": f"pfctl exit {proc.exit_status}"}
    rows = parse_pfctl_states(content)
    return {"rows": rows, "total": len(rows)}


async def pfsense_nat_rules(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Return the active pfSense NAT ruleset from ``pfctl -sn``.

    Op-id: ``pfsense.nat.rules``. Parses pfctl's NAT rule dump into
    structured row dicts. Each row carries the action token (``nat`` /
    ``rdr`` / ``binat`` / ``no_nat`` / ``no_rdr``), optional direction,
    and the full unparsed rule line.
    """
    del params  # declared empty; intentionally ignored
    proc = await self._run_command(target, "pfctl -sn", operator=operator)
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    content = stdout if isinstance(stdout, str) else ""
    if proc.exit_status != 0 and not content.strip():
        return {"rows": [], "total": 0, "error": f"pfctl exit {proc.exit_status}"}
    rows = parse_pfctl_nat(content)
    return {"rows": rows, "total": len(rows)}


async def pfsense_interface_list(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Return the pfSense interface list from ``ifconfig -a``.

    Op-id: ``pfsense.interface.list``. Parses ifconfig output into
    structured interface dicts with ``name``, ``mtu``, ``inet`` (list
    of IPv4 CIDR addresses), ``inet6`` (list), ``ether`` (MAC),
    ``status``, and ``media`` fields.
    """
    del params  # declared empty; intentionally ignored
    proc = await self._run_command(target, "ifconfig -a", operator=operator)
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    content = stdout if isinstance(stdout, str) else ""
    if proc.exit_status != 0 and not content.strip():
        return {"rows": [], "total": 0, "error": f"ifconfig exit {proc.exit_status}"}
    rows = parse_ifconfig(content)
    return {"rows": rows, "total": len(rows)}


async def pfsense_gateway_list(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Return the pfSense gateway list from ``/cf/conf/config.xml``.

    Op-id: ``pfsense.gateway.list``. Reads the live pfSense
    configuration file and parses the ``<gateways>`` block into
    structured gateway dicts. Each gateway dict carries ``name``,
    ``interface``, ``gateway`` (IP), ``monitor`` (monitor IP),
    ``descr``, and ``defaultgw`` (bool).
    """
    del params  # declared empty; intentionally ignored
    proc = await self._run_command(target, "cat /cf/conf/config.xml", operator=operator)
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    content = stdout if isinstance(stdout, str) else ""
    if proc.exit_status != 0 and not content.strip():
        return {
            "rows": [],
            "total": 0,
            "error": f"cat /cf/conf/config.xml exit {proc.exit_status}",
        }
    rows = parse_gateways_xml(content)
    return {"rows": rows, "total": len(rows)}


async def pfsense_config_show(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Return the full pfSense configuration from ``/cf/conf/config.xml``.

    Op-id: ``pfsense.config.show``. Returns the raw XML content of
    the live pfSense configuration file plus its character length.
    The XML is returned as a string so callers can parse further or
    search for specific sections. Does not attempt to parse the full
    XML tree (the pfSense config schema is large and version-variable);
    use ``pfsense.gateway.list`` for structured gateway data.
    """
    del params  # declared empty; intentionally ignored
    proc = await self._run_command(target, "cat /cf/conf/config.xml", operator=operator)
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    content = stdout if isinstance(stdout, str) else ""
    if proc.exit_status != 0 and not content.strip():
        return {
            "config_xml": None,
            "length": 0,
            "error": f"cat /cf/conf/config.xml exit {proc.exit_status}",
        }
    return {"config_xml": content, "length": len(content)}


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

#: Curated ``when_to_use`` for the ``firewall`` group.
_WHEN_TO_USE_FIREWALL = (
    "Use for pfSense firewall operations: reading active filter rules "
    "(``pfsense.firewall.rules``) or the active state table "
    "(``pfsense.firewall.state``). Call ``pfsense.firewall.rules`` when "
    "the operator wants to audit the ruleset; call "
    "``pfsense.firewall.state`` when the operator wants to inspect active "
    "connections. The state table can be large on busy firewalls; rows "
    "are returned inline as ``{rows, total}``. A large state list is "
    "reduced to a JSONFlux handle carrying a bounded inline sample plus a "
    "``fetch_more`` envelope; to act on more than the sample, re-call with "
    "a narrower filter rather than expecting a handle read-back tool."
)

#: Curated ``when_to_use`` for the ``nat`` group.
_WHEN_TO_USE_NAT = (
    "Use for pfSense NAT operations: reading the active NAT ruleset "
    "(``pfsense.nat.rules``). Call when the operator wants to audit "
    "port-forwarding, outbound NAT, or 1:1 NAT rules. Each row "
    "carries the rule action (``nat``/``rdr``/``binat``) and the "
    "full unparsed pfctl NAT rule line."
)

#: Curated ``when_to_use`` for the ``network`` group (interfaces).
_WHEN_TO_USE_NETWORK = (
    "Use for pfSense network-interface operations: listing all "
    "interfaces (``pfsense.interface.list``) or listing configured "
    "gateways (``pfsense.gateway.list``). Call "
    "``pfsense.interface.list`` when the operator wants IP address, "
    "MAC, MTU, or link-status information. Call "
    "``pfsense.gateway.list`` when the operator wants routing-gateway "
    "configuration from the pfSense config."
)

#: Curated ``when_to_use`` for the ``config`` group.
_WHEN_TO_USE_CONFIG = (
    "Use for pfSense configuration operations: reading the full "
    "pfSense configuration (``pfsense.config.show``) or getting a "
    "structured version summary (``pfsense.version``). Call "
    "``pfsense.config.show`` when the operator needs to inspect or "
    "export the complete pfSense config.xml. Call "
    "``pfsense.version`` when a structured version output is needed "
    "without the full FingerprintResult envelope."
)

_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

READ_OPS: tuple[PfSenseOp, ...] = (
    PfSenseOp(
        op_id="pfsense.version",
        handler_attr="get_version",
        summary="Return the pfSense firewall version and FreeBSD kernel identifier.",
        description=(
            "Runs ``cat /etc/version`` over SSH and returns a structured "
            "dict with the pfSense release string (e.g. ``2.7.2-RELEASE``), "
            "the full build line, and the FreeBSD kernel identifier. Use "
            "when a structured version output is needed without the full "
            "``pfsense.about`` FingerprintResult envelope. No params; safe "
            "to call on any healthy pfSense target."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "version": {"type": ["string", "null"]},
                "build": {"type": ["string", "null"]},
                "kernel": {"type": ["string", "null"]},
                "error": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="config",
        tags=("read-only", "version", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_CONFIG,
            "parameter_hints": {},
            "output_shape": (
                "Flat dict; ``version`` is the pfSense release string "
                "(e.g. ``2.7.2-RELEASE``), ``build`` is the full first "
                "line of ``/etc/version``, ``kernel`` is the FreeBSD "
                "kernel identifier (e.g. ``FreeBSD 14.1-RELEASE-p5``). "
                "``error`` is set when the command failed."
            ),
        },
    ),
    PfSenseOp(
        op_id="pfsense.firewall.rules",
        handler_attr="firewall_rules",
        summary="List the active pfSense firewall filter rules from pfctl.",
        description=(
            "Runs ``pfctl -sr`` over SSH and parses the active ruleset "
            "into structured rule rows. Each row carries the action "
            "(``pass``/``block``/``match``/``anchor``), optional direction "
            "(``in``/``out``/``in/out``), and the full unparsed pfctl rule "
            "line. Returns a ``{rows, total}`` envelope. No params; safe "
            "to call on any healthy pfSense target with shell access."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": ["string", "null"]},
                            "direction": {"type": ["string", "null"]},
                            "rule": {"type": "string"},
                        },
                    },
                },
                "total": {"type": "integer"},
            },
            "additionalProperties": True,
        },
        group_key="firewall",
        tags=("read-only", "firewall", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_FIREWALL,
            "parameter_hints": {},
            "output_shape": (
                "``{rows: [{action, direction, rule}], total: N}``. "
                "``action`` is the pfctl leading token (``pass`` / "
                "``block`` / ``match`` / ``anchor``). ``direction`` is "
                "``in`` / ``out`` / ``in/out`` or ``null`` when the "
                "directive is absent. ``rule`` is the full unparsed line."
            ),
        },
    ),
    PfSenseOp(
        op_id="pfsense.firewall.state",
        handler_attr="firewall_state",
        summary="List active pfSense connection-state table entries from pfctl.",
        description=(
            "Runs ``pfctl -ss`` over SSH and parses the active state "
            "table into structured state rows. Each row carries the "
            "protocol, interface, source address, direction, destination "
            "address, and the full unparsed pfctl state line. Returns "
            "rows inline as ``{rows, total}``. The state table can be "
            "very large on busy firewalls. No params; safe to call on "
            "any healthy pfSense target with shell access."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "proto": {"type": ["string", "null"]},
                            "iface": {"type": ["string", "null"]},
                            "src": {"type": ["string", "null"]},
                            "direction": {"type": ["string", "null"]},
                            "dst": {"type": ["string", "null"]},
                            "state": {"type": "string"},
                        },
                    },
                },
                "total": {"type": "integer"},
            },
            "additionalProperties": True,
        },
        group_key="firewall",
        tags=("read-only", "firewall", "state-table", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_FIREWALL,
            "parameter_hints": {},
            "output_shape": (
                "``{rows: [{proto, iface, src, direction, dst, state}], "
                "total: N}``. Rows are returned inline; ``total`` can be "
                "in the thousands on busy firewalls. A large state list is "
                "reduced to a JSONFlux handle with a bounded inline sample "
                "plus a ``fetch_more`` envelope; re-call with a narrower "
                "filter to act on more than the sample."
            ),
        },
    ),
    PfSenseOp(
        op_id="pfsense.nat.rules",
        handler_attr="nat_rules",
        summary="List the active pfSense NAT ruleset from pfctl.",
        description=(
            "Runs ``pfctl -sn`` over SSH and parses the active NAT "
            "ruleset into structured rule rows. Each row carries the "
            "action (``nat``/``rdr``/``binat``/``no_nat``/``no_rdr``), "
            "optional direction, and the full unparsed pfctl NAT rule "
            "line. Returns a ``{rows, total}`` envelope. No params; safe "
            "to call on any healthy pfSense target."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": ["string", "null"]},
                            "direction": {"type": ["string", "null"]},
                            "rule": {"type": "string"},
                        },
                    },
                },
                "total": {"type": "integer"},
            },
            "additionalProperties": True,
        },
        group_key="nat",
        tags=("read-only", "nat", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_NAT,
            "parameter_hints": {},
            "output_shape": (
                "``{rows: [{action, direction, rule}], total: N}``. "
                "``action`` is the pfctl leading token "
                "(``nat``/``rdr``/``binat``/``no_nat``/``no_rdr``). "
                "``rule`` is the full unparsed pfctl NAT rule line."
            ),
        },
    ),
    PfSenseOp(
        op_id="pfsense.interface.list",
        handler_attr="interface_list",
        summary="List pfSense network interfaces from ifconfig.",
        description=(
            "Runs ``ifconfig -a`` over SSH and parses the output into "
            "structured interface dicts. Each dict carries the interface "
            "name, MTU, IP addresses (IPv4 CIDR list and IPv6 list), "
            "MAC address, link status, and media type. Returns a "
            "``{rows, total}`` envelope. No params; safe to call on any "
            "healthy pfSense target."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "metric": {"type": "integer"},
                            "mtu": {"type": "integer"},
                            "inet": {"type": "array", "items": {"type": "string"}},
                            "inet6": {"type": "array", "items": {"type": "string"}},
                            "ether": {"type": ["string", "null"]},
                            "status": {"type": ["string", "null"]},
                            "media": {"type": ["string", "null"]},
                        },
                    },
                },
                "total": {"type": "integer"},
            },
            "additionalProperties": True,
        },
        group_key="network",
        tags=("read-only", "network", "interface", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_NETWORK,
            "parameter_hints": {},
            "output_shape": (
                "``{rows: [{name, metric, mtu, inet, inet6, ether, status, "
                "media}], total: N}``. ``inet`` is a list of IPv4 CIDR "
                "strings (e.g. ``['192.168.1.1/24']``). ``inet6`` is a "
                "list of IPv6 address strings. ``ether`` is the MAC "
                "address or ``null``. ``status`` is ``active`` / "
                "``no carrier`` / ``null``."
            ),
        },
    ),
    PfSenseOp(
        op_id="pfsense.gateway.list",
        handler_attr="gateway_list",
        summary="List pfSense routing gateways from config.xml.",
        description=(
            "Reads ``/cf/conf/config.xml`` over SSH and parses the "
            "``<gateways>`` block into structured gateway dicts. Each "
            "dict carries the gateway name, interface, gateway IP, "
            "monitor IP, description, and whether it is the default "
            "gateway. Returns a ``{rows, total}`` envelope. No params; "
            "safe to call on any healthy pfSense target."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": ["string", "null"]},
                            "interface": {"type": ["string", "null"]},
                            "gateway": {"type": ["string", "null"]},
                            "monitor": {"type": ["string", "null"]},
                            "descr": {"type": ["string", "null"]},
                            "defaultgw": {"type": "boolean"},
                        },
                    },
                },
                "total": {"type": "integer"},
            },
            "additionalProperties": True,
        },
        group_key="network",
        tags=("read-only", "network", "gateway", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_NETWORK,
            "parameter_hints": {},
            "output_shape": (
                "``{rows: [{name, interface, gateway, monitor, descr, "
                "defaultgw}], total: N}``. ``defaultgw`` is ``true`` "
                "when the ``<defaultgw/>`` element is present in the "
                "pfSense config. ``gateway`` is the gateway IP address. "
                "``interface`` is the pfSense interface logical name "
                "(e.g. ``wan``, ``lan``)."
            ),
        },
    ),
    PfSenseOp(
        op_id="pfsense.config.show",
        handler_attr="config_show",
        summary="Return the full pfSense configuration as XML.",
        description=(
            "Reads ``/cf/conf/config.xml`` over SSH and returns the raw "
            "XML content and its character length. Use when the operator "
            "needs to inspect or export the complete pfSense "
            "configuration. For structured gateway data, prefer "
            "``pfsense.gateway.list``. No params; safe to call on any "
            "healthy pfSense target."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "config_xml": {"type": ["string", "null"]},
                "length": {"type": "integer"},
                "error": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="config",
        tags=("read-only", "config", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_CONFIG,
            "parameter_hints": {},
            "output_shape": (
                "``{config_xml: '<string>', length: N}``. ``config_xml`` "
                "is the raw pfSense ``config.xml`` content as a string. "
                "``length`` is the character count. ``error`` is set when "
                "the command failed."
            ),
        },
    ),
)
