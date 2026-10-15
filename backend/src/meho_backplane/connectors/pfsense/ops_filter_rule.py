# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: declarative op module (parsers + small handlers + agent-facing
# op metadata / JSON schemas / llm_instructions) mirroring the ops_write.py /
# ops_delete.py siblings' single-module-per-op-group convention.

"""pfSense filter-rule write ops -- ``pfsense.filter.rule.add`` / ``.delete`` (#3850).

Adds a governed way to add and remove ONE firewall (filter) rule, so a
change such as a segment-to-segment block rule goes through the same
approval, audit and read-back path as the other pfSense writes, and is
undone by a governed inverse op instead of a hand-written script.

* ``pfsense.filter.rule.add`` -- add one ``<filter><rule>`` (``block`` /
  ``pass`` / ``match``; one interface, or floating with a direction;
  ``ipprotocol``; source / destination as ``any`` / IP / CIDR with an
  optional port; ``quick`` for floating rules; optional ``log``). The caller
  picks the rule's ``ridentifier``. pfSense stores it as the rule's
  ``<tracker>`` and compiles it into the pf rule as ``ridentifier <n>``
  (``filter.inc``), so it is a stable id for logs, re-runs and the delete.
  ``safety_level="dangerous"`` + ``requires_approval=True``: a firewall rule
  changes what traffic the whole firewall lets through, so a human approves
  every add.
* ``pfsense.filter.rule.delete`` -- delete ONE rule by ``ridentifier`` on the
  governed-delete tier (``destructive``, mandatory approval, preview-hash
  binding, blast-radius preview), the same as ``pfsense.nat.delete``.

Both ops use the ``pfSsh.php playback`` idiom of
:mod:`~meho_backplane.connectors.pfsense.ops_write` through the shared
:func:`~meho_backplane.connectors.pfsense.ops_write._apply_playback`, then
apply with ``filter_configure()`` and read ``config.xml`` back.

Guards (fail closed)
--------------------

* **Own management path.** The firewall is also the path MEHO uses to reach
  it. Before an add of a ``block`` rule, and before a delete of an enabled
  ``pass`` rule, the handler reads the SSH session's own flow from
  ``$SSH_CONNECTION`` (client IP, server IP, server port) and refuses
  (``mgmt_path``) when the rule could match that flow. Interface and
  direction are ignored on purpose (the check over-matches rather than
  under-matches). If the flow cannot be read, the op refuses
  (``mgmt_path_unknown``).
* **One object by stable id.** The ``ridentifier`` must be a 9-digit number
  (100000000-999999999). pfSense never assigns ids in that range itself (its
  own generated rules use 10000-13999, 10000000+ and 1000000000+; GUI rules use
  a unix timestamp), so a MEHO rule never collides with a built-in one. An
  add whose id already exists is a no-op when the stored rule is the same
  (``already_present``) and is refused when it differs (``conflict``). Two
  rules with the same id is ``ambiguous`` and refused by both ops.
* **Persist only on the expected change.** The PHP fragment writes the
  config only when the id is still absent (add) or exactly one rule was
  removed (delete). The read-back then checks the rule count moved by
  exactly one.

Rule order
----------

pfSense evaluates interface rules top-down and the first match wins, so a
``block`` rule under a broad ``pass`` rule has no effect. ``position``
(``top`` by default, or ``bottom``) places the new rule first or last among
the rules of its interface (or among the floating rules), the same way the
GUI does, including the GUI's separator shift and ``filter_rules_sort()``.

References
----------

* Task: #3850. Parent decision: ``docs/decisions/governed-delete-operations.md``.
* pfSense 2.7.2 sources: ``firewall_rules_edit.php`` (rule shape, tracker),
  ``firewall_rules.php`` (delete + separators), ``filter.inc``
  (``ridentifier``, ``get_interface_ruleindex``, ``shift_separators``),
  ``util.inc`` (``pconfig_to_address``).
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING, Any

from defusedxml.ElementTree import ParseError, fromstring

from meho_backplane.connectors.pfsense.ops import PfSenseOp
from meho_backplane.connectors.pfsense.ops_write import (
    _INTERFACE_RE,
    _apply_playback,
    _child_text,
    _php_squote,
    _read_config_xml,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "FILTER_RULE_OPS",
    "parse_filter_rules_xml",
    "parse_ssh_connection",
    "pfsense_filter_rule_add",
    "pfsense_filter_rule_delete",
    "register_pfsense_filter_rule_preview_builders",
    "rule_matches_mgmt_flow",
]

# Nine digits, first digit non-zero: 100000000-999999999. pfSense's own
# generated rules use 10000-13999, 10000000+ (negate rules) and
# 1000000000+; GUI-made rules use ``(int)microtime(true)`` (> 1e9).
_RIDENTIFIER_RE = re.compile(r"^[1-9][0-9]{8}$")
_PORT_RE = re.compile(r"^([0-9]{1,5})(?:-([0-9]{1,5}))?$")
_DESCR_RE = re.compile(r"^[A-Za-z0-9 _.,:/@()-]{1,200}$")

_RULE_TYPES = ("block", "pass", "match")
_IPPROTOCOLS = ("inet", "inet6", "inet46")
_PROTOCOLS = ("any", "tcp", "udp", "tcp/udp", "icmp")
_PORT_PROTOCOLS = ("tcp", "udp", "tcp/udp")
_DIRECTIONS = ("any", "in", "out")
_POSITIONS = ("top", "bottom")

_MGMT_FLOW_COMMAND = 'echo "$SSH_CONNECTION"'

# The fields that define what a rule does. Two rules with equal values here
# behave the same; the add compares them for its idempotency check.
_SPEC_KEYS = (
    "type",
    "interface",
    "floating",
    "direction",
    "ipprotocol",
    "protocol",
    "source",
    "source_port",
    "destination",
    "destination_port",
    "quick",
    "log",
)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_ridentifier(value: Any) -> str:
    """Return *value* if it is a valid MEHO rule id, else raise."""
    if not isinstance(value, str) or not _RIDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            "ridentifier must be a 9-digit number from 100000000 to 999999999 "
            f"(a range pfSense never assigns itself); got {value!r}"
        )
    return value


def _choice(params: dict[str, Any], key: str, allowed: tuple[str, ...], default: str) -> str:
    value = params.get(key, default)
    if value not in allowed:
        raise ValueError(f"{key} must be one of {list(allowed)}; got {value!r}")
    return str(value)


def _normalize_address(value: Any, field: str) -> str:
    """Return ``any``, a bare IP (single host) or a canonical CIDR."""
    if value == "any":
        return "any"
    if not isinstance(value, str):
        raise ValueError(f"{field} must be 'any', an IP address or a CIDR; got {value!r}")
    # pfSense rule addresses never carry an IPv6 zone (``%eth0``) or spaces.
    if "%" in value or any(ch.isspace() for ch in value):
        raise ValueError(
            f"{field} must be 'any', an IP address or a CIDR without a zone or "
            f"spaces; got {value!r}"
        )
    try:
        net = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise ValueError(
            f"{field} must be 'any', an IP address or a CIDR (aliases are not "
            f"supported); got {value!r}"
        ) from exc
    if net.prefixlen == net.max_prefixlen:
        return str(net.network_address)
    return str(net)


def _normalize_port(value: Any, field: str) -> str | None:
    """Return ``None``, ``"N"`` or ``"A-B"`` (the pfSense ``<port>`` form)."""
    if value is None:
        return None
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    match = _PORT_RE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise ValueError(f"{field} must be a port 'N' or a range 'A-B'; got {value!r}")
    low = int(match.group(1))
    high = int(match.group(2)) if match.group(2) else low
    if not (1 <= low <= high <= 65535):
        raise ValueError(f"{field} must be within 1-65535 with A <= B; got {value!r}")
    return str(low) if low == high else f"{low}-{high}"


def _address_version(address: str) -> int | None:
    if address == "any":
        return None
    return ipaddress.ip_network(address, strict=False).version


def _check_family(ipprotocol: str, source: str, destination: str) -> None:
    for field, address in (("source", source), ("destination", destination)):
        version = _address_version(address)
        if version is None:
            continue
        if ipprotocol == "inet46":
            raise ValueError(
                f"{field} must be 'any' when ipprotocol is 'inet46' (pfSense "
                "rejects an IP address on a rule for both families)"
            )
        if (ipprotocol == "inet") != (version == 4):
            raise ValueError(f"{field} {address!r} does not match ipprotocol {ipprotocol!r}")


def _validate_placement(params: dict[str, Any], rule_type: str) -> dict[str, Any]:
    """Validate interface / floating / direction / quick; return those fields."""
    floating = params.get("floating", False)
    if not isinstance(floating, bool):
        raise ValueError(f"floating must be a boolean; got {floating!r}")
    interface = params.get("interface")
    if not isinstance(interface, str) or not _INTERFACE_RE.fullmatch(interface):
        raise ValueError(
            "interface must be a pfSense logical interface handle ('wan', 'lan', "
            f"'opt1'; or 'any' for a floating rule); got {interface!r}"
        )
    quick = params.get("quick", True)
    if not isinstance(quick, bool):
        raise ValueError(f"quick must be a boolean; got {quick!r}")
    if floating:
        return {
            "interface": interface,
            "floating": True,
            "direction": _choice(params, "direction", _DIRECTIONS, "any"),
            "quick": quick,
        }
    if interface == "any":
        raise ValueError("interface 'any' is only valid for a floating rule")
    if rule_type == "match":
        raise ValueError("type 'match' is only valid for a floating rule")
    if "direction" in params:
        raise ValueError("direction is only valid for a floating rule")
    if not quick:
        raise ValueError(
            "quick=false is only valid for a floating rule (interface rules always "
            "stop at the first match)"
        )
    return {"interface": interface, "floating": False, "direction": None, "quick": True}


def _validate_rule_params(params: dict[str, Any]) -> dict[str, Any]:
    """Validate the add params; return the normalised rule spec."""
    rule_type = _choice(params, "type", _RULE_TYPES, "")
    placement = _validate_placement(params, rule_type)
    ipprotocol = _choice(params, "ipprotocol", _IPPROTOCOLS, "inet")
    protocol = _choice(params, "protocol", _PROTOCOLS, "any")
    source = _normalize_address(params.get("source", "any"), "source")
    destination = _normalize_address(params.get("destination", "any"), "destination")
    _check_family(ipprotocol, source, destination)
    source_port = _normalize_port(params.get("source_port"), "source_port")
    destination_port = _normalize_port(params.get("destination_port"), "destination_port")
    if (source_port or destination_port) and protocol not in _PORT_PROTOCOLS:
        raise ValueError(f"ports need protocol tcp, udp or tcp/udp; got protocol {protocol!r}")
    log = params.get("log", False)
    if not isinstance(log, bool):
        raise ValueError(f"log must be a boolean; got {log!r}")
    return {
        "type": rule_type,
        **placement,
        "ipprotocol": ipprotocol,
        "protocol": protocol,
        "source": source,
        "source_port": source_port,
        "destination": destination,
        "destination_port": destination_port,
        "log": log,
    }


def _validate_descr(value: Any, ridentifier: str) -> str:
    if value is None:
        return f"meho: filter rule {ridentifier}"
    if not isinstance(value, str) or not _DESCR_RE.fullmatch(value):
        raise ValueError(
            "descr must match ^[A-Za-z0-9 _.,:/@()-]{1,200}$ (letters, digits, "
            f"space and _ . , : / @ ( ) -); got {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# config.xml parsing
# ---------------------------------------------------------------------------


def _parse_endpoint(element: Any) -> dict[str, Any]:
    """Read a ``<source>`` / ``<destination>`` block.

    ``kind`` is ``any`` / ``address`` (IP, CIDR or alias) / ``network``
    (pfSense keyword such as ``lan`` or ``(self)``).
    """
    kind: str
    value: str | None
    if element is None or element.find("any") is not None:
        kind, value = "any", "any"
    elif _child_text(element, "network") is not None:
        kind, value = "network", _child_text(element, "network")
    elif _child_text(element, "address") is not None:
        kind, value = "address", _child_text(element, "address")
    else:
        kind, value = "any", "any"
    return {
        "kind": kind,
        "value": value,
        "port": _child_text(element, "port") if element is not None else None,
        "not": element is not None and element.find("not") is not None,
    }


def _comparable_address(endpoint: dict[str, Any]) -> str:
    """Return the endpoint in the add op's normalised form, when possible."""
    if endpoint["kind"] == "any":
        return "any"
    value = endpoint["value"] or ""
    if endpoint["kind"] == "address":
        try:
            return _normalize_address(value, "address")
        except ValueError:
            return value
    return f"network:{value}"


def parse_filter_rules_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse the ``<filter><rule>`` list from ``/cf/conf/config.xml``.

    Returns one dict per rule in document order with the rule's ``tracker``
    (the ``ridentifier``), what it does (the ``_SPEC_KEYS`` fields, in the
    same form :func:`pfsense_filter_rule_add` uses), plus ``source_kind`` /
    ``destination_kind``, ``source_not`` / ``destination_not``,
    ``disabled``, ``associated_rule_id``, ``descr`` and the informational
    1-based ``position``. Returns an empty list on a parse failure.

    >>> xml = (
    ...     "<pfsense><filter><rule><tracker>100000001</tracker><type>block</type>"
    ...     "<interface>lan</interface><ipprotocol>inet</ipprotocol>"
    ...     "<source><address>10.1.0.0/24</address></source>"
    ...     "<destination><any></any></destination></rule></filter></pfsense>"
    ... )
    >>> row = parse_filter_rules_xml(xml)[0]
    >>> (row["tracker"], row["type"], row["source"], row["destination"])
    ('100000001', 'block', '10.1.0.0/24', 'any')
    """
    if not xml_text.strip():
        return []
    try:
        root = fromstring(xml_text)
    except ParseError:
        return []
    filter_root = root if root.tag == "filter" else root.find("filter")
    if filter_root is None:
        return []
    rows: list[dict[str, Any]] = []
    for position, item in enumerate(filter_root.findall("rule"), start=1):
        source = _parse_endpoint(item.find("source"))
        destination = _parse_endpoint(item.find("destination"))
        floating = item.find("floating") is not None
        rows.append(
            {
                "tracker": _child_text(item, "tracker"),
                "type": _child_text(item, "type") or "pass",
                "interface": _child_text(item, "interface"),
                "floating": floating,
                "direction": (_child_text(item, "direction") or "any") if floating else None,
                "ipprotocol": _child_text(item, "ipprotocol") or "inet",
                "protocol": _child_text(item, "protocol") or "any",
                "source": _comparable_address(source),
                "source_port": source["port"],
                "destination": _comparable_address(destination),
                "destination_port": destination["port"],
                "quick": (item.find("quick") is not None) if floating else True,
                "log": item.find("log") is not None,
                "source_kind": source["kind"],
                "destination_kind": destination["kind"],
                "source_not": source["not"],
                "destination_not": destination["not"],
                "disabled": item.find("disabled") is not None,
                "associated_rule_id": _child_text(item, "associated-rule-id"),
                "descr": _child_text(item, "descr"),
                "position": position,
            }
        )
    return rows


def _interface_known(xml_text: str, interface: str) -> bool:
    """Whether *interface* is an ``<interfaces>`` handle or an interface group."""
    try:
        root = fromstring(xml_text)
    except ParseError:
        return False
    ifaces = root.find("interfaces")
    if ifaces is not None and ifaces.find(interface) is not None:
        return True
    groups = root.find("ifgroups")
    if groups is None:
        return False
    return any(_child_text(g, "ifname") == interface for g in groups.findall("ifgroupentry"))


# ---------------------------------------------------------------------------
# Management-path guard
# ---------------------------------------------------------------------------


def parse_ssh_connection(text: str) -> dict[str, Any] | None:
    """Parse ``$SSH_CONNECTION`` (``client_ip client_port server_ip server_port``).

    Returns ``{client_ip, server_ip, server_port}`` (IPs as
    :mod:`ipaddress` objects) or ``None`` when the text is not that shape.

    >>> flow = parse_ssh_connection("192.0.2.50 51000 192.0.2.1 22")
    >>> (str(flow["client_ip"]), str(flow["server_ip"]), flow["server_port"])
    ('192.0.2.50', '192.0.2.1', 22)
    """
    parts = text.split()
    if len(parts) != 4:
        return None
    try:
        client_ip = ipaddress.ip_address(parts[0])
        server_ip = ipaddress.ip_address(parts[2])
        server_port = int(parts[3])
    except ValueError:
        return None
    if client_ip.version != server_ip.version or not 1 <= server_port <= 65535:
        return None
    return {"client_ip": client_ip, "server_ip": server_ip, "server_port": server_port}


def _address_covers(address: str, negated: bool, ip: Any) -> bool:
    """Whether a rule endpoint could match *ip* (unknown forms count as a match)."""
    if address == "any" or negated:
        return True
    try:
        net = ipaddress.ip_network(address, strict=False)
    except ValueError:
        return True
    return bool(net.version == ip.version and ip in net)


def _port_covers(port: str | None, value: int) -> bool:
    """Whether a rule ``<port>`` could match *value* (aliases count as a match)."""
    if not port:
        return True
    match = _PORT_RE.fullmatch(port)
    if match is None:
        return True
    low = int(match.group(1))
    high = int(match.group(2)) if match.group(2) else low
    return low <= value <= high


def rule_matches_mgmt_flow(rule: dict[str, Any], flow: dict[str, Any]) -> bool:
    """Whether *rule* could match the SSH flow MEHO uses to reach the firewall.

    Over-matches on purpose: interface and direction are ignored, a source
    port is ignored (the client port changes per session), and an alias,
    keyword or negated address counts as a match.
    """
    family = "inet" if flow["server_ip"].version == 4 else "inet6"
    if rule.get("ipprotocol") not in (family, "inet46"):
        return False
    if rule.get("protocol") not in ("any", "tcp", "tcp/udp"):
        return False
    if not _address_covers(
        str(rule.get("source")), bool(rule.get("source_not")), flow["client_ip"]
    ):
        return False
    if not _address_covers(
        str(rule.get("destination")), bool(rule.get("destination_not")), flow["server_ip"]
    ):
        return False
    return _port_covers(rule.get("destination_port"), flow["server_port"])


async def _read_mgmt_flow(
    self: PfSenseConnector, target: Any, operator: Operator | None
) -> dict[str, Any] | None:
    """Read this SSH session's own flow; ``None`` when it cannot be read."""
    try:
        proc = await self._run_command(target, _MGMT_FLOW_COMMAND, operator=operator)
    except Exception:
        # Fail closed: an unreadable flow becomes a ``mgmt_path_unknown`` refusal.
        return None
    stdout = getattr(proc, "stdout", "") or ""
    if proc.exit_status != 0 or not isinstance(stdout, str):
        return None
    return parse_ssh_connection(stdout.strip())


async def _mgmt_guard(
    self: PfSenseConnector, target: Any, operator: Operator | None, rule: dict[str, Any]
) -> tuple[str, str] | None:
    """Return ``(status, guidance)`` when the management-path guard refuses."""
    flow = await _read_mgmt_flow(self, target, operator)
    if flow is None:
        return (
            "mgmt_path_unknown",
            "could not read this SSH session's own flow ($SSH_CONNECTION), so MEHO "
            "cannot prove the change keeps its management path open; refusing",
        )
    if rule_matches_mgmt_flow(rule, flow):
        return (
            "mgmt_path",
            "this change could cut the SSH path MEHO uses to manage this firewall "
            f"(tcp to port {flow['server_port']}); refusing. Narrow the source, "
            "destination or destination port so the rule cannot match that path",
        )
    return None


# ---------------------------------------------------------------------------
# pfSsh.php playback fragments
# ---------------------------------------------------------------------------


def _php_endpoint(var: str, address: str, port: str | None) -> list[str]:
    lines = [f"{var} = array();"]
    if address == "any":
        lines.append(f"{var}['any'] = true;")
    else:
        lines.append(f"{var}['address'] = {_php_squote(address)};")
    if port:
        lines.append(f"{var}['port'] = {_php_squote(port)};")
    return lines


def _build_rule_add_playback(
    ridentifier: str, spec: dict[str, Any], descr: str, position: str
) -> str:
    """Return the PHP fragment that inserts ONE filter rule.

    Inserts only when no rule carries the tracker yet (``$meho_found ===
    0``), at the top or bottom of the rule's interface section (as the GUI
    does, with its separator shift), then ``filter_rules_sort()``,
    ``write_config()`` and ``filter_configure()``.
    """
    section = "FloatingRules" if spec["floating"] else spec["interface"]
    lines = [
        "global $config;",
        f"$meho_tracker = {_php_squote(ridentifier)};",
        "$meho_found = 0;",
        "foreach (config_get_path('filter/rule', []) as $meho_r) {",
        "  if (isset($meho_r['tracker']) && (string)$meho_r['tracker'] === $meho_tracker) {",
        "    $meho_found++;",
        "  }",
        "}",
        "if ($meho_found === 0) {",
        "  $meho_rule = array();",
        "  $meho_rule['tracker'] = $meho_tracker;",
        f"  $meho_rule['type'] = {_php_squote(spec['type'])};",
        f"  $meho_rule['interface'] = {_php_squote(spec['interface'])};",
        f"  $meho_rule['ipprotocol'] = {_php_squote(spec['ipprotocol'])};",
    ]
    if spec["protocol"] != "any":
        lines.append(f"  $meho_rule['protocol'] = {_php_squote(spec['protocol'])};")
    for var, address, port in (
        ("$meho_src", spec["source"], spec["source_port"]),
        ("$meho_dst", spec["destination"], spec["destination_port"]),
    ):
        lines += ["  " + line for line in _php_endpoint(var, address, port)]
    lines += [
        "  $meho_rule['source'] = $meho_src;",
        "  $meho_rule['destination'] = $meho_dst;",
    ]
    if spec["floating"]:
        lines += [
            "  $meho_rule['floating'] = 'yes';",
            f"  $meho_rule['direction'] = {_php_squote(spec['direction'])};",
        ]
        if spec["quick"]:
            lines.append("  $meho_rule['quick'] = 'yes';")
    if spec["log"]:
        lines.append("  $meho_rule['log'] = true;")
    lines += [
        f"  $meho_rule['descr'] = {_php_squote(descr)};",
        "  $meho_rules = config_get_path('filter/rule', []);",
        f"  $meho_section = {_php_squote(section)};",
        "  $meho_sep_path = 'filter/separator/' . strtolower($meho_section);",
        "  $meho_idx = get_interface_ruleindex($meho_section);",
    ]
    if position == "top":
        lines += [
            "  if ($meho_idx['first'] !== null) {",
            "    array_splice($meho_rules, $meho_idx['first'], 0, array($meho_rule));",
            "    $meho_seps = config_get_path($meho_sep_path, []);",
            "    shift_separators($meho_seps, -1);",
            "    config_set_path($meho_sep_path, $meho_seps);",
            "  } else {",
            "    $meho_rules[] = $meho_rule;",
            "  }",
        ]
    else:
        lines += [
            "  if ($meho_idx['last'] !== null) {",
            "    array_splice($meho_rules, $meho_idx['last'] + 1, 0, array($meho_rule));",
            "  } else {",
            "    $meho_rules[] = $meho_rule;",
            "  }",
        ]
    lines += [
        "  config_set_path('filter/rule', $meho_rules);",
        "  filter_rules_sort();",
        f"  write_config({_php_squote('meho: add filter rule ' + ridentifier)});",
        "  filter_configure();",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _build_rule_delete_playback(ridentifier: str) -> str:
    """Return the PHP fragment that deletes ONE filter rule by tracker.

    Persists only when exactly one rule carries the tracker
    (``$meho_found === 1``). Shifts that section's separators up first (as
    the GUI delete does), then removes the rule, reindexes,
    ``write_config()`` and ``filter_configure()``.
    """
    lines = [
        "global $config;",
        f"$meho_tracker = {_php_squote(ridentifier)};",
        "$meho_rules = config_get_path('filter/rule', []);",
        "$meho_found = 0;",
        "$meho_at = null;",
        "foreach ($meho_rules as $meho_i => $meho_r) {",
        "  if (isset($meho_r['tracker']) && (string)$meho_r['tracker'] === $meho_tracker) {",
        "    $meho_found++;",
        "    $meho_at = $meho_i;",
        "  }",
        "}",
        "if ($meho_found === 1) {",
        "  $meho_r = $meho_rules[$meho_at];",
        "  $meho_section = isset($meho_r['floating']) ? 'FloatingRules' : $meho_r['interface'];",
        "  $meho_sep_path = 'filter/separator/' . strtolower($meho_section);",
        "  $meho_idx = get_interface_ruleindex($meho_section, $meho_at);",
        "  $meho_seps = config_get_path($meho_sep_path, []);",
        "  shift_separators($meho_seps, $meho_idx['index'], true);",
        "  config_set_path($meho_sep_path, $meho_seps);",
        "  unset($meho_rules[$meho_at]);",
        "  config_set_path('filter/rule', array_values($meho_rules));",
        f"  write_config({_php_squote('meho: delete filter rule ' + ridentifier)});",
        "  filter_configure();",
        "}",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _spec_of(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in _SPEC_KEYS}


async def pfsense_filter_rule_add(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.filter.rule.add`` -- add ONE filter rule.

    Sequence: validate -> read ``config.xml`` -> by ``ridentifier``: 1 match
    with the same rule -> ``already_present`` (no change); 1 match that
    differs -> ``conflict``; >1 -> ``ambiguous`` -> unknown interface ->
    ``unknown_interface`` -> a ``block`` rule that could match MEHO's own
    SSH path -> ``mgmt_path`` / ``mgmt_path_unknown`` -> otherwise stage the
    insert, then read back and check the rule is there exactly once and the
    rule count grew by exactly one.
    """
    ridentifier = _validate_ridentifier(params.get("ridentifier"))
    spec = _validate_rule_params(params)
    descr = _validate_descr(params.get("descr"), ridentifier)
    position = _choice(params, "position", _POSITIONS, "top")

    before = await _read_config_xml(self, target, operator)
    rules_before = parse_filter_rules_xml(before)
    matches = [r for r in rules_before if r.get("tracker") == ridentifier]
    result: dict[str, Any] = {
        "op_class": "write",
        "resource": "filter_rule",
        "ridentifier": ridentifier,
        "rule": {**spec, "descr": descr},
        "position": position,
        "applied": False,
        "existing": None,
        "rules_before": len(rules_before),
        "rules_after": None,
        "verified": False,
        "guidance": None,
    }
    if len(matches) > 1:
        return {
            **result,
            "status": "ambiguous",
            "guidance": (
                f"{len(matches)} filter rules share ridentifier {ridentifier!r} "
                "(corrupt config); refusing -- an operator must fix the duplicates first"
            ),
        }
    if matches:
        same = _spec_of(matches[0]) == spec
        return {
            **result,
            "status": "already_present" if same else "conflict",
            "existing": matches[0],
            "guidance": None
            if same
            else (
                f"a different filter rule already uses ridentifier {ridentifier!r}; "
                "nothing changed -- pick another ridentifier or delete that rule first"
            ),
        }
    if spec["interface"] != "any" and not _interface_known(before, spec["interface"]):
        return {
            **result,
            "status": "unknown_interface",
            "guidance": (
                f"interface {spec['interface']!r} is not in config.xml <interfaces> "
                "or <ifgroups>; nothing changed"
            ),
        }
    if spec["type"] == "block":
        refusal = await _mgmt_guard(self, target, operator, spec)
        if refusal is not None:
            return {**result, "status": refusal[0], "guidance": refusal[1]}

    await _apply_playback(
        self,
        target,
        script_name=f"meho_filter_rule_add_{ridentifier}",
        script_body=_build_rule_add_playback(ridentifier, spec, descr, position),
        operator=operator,
    )

    rules_after = parse_filter_rules_xml(await _read_config_xml(self, target, operator))
    landed = [r for r in rules_after if r.get("tracker") == ridentifier]
    if len(landed) != 1 or len(rules_after) != len(rules_before) + 1:
        raise RuntimeError(
            f"filter.rule.add verification failed for ridentifier {ridentifier!r}: "
            f"present_after={len(landed)}, rules {len(rules_before)}->{len(rules_after)} "
            "(expected exactly one more); the pfSsh.php playback did not persist a "
            "clean single-rule add"
        )
    return {
        **result,
        "status": "added",
        "applied": True,
        "rules_after": len(rules_after),
        "verified": True,
    }


async def pfsense_filter_rule_delete(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.filter.rule.delete`` -- governed single-rule delete.

    Sequence: validate -> read ``config.xml`` -> by ``ridentifier``: 0 ->
    ``not_found``, >1 -> ``ambiguous`` -> a NAT-linked rule ->
    ``nat_associated`` -> an enabled ``pass`` rule that could carry MEHO's
    own SSH path -> ``mgmt_path`` / ``mgmt_path_unknown`` -> otherwise stage
    the delete, then read back and check the rule is gone and the count
    dropped by exactly one.
    """
    ridentifier = _validate_ridentifier(params.get("ridentifier"))

    before = await _read_config_xml(self, target, operator)
    rules_before = parse_filter_rules_xml(before)
    matches = [r for r in rules_before if r.get("tracker") == ridentifier]
    result: dict[str, Any] = {
        "op_class": "delete",
        "resource": "filter_rule",
        "ridentifier": ridentifier,
        "matched": len(matches),
        "removed": None,
        "rules_before": len(rules_before),
        "rules_after": None,
        "verified": False,
        "guidance": None,
    }
    if not matches:
        return {
            **result,
            "status": "not_found",
            "guidance": f"no filter rule with ridentifier {ridentifier!r}; nothing deleted",
        }
    if len(matches) > 1:
        return {
            **result,
            "status": "ambiguous",
            "guidance": (
                f"{len(matches)} filter rules share ridentifier {ridentifier!r} "
                "(corrupt config); refusing to delete"
            ),
        }
    rule = matches[0]
    if rule.get("associated_rule_id"):
        return {
            **result,
            "status": "nat_associated",
            "guidance": (
                "this filter rule belongs to a NAT port forward "
                f"({rule['associated_rule_id']}); refusing -- remove it with the NAT rule"
            ),
        }
    if rule.get("type") == "pass" and not rule.get("disabled"):
        refusal = await _mgmt_guard(self, target, operator, rule)
        if refusal is not None:
            return {**result, "status": refusal[0], "guidance": refusal[1]}

    await _apply_playback(
        self,
        target,
        script_name=f"meho_filter_rule_delete_{ridentifier}",
        script_body=_build_rule_delete_playback(ridentifier),
        operator=operator,
    )

    rules_after = parse_filter_rules_xml(await _read_config_xml(self, target, operator))
    still_present = any(r.get("tracker") == ridentifier for r in rules_after)
    if still_present or len(rules_after) != len(rules_before) - 1:
        raise RuntimeError(
            f"filter.rule.delete verification failed for ridentifier {ridentifier!r}: "
            f"present_after={still_present}, rules {len(rules_before)}->{len(rules_after)} "
            "(expected exactly one fewer); the pfSsh.php playback did not persist a "
            "clean single-rule delete"
        )
    return {
        **result,
        "status": "deleted",
        "removed": rule,
        "rules_after": len(rules_after),
        "verified": True,
    }


# ---------------------------------------------------------------------------
# Blast-radius preview (#3197 mandatory destructive-tier block)
# ---------------------------------------------------------------------------


async def _filter_rule_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast-radius builder for ``pfsense.filter.rule.delete``.

    Live-reads ``config.xml`` and, when the ``ridentifier`` resolves to
    exactly one rule, names that rule (what it does, where it sits) so the
    approver reads exactly what is removed. Declines (``None`` -> the park
    is refused ``blast_radius_required``) when the config cannot be read or
    the id does not resolve to one rule.
    """
    ridentifier = ctx.params.get("ridentifier")
    if not isinstance(ridentifier, str) or not _RIDENTIFIER_RE.fullmatch(ridentifier):
        return None
    if ctx.connector_instance is None:
        return None
    try:
        xml_text = await _read_config_xml(
            ctx.connector_instance,  # type: ignore[arg-type]  # PfSenseConnector at runtime
            ctx.target,
            ctx.operator,
        )
    except Exception:
        # Fail-soft: a read fault declines the preview -> park refused (fail-closed).
        return None
    matches = [r for r in parse_filter_rules_xml(xml_text) if r.get("tracker") == ridentifier]
    if len(matches) != 1:
        return None
    rule = matches[0]
    return {
        "blast_radius": {
            "object": {
                "kind": "filter_rule",
                "ridentifier": ridentifier,
                **_spec_of(rule),
                "disabled": rule.get("disabled"),
                "descr": rule.get("descr"),
                "position": rule.get("position"),
            },
            "children": [],
            "irreversibility": "permanent",
        },
    }


def register_pfsense_filter_rule_preview_builders() -> None:
    """Wire the delete's blast-radius builder. Import-time, idempotent."""
    register_preview_builder("pfsense.filter.rule.delete", _filter_rule_delete_preview)


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_RIDENTIFIER_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": "^[1-9][0-9]{8}$",
    "description": (
        "The rule's stable id: a 9-digit number from 100000000 to 999999999 "
        "(a range pfSense never assigns itself). Stored as the rule's "
        "``<tracker>``; pf logs show it as ``ridentifier``."
    ),
}

_PORT_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": "^[0-9]{1,5}(-[0-9]{1,5})?$",
}

_ADDRESS_SCHEMA: dict[str, Any] = {"type": "string", "minLength": 1, "default": "any"}

_RULE_ADD_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ridentifier": _RIDENTIFIER_SCHEMA,
        "type": {"type": "string", "enum": list(_RULE_TYPES)},
        "interface": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_]{1,32}$",
            "description": (
                "pfSense logical interface handle (``lan``, ``opt1``) or interface "
                "group. A floating rule may use ``any``."
            ),
        },
        "floating": {"type": "boolean", "default": False},
        "direction": {
            "type": "string",
            "enum": list(_DIRECTIONS),
            "description": "Floating rules only. Default ``any``.",
        },
        "quick": {
            "type": "boolean",
            "default": True,
            "description": (
                "Floating rules only: stop at the first match. Interface rules always do."
            ),
        },
        "ipprotocol": {"type": "string", "enum": list(_IPPROTOCOLS), "default": "inet"},
        "protocol": {"type": "string", "enum": list(_PROTOCOLS), "default": "any"},
        "source": {
            **_ADDRESS_SCHEMA,
            "description": "``any``, an IP address or a CIDR. Aliases are not supported.",
        },
        "source_port": {**_PORT_SCHEMA, "description": "Port ``N`` or range ``A-B``."},
        "destination": {
            **_ADDRESS_SCHEMA,
            "description": "``any``, an IP address or a CIDR. Aliases are not supported.",
        },
        "destination_port": {**_PORT_SCHEMA, "description": "Port ``N`` or range ``A-B``."},
        "log": {"type": "boolean", "default": False},
        "descr": {"type": "string", "pattern": "^[A-Za-z0-9 _.,:/@()-]{1,200}$"},
        "position": {
            "type": "string",
            "enum": list(_POSITIONS),
            "default": "top",
            "description": (
                "Where the rule goes among its interface's rules (or the floating "
                "rules). The first match wins, so a block rule usually goes on top."
            ),
        },
    },
    "required": ["ridentifier", "type", "interface"],
    "additionalProperties": False,
}

_RULE_ADD_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op_class": {"type": "string", "enum": ["write"]},
        "resource": {"type": "string", "enum": ["filter_rule"]},
        "status": {
            "type": "string",
            "enum": [
                "added",
                "already_present",
                "conflict",
                "ambiguous",
                "unknown_interface",
                "mgmt_path",
                "mgmt_path_unknown",
            ],
        },
        "ridentifier": {"type": "string"},
        "rule": {"type": "object"},
        "position": {"type": "string", "enum": list(_POSITIONS)},
        "applied": {"type": "boolean"},
        "existing": {"type": ["object", "null"]},
        "rules_before": {"type": ["integer", "null"]},
        "rules_after": {"type": ["integer", "null"]},
        "verified": {"type": "boolean"},
        "guidance": {"type": ["string", "null"]},
    },
    "required": ["op_class", "resource", "status", "ridentifier", "applied", "verified"],
    "additionalProperties": False,
}

_RULE_ADD_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Add ONE pfSense firewall (filter) rule -- for example a block rule that "
        "fences one network off from another. safety_level=dangerous: a human "
        "approves every add, because a firewall rule changes what traffic the "
        "whole firewall lets through. Pick a 9-digit ``ridentifier`` and keep it: "
        "re-running the same add is a no-op (``already_present``), and "
        "``pfsense.filter.rule.delete`` removes the rule by that id. A ``block`` "
        "rule that could match the SSH path MEHO uses to manage the firewall is "
        "refused (``mgmt_path``). The first matching rule wins, so a block rule "
        "usually needs ``position: top``. Read the current rules first with "
        "``pfsense.firewall.rules``."
    ),
    "parameter_hints": {
        "ridentifier": "Required. 9-digit id, 100000000-999999999.",
        "type": "Required. ``block``, ``pass`` or ``match`` (``match`` is floating only).",
        "interface": "Required. Logical interface (``lan``, ``opt1``); ``any`` if floating.",
        "source": "Optional. ``any`` (default), an IP or a CIDR.",
        "destination": "Optional. ``any`` (default), an IP or a CIDR.",
        "destination_port": "Optional. ``N`` or ``A-B``; needs protocol tcp/udp/tcp/udp.",
        "position": "Optional. ``top`` (default) or ``bottom``.",
    },
    "output_shape": (
        "``{op_class: 'write', resource: 'filter_rule', status, ridentifier, rule, "
        "position, applied, existing, rules_before, rules_after, verified, "
        "guidance}``. ``status`` is ``added`` (``applied`` and ``verified`` true), "
        "``already_present`` (same rule exists, no change), or a refusal: "
        "``conflict`` / ``ambiguous`` / ``unknown_interface`` / ``mgmt_path`` / "
        "``mgmt_path_unknown`` (no change, ``guidance`` says why)."
    ),
}

_RULE_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ridentifier": _RIDENTIFIER_SCHEMA},
    "required": ["ridentifier"],
    "additionalProperties": False,
}

_RULE_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op_class": {"type": "string", "enum": ["delete"]},
        "resource": {"type": "string", "enum": ["filter_rule"]},
        "status": {
            "type": "string",
            "enum": [
                "deleted",
                "not_found",
                "ambiguous",
                "nat_associated",
                "mgmt_path",
                "mgmt_path_unknown",
            ],
        },
        "ridentifier": {"type": "string"},
        "matched": {"type": "integer"},
        "removed": {"type": ["object", "null"]},
        "rules_before": {"type": ["integer", "null"]},
        "rules_after": {"type": ["integer", "null"]},
        "verified": {"type": "boolean"},
        "guidance": {"type": ["string", "null"]},
    },
    "required": ["op_class", "resource", "status", "ridentifier", "matched", "verified"],
    "additionalProperties": False,
}

_RULE_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Permanently delete ONE pfSense filter rule by its ``ridentifier`` -- the "
        "inverse of ``pfsense.filter.rule.add``. GOVERNED DESTRUCTIVE DELETE "
        "(safety_level=destructive): mandatory human approval, a preview-hash "
        "binding, and a blast-radius statement naming the rule. Refuses "
        "``not_found`` / ``ambiguous``, a rule that belongs to a NAT port forward "
        "(``nat_associated``), and an enabled pass rule that could carry the SSH "
        "path MEHO uses to manage the firewall (``mgmt_path``). Deletes exactly "
        "one rule and checks the count dropped by one."
    ),
    "parameter_hints": {"ridentifier": "Required. The 9-digit id used at add time."},
    "output_shape": (
        "``{op_class: 'delete', resource: 'filter_rule', status, ridentifier, "
        "matched, removed, rules_before, rules_after, verified, guidance}``. On "
        "``deleted``, ``removed`` is the deleted rule and ``verified`` is true."
    ),
}

#: The filter-rule ops. The add is ``dangerous`` + ``requires_approval``; the
#: delete is on the governed-delete tier like ``pfsense.nat.delete``.
FILTER_RULE_OPS: tuple[PfSenseOp, ...] = (
    PfSenseOp(
        op_id="pfsense.filter.rule.add",
        handler_attr="filter_rule_add",
        summary="Add ONE firewall filter rule with a stable ridentifier (approval required).",
        description=(
            "Adds one pfSense filter rule (``config.xml`` ``<filter><rule>``): "
            "block / pass / match, on one interface or floating with a direction, "
            "source / destination as any / IP / CIDR with optional ports, quick "
            "(floating), optional log. The caller-chosen ``ridentifier`` is stored "
            "as the rule's ``<tracker>`` (pf ``ridentifier``). Re-adding the same "
            "rule is a no-op; a different rule with that id is refused. A block "
            "rule that could match MEHO's own SSH management path is refused. "
            "Placed at the top (default) or bottom of its interface's rules, then "
            "``write_config()`` + ``filter_configure()`` and a read-back check. "
            "safety_level=dangerous with mandatory approval."
        ),
        parameter_schema=_RULE_ADD_PARAMETER_SCHEMA,
        response_schema=_RULE_ADD_RESPONSE_SCHEMA,
        group_key="firewall",
        tags=("write", "firewall", "filter-rule", "pfsense"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_RULE_ADD_LLM_INSTRUCTIONS,
    ),
    PfSenseOp(
        op_id="pfsense.filter.rule.delete",
        handler_attr="filter_rule_delete",
        summary="Permanently delete ONE firewall filter rule by ridentifier (governed).",
        description=(
            "Governed destructive delete of one pfSense filter rule by its "
            "``ridentifier`` (``<tracker>``) -- the inverse of "
            "``pfsense.filter.rule.add``. safety_level=destructive (same tier and "
            "gate as ``pfsense.nat.delete``): mandatory human approval, a "
            "preview-hash binding, and a blast-radius statement naming the rule. "
            "Fail-closed: ``not_found``, ``ambiguous``, ``nat_associated`` (a rule "
            "owned by a NAT port forward), and ``mgmt_path`` (an enabled pass rule "
            "that could carry MEHO's own SSH management path). Deletes exactly one "
            "rule via ``pfSsh.php playback`` (``write_config`` + "
            "``filter_configure``) and checks it is gone and the count dropped by one."
        ),
        parameter_schema=_RULE_DELETE_PARAMETER_SCHEMA,
        response_schema=_RULE_DELETE_RESPONSE_SCHEMA,
        group_key="firewall",
        tags=("write", "firewall", "filter-rule", "delete", "destructive", "pfsense"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=_RULE_DELETE_LLM_INSTRUCTIONS,
    ),
)


# Import-time side-effect: register the blast-radius builder (mirrors ops_delete).
register_pfsense_filter_rule_preview_builders()
