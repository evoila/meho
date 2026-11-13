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

A URL alias has contents that ``config.xml`` does not hold. A rule with
such a source counts as a rule that may let the address in: unknown is
not "no rule". The Python check (:func:`rules_reaching`) and the PHP
check of the fragment (:func:`source_check_php`) follow the same rules.

Checked against the pfSense 2.8 sources: aliases are ``<aliases><alias>``
with ``name``, ``type`` and a space separated ``address``; a rule source
is ``any``, ``network`` or ``address`` with an optional ``not``.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from xml.etree.ElementTree import ParseError, fromstring

from meho_backplane.connectors.pfsense.ops_write import _php_squote

__all__ = ["rules_reaching", "source_check_php"]

_URL_ALIAS_TYPES = ("url", "urltable")
_LIST_ALIAS_TYPES = ("host", "network")


def _text(element: Any, tag: str) -> str:
    child = element.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _aliases(root: Any) -> tuple[dict[str, list[str]], set[str]]:
    """Return ``(entries by alias name, names of aliases with unknown contents)``."""
    entries: dict[str, list[str]] = {}
    unknown: set[str] = set()
    section = root.find("aliases")
    for alias in section.findall("alias") if section is not None else []:
        name = _text(alias, "name")
        kind = _text(alias, "type")
        if kind in _URL_ALIAS_TYPES:
            unknown.add(name)
        elif kind in _LIST_ALIAS_TYPES:
            entries[name] = _text(alias, "address").split()
    return entries, unknown


def _interface_net(root: Any, keyword: str) -> str | None:
    """Return the static network of an interface keyword (``lan``, ``lanip``), else ``None``."""
    only_address = keyword.endswith("ip")
    name = keyword[:-2] if only_address else keyword
    section = root.find("interfaces")
    iface = section.find(name) if section is not None and name else None
    if iface is None:
        return None
    address, subnet = _text(iface, "ipaddr"), _text(iface, "subnet")
    if not address or not subnet:
        return None
    return f"{address}/32" if only_address else f"{address}/{subnet}"


def _holds(entry: str, addresses: list[ipaddress.IPv4Address]) -> bool:
    try:
        net = ipaddress.ip_network(entry.strip(), strict=False)
    except ValueError:
        return False
    return any(address in net for address in addresses)


def _source_reaches(
    rule: dict[str, Any],
    root: Any,
    addresses: list[ipaddress.IPv4Address],
    aliases: tuple[dict[str, list[str]], set[str]],
) -> bool:
    if rule["source_not"] or rule["source_kind"] == "any":
        return False
    value = str(rule["source"])
    if rule["source_kind"] == "network":
        net = _interface_net(root, value.removeprefix("network:"))
        return net is not None and _holds(net, addresses)
    entries, unknown = aliases
    todo, seen = [value], set[str]()
    while todo:
        entry = todo.pop()
        if _holds(entry, addresses) or entry in unknown:
            return True
        if entry in entries and entry not in seen:
            seen.add(entry)
            todo.extend(entries[entry])
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
    except ParseError as exc:
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


def source_check_php() -> list[str]:
    """Return PHP lines that define ``$meho_reach($rule_source)`` for ``$meho_sources``.

    ``$meho_sources`` holds the user's addresses, bare and as ``/32``. The
    closure answers ``true`` when the source holds one of them, by the
    rules in the module text.
    """
    return [
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
        "  if (!preg_match('/^[0-9]{1,2}$/', $meho_bits) || (int)$meho_bits > 32) {",
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
        "$meho_alias_unknown = array();",
        "foreach ((array)($config['aliases']['alias'] ?? array()) as $meho_al) {",
        "  $meho_an = (string)($meho_al['name'] ?? '');",
        "  $meho_at = (string)($meho_al['type'] ?? '');",
        f"  if (in_array($meho_at, array({_php_list(_URL_ALIAS_TYPES)}), true)) {{",
        "    $meho_alias_unknown[$meho_an] = true;",
        f"  }} elseif (in_array($meho_at, array({_php_list(_LIST_ALIAS_TYPES)}), true)) {{",
        "    $meho_list = trim((string)($meho_al['address'] ?? ''));",
        "    $meho_alias[$meho_an] = preg_split('/\\s+/', $meho_list, -1, PREG_SPLIT_NO_EMPTY);",
        "  }",
        "}",
        "$meho_reach = function ($meho_s) use ($meho_holds, $meho_alias, $meho_alias_unknown,",
        "    $config) {",
        "  if (!is_array($meho_s) || isset($meho_s['any']) || isset($meho_s['not'])) {",
        "    return false;",
        "  }",
        "  if (isset($meho_s['network'])) {",
        "    $meho_k = (string)$meho_s['network'];",
        "    $meho_only = substr($meho_k, -2) === 'ip';",
        "    if ($meho_only) {",
        "      $meho_k = substr($meho_k, 0, -2);",
        "    }",
        "    $meho_if = $config['interfaces'][$meho_k] ?? null;",
        "    if (!is_array($meho_if) || empty($meho_if['ipaddr']) || empty($meho_if['subnet'])) {",
        "      return false;",
        "    }",
        "    $meho_bits = $meho_only ? '32' : $meho_if['subnet'];",
        "    return $meho_holds($meho_if['ipaddr'] . '/' . $meho_bits);",
        "  }",
        "  if (!isset($meho_s['address'])) {",
        "    return false;",
        "  }",
        "  $meho_todo = array((string)$meho_s['address']);",
        "  $meho_seen = array();",
        "  while (count($meho_todo) > 0) {",
        "    $meho_e = array_pop($meho_todo);",
        "    if ($meho_holds($meho_e) || isset($meho_alias_unknown[$meho_e])) {",
        "      return true;",
        "    }",
        "    if (isset($meho_alias[$meho_e]) && !isset($meho_seen[$meho_e])) {",
        "      $meho_seen[$meho_e] = true;",
        "      foreach ($meho_alias[$meho_e] as $meho_x) {",
        "        $meho_todo[] = $meho_x;",
        "      }",
        "    }",
        "  }",
        "  return false;",
        "};",
    ]
