# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which firewall rules still let a VPN user's tunnel address in.

``pfsense.openvpn.user.delete`` frees the user's fixed tunnel address for
the next user. A rule that lets that address in must therefore be removed
first. A rule counts when its source (not negated) holds one of the
user's addresses, on any interface tab, also an interface group, a floating
rule and an OpenVPN server assigned as its own interface. A source holds
an address when it is

* the address itself, or a network (CIDR) that contains it;
* an alias (host or network type, also nested) with such an entry;
* an interface network (for example ``lan``) whose static address and
  subnet contain it, or an interface address (``lanip``) that is it.

When MEHO cannot tell what a source holds, the rule counts as a rule that
may let the address in: unknown is not "no rule". That is so for

* an interface network or address of an interface with no static IPv4
  address in ``config.xml`` (no ``ipaddr`` or ``subnet``, ``dhcp``, an
  OpenVPN server assigned as its own interface) and for a keyword this
  code does not know; ``(self)`` (the firewall itself) does not count;
* a URL alias, whose contents ``config.xml`` does not hold;
* a host name in an alias (MEHO does not resolve it), and any other alias
  entry that is neither an address, a network nor a host or network alias.

A user with no address has no rule that lets it in, also not through such
a source. The Python check (:func:`rules_reaching`) and the PHP check of
the fragment (:func:`source_check_php`) follow the same rules.

Checked against the pfSense 2.7.2 sources and the development branch
(2.9.0-DEVELOPMENT): aliases are ``<aliases><alias>`` with ``name``,
``type`` and a space separated ``address``; a rule source is ``any``,
``network`` or ``address`` with an optional ``not``.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from defusedxml.ElementTree import ParseError, fromstring

from meho_backplane.connectors.pfsense.ops_write import _php_squote

__all__ = ["rules_reaching", "source_check_php"]

_LIST_ALIAS_TYPES = ("host", "network")
_SELF = "(self)"
_PREFIX = re.compile(r"[0-9]{1,3}")


def _text(element: Any, tag: str) -> str:
    child = element.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _aliases(root: Any) -> dict[str, list[str]]:
    """Return the entries of each host or network alias, by alias name."""
    entries: dict[str, list[str]] = {}
    section = root.find("aliases")
    for alias in section.findall("alias") if section is not None else []:
        if _text(alias, "type") in _LIST_ALIAS_TYPES:
            entries[_text(alias, "name")] = _text(alias, "address").split()
    return entries


def _network(entry: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """Return *entry* as an address or a ``/<bits>`` network (IPv4 or IPv6), else ``None``."""
    address, slash, bits = entry.strip().partition("/")
    if "%" in address or (slash and not _PREFIX.fullmatch(bits)):
        return None
    try:
        return ipaddress.ip_network(entry.strip(), strict=False)
    except ValueError:
        return None


def _interface_net(root: Any, keyword: str) -> ipaddress.IPv4Network | None:
    """Return the static IPv4 network of ``lan`` / ``lanip`` and the like, else ``None``."""
    only_address = keyword.endswith("ip")
    name = keyword[:-2] if only_address else keyword
    section = root.find("interfaces")
    iface = section.find(name) if section is not None and name else None
    if iface is None:
        return None
    address, subnet = _text(iface, "ipaddr"), _text(iface, "subnet")
    if not re.fullmatch(r"[0-9]{1,2}", subnet) or int(subnet) > 32:
        return None
    try:
        ipaddress.IPv4Address(address)
    except ValueError:
        return None
    return ipaddress.IPv4Network(f"{address}/{32 if only_address else subnet}", strict=False)


def _source_reaches(
    rule: dict[str, Any],
    root: Any,
    addresses: list[ipaddress.IPv4Address],
    aliases: dict[str, list[str]],
) -> bool:
    if rule["source_not"] or rule["source_kind"] == "any":
        return False
    value = str(rule["source"])
    if rule["source_kind"] == "network":
        keyword = value.removeprefix("network:")
        if keyword == _SELF:
            return False
        own = _interface_net(root, keyword)
        return own is None or any(address in own for address in addresses)
    todo, seen = [value], set[str]()
    while todo:
        entry = todo.pop()
        net = _network(entry)
        if net is not None:
            if any(address in net for address in addresses):
                return True
        elif entry not in aliases:
            return True  # a URL alias, a host name or an unknown name: cannot tell
        elif entry not in seen:
            seen.add(entry)
            todo.extend(aliases[entry])
    return False


def rules_reaching(
    xml_text: str, rules: list[dict[str, Any]], addresses: list[str]
) -> list[dict[str, Any]]:
    """Return each rule of *rules* whose source holds one of *addresses*.

    *rules* come from ``parse_filter_rules_xml`` of *xml_text*. Raises
    :class:`RuntimeError` when ``config.xml`` cannot be read again.
    """
    try:
        root = fromstring(xml_text)
    except (ParseError, ValueError) as exc:  # defusedxml's refusals are ValueErrors
        raise RuntimeError(f"openvpn.user.delete: config.xml could not be read: {exc}") from exc
    if root.tag != "pfsense":
        return []
    hosts = [ipaddress.IPv4Address(a) for a in addresses]
    if not hosts:
        return []
    aliases = _aliases(root)
    return [r for r in rules if _source_reaches(r, root, hosts, aliases)]


def _php_list(values: tuple[str, ...]) -> str:
    return ", ".join(_php_squote(v) for v in values)


def _net_check_php() -> list[str]:
    """Return PHP lines that define ``$meho_is_net($entry)``: an address or ``/<bits>`` network."""
    return [
        "$meho_is_net = function ($meho_entry) {",
        "  $meho_parts = explode('/', trim((string)$meho_entry));",
        "  if (count($meho_parts) > 2",
        "      || filter_var($meho_parts[0], FILTER_VALIDATE_IP) === false) {",
        "    return false;",
        "  }",
        "  if (count($meho_parts) === 1) {",
        "    return true;",
        "  }",
        "  $meho_max = strpos($meho_parts[0], ':') === false ? 32 : 128;",
        "  return preg_match('/^[0-9]{1,3}$/', $meho_parts[1]) === 1",
        "      && (int)$meho_parts[1] <= $meho_max;",
        "};",
    ]


def source_check_php() -> list[str]:
    """Return PHP lines that define ``$meho_reach($rule_source)`` for ``$meho_sources``.

    ``$meho_sources`` holds the user's addresses, bare and as ``/32``. The
    closure answers ``true`` when the source holds one of them, by the
    rules in the module text.
    """
    return [
        *_net_check_php(),
        "$meho_ips = array();",
        "foreach ($meho_sources as $meho_s) {",
        "  $meho_l = ip2long(explode('/', $meho_s)[0]);",
        "  if ($meho_l !== false) {",
        "    $meho_ips[$meho_l] = true;",
        "  }",
        "}",
        "$meho_holds = function ($meho_entry) use ($meho_ips) {",
        "  $meho_parts = explode('/', trim((string)$meho_entry));",
        "  $meho_base = ip2long($meho_parts[0]);",
        "  if ($meho_base === false || count($meho_parts) > 2) {",
        "    return false;",
        "  }",
        "  $meho_bits = count($meho_parts) === 2 ? $meho_parts[1] : '32';",
        "  if (!preg_match('/^[0-9]{1,3}$/', $meho_bits) || (int)$meho_bits > 32) {",
        "    return false;",
        "  }",
        "  $meho_mask = (int)$meho_bits === 0 ? 0 : ((-1 << (32 - (int)$meho_bits)) & 0xFFFFFFFF);",
        "  foreach (array_keys($meho_ips) as $meho_ip) {",
        "    if (($meho_ip & $meho_mask) === ($meho_base & $meho_mask)) {",
        "      return true;",
        "    }",
        "  }",
        "  return false;",
        "};",
        "$meho_alias = array();",
        "foreach ((array)($config['aliases']['alias'] ?? array()) as $meho_al) {",
        "  $meho_at = (string)($meho_al['type'] ?? '');",
        f"  if (in_array($meho_at, array({_php_list(_LIST_ALIAS_TYPES)}), true)) {{",
        "    $meho_list = trim((string)($meho_al['address'] ?? ''));",
        "    $meho_an = trim((string)($meho_al['name'] ?? ''));",
        "    $meho_alias[$meho_an] = preg_split('/\\s+/', $meho_list, -1, PREG_SPLIT_NO_EMPTY);",
        "  }",
        "}",
        "$meho_reach = function ($meho_s) use ($meho_ips, $meho_holds, $meho_is_net, $meho_alias,",
        "    $config) {",
        "  if (count($meho_ips) === 0 || !is_array($meho_s) || isset($meho_s['any'])",
        "      || isset($meho_s['not'])) {",
        "    return false;",
        "  }",
        "  if (isset($meho_s['network'])) {",
        "    $meho_k = trim((string)$meho_s['network']);",
        f"    if ($meho_k === {_php_squote(_SELF)}) {{",
        "      return false;",
        "    }",
        "    $meho_only = substr($meho_k, -2) === 'ip';",
        "    if ($meho_only) {",
        "      $meho_k = substr($meho_k, 0, -2);",
        "    }",
        "    $meho_if = $meho_k === '' ? null : ($config['interfaces'][$meho_k] ?? null);",
        "    $meho_a = is_array($meho_if) ? trim((string)($meho_if['ipaddr'] ?? '')) : '';",
        "    $meho_b = is_array($meho_if) ? trim((string)($meho_if['subnet'] ?? '')) : '';",
        "    if (filter_var($meho_a, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4) === false",
        "        || preg_match('/^[0-9]{1,2}$/', $meho_b) !== 1 || (int)$meho_b > 32) {",
        "      return true;",
        "    }",
        "    return $meho_holds($meho_a . '/' . ($meho_only ? '32' : $meho_b));",
        "  }",
        "  if (!isset($meho_s['address'])) {",
        "    return false;",
        "  }",
        "  $meho_todo = array(trim((string)$meho_s['address']));",
        "  $meho_seen = array();",
        "  while (count($meho_todo) > 0) {",
        "    $meho_e = array_pop($meho_todo);",
        "    if ($meho_is_net($meho_e)) {",
        "      if ($meho_holds($meho_e)) {",
        "        return true;",
        "      }",
        "    } elseif (!isset($meho_alias[$meho_e])) {",
        "      return true;",
        "    } elseif (!isset($meho_seen[$meho_e])) {",
        "      $meho_seen[$meho_e] = true;",
        "      foreach ($meho_alias[$meho_e] as $meho_x) {",
        "        $meho_todo[] = $meho_x;",
        "      }",
        "    }",
        "  }",
        "  return false;",
        "};",
    ]
