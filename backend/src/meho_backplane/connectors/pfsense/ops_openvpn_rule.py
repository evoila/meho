# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: declarative op module (planner + PHP fragment + op metadata /
# JSON schemas / llm_instructions) in the single-module-per-op-group shape of its
# pfSense siblings; the functions are small and low-complexity.

"""pfSense VPN onboarding: the per-user pass rules (``pfsense.openvpn.rule.sync``, #3993).

A VPN user may reach only the networks an operator gave them. On pfSense
that is one ``pass`` rule per user and destination on the OpenVPN rule tab
(interface ``openvpn``), with the user's tunnel address as source. This op
keeps those rules in line with a wanted list.

Tag
---

Every rule the op makes carries the description ``meho-vpn-user:<name>``.
That tag is how the op finds a user's rules again. A rule without the tag
is never counted and never changed, even when it looks the same.

How the sync works
------------------

1. Every wanted rule is checked first: the user exists, the addresses are
   IPv4, a port needs ``tcp``, ``udp`` or ``tcp/udp``. The source must be
   exactly the user's own fixed tunnel address (one host): the address of a
   client override of the user on a server that logs users in by name
   (reason ``source_not_user_address`` otherwise). A tagged rule of
   the user that does the same is ``unchanged``; else the rule is
   ``create``. A tagged rule of a named user that matches no wanted rule is
   ``not_wanted``: the op reports it and leaves it (remove it with
   ``pfsense.filter.rule.delete`` and its ``ridentifier``). A refused rule
   stops the whole call. Outside compare-only mode the call then fails with
   :class:`~meho_backplane.connectors.pfsense.refusal.PfSenseRefusedError`
   (code ``refused``) that names each refused rule.
2. In compare-only mode the call returns the plan and stops.
3. Otherwise the handler plays back one fragment and reads ``config.xml``
   again (pfSense keeps its own history of ``config.xml``). The
   fragment first checks that the tagged rules of each named user are still
   the ones the plan saw. When not, it writes nothing and prints
   ``MEHO_RULES_CHANGED``. Else it adds the new rules at the bottom of the
   OpenVPN tab (a new ``pass`` rule never goes above an existing ``block``
   rule there), then ``filter_rules_sort()``, ``write_config()`` and
   ``filter_configure()``. The read-back checks that each new rule is there
   once, as asked, and that nothing else in ``config.xml`` changed.

The op never deletes and never edits a rule. Each new rule gets a new
``ridentifier`` in the range of MEHO's rules (see
:mod:`~meho_backplane.connectors.pfsense.ops_filter_rule`).
"""

from __future__ import annotations

import ipaddress
import re
import secrets
from typing import TYPE_CHECKING, Any, Final

from defusedxml.ElementTree import ParseError, fromstring

from meho_backplane.connectors.pfsense.openvpn_config import (
    parse_csc_entries,
    parse_openvpn_servers,
    user_addresses,
)
from meho_backplane.connectors.pfsense.ops import PfSenseOp
from meho_backplane.connectors.pfsense.ops_filter_rule import (
    _PORT_PROTOCOLS,
    _normalize_port,
    _stored_rule_differences,
    parse_filter_rules_xml,
)
from meho_backplane.connectors.pfsense.ops_user_write import (
    USER_NAME_RE,
    _user_rows,
    _validate_user_name,
)
from meho_backplane.connectors.pfsense.ops_write import (
    _ALWAYS_REWRITTEN_SECTIONS,
    _apply_playback,
    _canonical,
    _child_text,
    _php_squote,
    _read_config_xml,
    serialized_config_write,
)
from meho_backplane.connectors.pfsense.refusal import PfSenseRefusedError
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "OPENVPN_RULE_OPS",
    "build_rule_sync_playback",
    "pfsense_openvpn_rule_sync",
    "plan_rule_sync",
    "rule_tag",
]

_OP_ID: Final = "pfsense.openvpn.rule.sync"
_INTERFACE: Final = "openvpn"
_TAG_PREFIX: Final = "meho-vpn-user:"
_PROTOCOLS: Final = ("any", "tcp", "udp", "tcp/udp", "icmp")
_MAX_RULES: Final = 200
_CHANGED_MARKER: Final = "MEHO_RULES_CHANGED="
_ACTIONS: Final = ("create", "unchanged", "not_wanted", "refused")


def rule_tag(name: str) -> str:
    """Return the description that marks a rule as the VPN rule of user *name*."""
    return f"{_TAG_PREFIX}{name}"


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def _ipv4(value: Any, field: str) -> str:
    """Return *value* as a bare IPv4 address (one host) or a canonical IPv4 network."""
    if not isinstance(value, str) or "%" in value or any(ch.isspace() for ch in value):
        raise ValueError(f"{field} must be an IPv4 address or network; got {value!r}")
    try:
        net = ipaddress.IPv4Network(value, strict=False)
    except ValueError as exc:
        raise ValueError(f"{field} must be an IPv4 address or network; got {value!r}") from exc
    return str(net.network_address) if net.prefixlen == 32 else str(net)


def _validate_rule(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("each rule must be an object")
    unknown = set(raw) - {"name", "source", "destination", "protocol", "destination_port"}
    if unknown:
        raise ValueError(f"unknown rule field(s): {', '.join(sorted(map(str, unknown)))}")
    name = _validate_user_name(raw.get("name"))
    protocol = raw.get("protocol", "any")
    if protocol not in _PROTOCOLS:
        raise ValueError(f"protocol must be one of {list(_PROTOCOLS)}; got {protocol!r}")
    port = _normalize_port(raw.get("destination_port"), "destination_port")
    if port is not None and protocol not in _PORT_PROTOCOLS:
        raise ValueError(f"a port needs protocol tcp, udp or tcp/udp; got {protocol!r}")
    return {
        "name": name,
        "source": _ipv4(raw.get("source"), "source"),
        "destination": _ipv4(raw.get("destination"), "destination"),
        "protocol": str(protocol),
        "destination_port": port,
    }


def _validate_rules(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > _MAX_RULES:
        raise ValueError(f"rules must be a list of 1 to {_MAX_RULES} rules")
    rules = [_validate_rule(raw) for raw in value]
    seen: set[tuple[Any, ...]] = set()
    for rule in rules:
        key = tuple(rule.values())
        if key in seen:
            raise ValueError(f"rules holds the same rule for {rule['name']!r} twice")
        seen.add(key)
    return rules


# ---------------------------------------------------------------------------
# The plan (pure)
# ---------------------------------------------------------------------------


def _spec(rule: dict[str, Any]) -> dict[str, Any]:
    """Return the stored form of *rule* in the shape of ``parse_filter_rules_xml``."""
    return {
        "type": "pass",
        "interface": _INTERFACE,
        "floating": False,
        "direction": None,
        "ipprotocol": "inet",
        "protocol": rule["protocol"],
        "source": rule["source"],
        "source_port": None,
        "destination": rule["destination"],
        "destination_port": rule["destination_port"],
        "quick": True,
        "log": False,
    }


def _row(rule: dict[str, Any], action: str, **kw: Any) -> dict[str, Any]:
    return {
        "name": rule["name"],
        "action": action,
        "reason": None,
        "ridentifier": None,
        "source": rule["source"],
        "destination": rule["destination"],
        "protocol": rule["protocol"],
        "destination_port": rule["destination_port"],
        **kw,
    }


def _tagged(stored: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [r for r in stored if (r.get("descr") or "").strip() == rule_tag(name)]


def plan_rule_sync(
    rules: list[dict[str, Any]],
    stored: list[dict[str, Any]],
    users: set[str],
    addresses: dict[str, set[str]],
) -> list[dict[str, Any]]:
    """Return one plan row per wanted rule, then one per tagged rule nobody wants.

    *addresses* maps a user name to its fixed tunnel addresses; a rule whose
    source is not one of them is refused.
    """
    plan: list[dict[str, Any]] = []
    matched: set[int] = set()
    for rule in rules:
        if rule["name"] not in users:
            plan.append(_row(rule, "refused", reason="user_not_found"))
            continue
        if rule["source"] not in addresses.get(rule["name"], set()):
            plan.append(_row(rule, "refused", reason="source_not_user_address"))
            continue
        same = [
            r
            for r in _tagged(stored, rule["name"])
            if id(r) not in matched and not _stored_rule_differences(r, _spec(rule))
        ]
        if same:
            matched.add(id(same[0]))
            plan.append(_row(rule, "unchanged", ridentifier=same[0].get("tracker")))
        else:
            plan.append(_row(rule, "create"))
    for name in dict.fromkeys(r["name"] for r in rules):
        for stored_rule in _tagged(stored, name):
            if id(stored_rule) not in matched:
                plan.append(
                    {
                        "name": name,
                        "action": "not_wanted",
                        "reason": None,
                        "ridentifier": stored_rule.get("tracker"),
                        "source": stored_rule.get("source"),
                        "destination": stored_rule.get("destination"),
                        "protocol": stored_rule.get("protocol"),
                        "destination_port": stored_rule.get("destination_port"),
                    }
                )
    return plan


def _assign_ridentifiers(plan: list[dict[str, Any]], stored: list[dict[str, Any]]) -> None:
    """Give each new rule a free ``ridentifier`` in MEHO's range (9 digits)."""
    taken = {r.get("tracker") for r in stored}
    for row in plan:
        if row["action"] != "create":
            continue
        while True:
            rid = str(100000000 + secrets.randbelow(900000000))
            if rid not in taken:
                break
        taken.add(rid)
        row["ridentifier"] = rid


# ---------------------------------------------------------------------------
# PHP fragment
# ---------------------------------------------------------------------------


def _php_rule(row: dict[str, Any]) -> list[str]:
    lines = [
        "$meho_rule = array();",
        f"$meho_rule['tracker'] = {_php_squote(row['ridentifier'])};",
        "$meho_rule['type'] = 'pass';",
        f"$meho_rule['interface'] = {_php_squote(_INTERFACE)};",
        "$meho_rule['ipprotocol'] = 'inet';",
    ]
    if row["protocol"] != "any":
        lines.append(f"$meho_rule['protocol'] = {_php_squote(row['protocol'])};")
    destination = f"array('address' => {_php_squote(row['destination'])}"
    if row["destination_port"]:
        destination += f", 'port' => {_php_squote(row['destination_port'])}"
    lines += [
        f"$meho_rule['source'] = array('address' => {_php_squote(row['source'])});",
        f"$meho_rule['destination'] = {destination});",
        f"$meho_rule['descr'] = {_php_squote(rule_tag(row['name']))};",
        "$meho_new[] = $meho_rule;",
    ]
    return lines


def build_rule_sync_playback(plan: list[dict[str, Any]], expected: dict[str, int]) -> str:
    """Return the fragment that adds the planned rules, or nothing when the rules moved.

    *expected* maps each named user's tag to the number of tagged rules the
    plan saw. When a count differs, or a new ``ridentifier`` is already
    used, the fragment prints ``MEHO_RULES_CHANGED=<...>`` and writes nothing.
    """
    rows = [r for r in plan if r["action"] == "create"]
    lines = [
        "global $config;",
        "$meho_rules = config_get_path('filter/rule', []);",
        "$meho_expected = array(",
        *[f"  {_php_squote(tag)} => {int(count)}," for tag, count in expected.items()],
        ");",
        "$meho_ids = array(",
        *[f"  {_php_squote(r['ridentifier'])}," for r in rows],
        ");",
        "$meho_seen = array();",
        "$meho_stale = array();",
        "foreach ($meho_expected as $meho_tag => $meho_n) {",
        "  $meho_seen[$meho_tag] = 0;",
        "}",
        "foreach ($meho_rules as $meho_r) {",
        "  $meho_d = isset($meho_r['descr']) ? trim((string)$meho_r['descr']) : '';",
        "  if (isset($meho_seen[$meho_d])) {",
        "    $meho_seen[$meho_d]++;",
        "  }",
        "  if (isset($meho_r['tracker'])",
        "      && in_array((string)$meho_r['tracker'], $meho_ids, true)) {",
        "    $meho_stale[] = 'id ' . $meho_r['tracker'];",
        "  }",
        "}",
        "foreach ($meho_expected as $meho_tag => $meho_n) {",
        "  if ($meho_seen[$meho_tag] !== $meho_n) {",
        "    $meho_stale[] = $meho_tag;",
        "  }",
        "}",
        "if (count($meho_stale) > 0) {",
        f"  echo '{_CHANGED_MARKER}' . implode(' ', $meho_stale) . \"\\n\";",
        "} else {",
        "  $meho_new = array();",
    ]
    for row in rows:
        lines += ["  " + line for line in _php_rule(row)]
    lines += [
        f"  $meho_idx = get_interface_ruleindex({_php_squote(_INTERFACE)});",
        "  if ($meho_idx['last'] !== null) {",
        "    array_splice($meho_rules, $meho_idx['last'] + 1, 0, $meho_new);",
        "  } else {",
        "    $meho_rules = array_merge($meho_rules, $meho_new);",
        "  }",
        "  config_set_path('filter/rule', $meho_rules);",
        "  filter_rules_sort();",
        f"  write_config({_php_squote('meho: sync VPN user rules')});",
        "  filter_configure();",
        "}",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _rest_of_config(xml_text: str, trackers: set[str]) -> dict[str, list[Any]]:
    """Return the canonical top-level sections without the rules in *trackers*."""
    try:
        root = fromstring(xml_text)
    except ParseError as exc:
        raise RuntimeError("config.xml could not be parsed for the read-back") from exc
    section = root.find("filter")
    if section is not None:
        for item in list(section.findall("rule")):
            if (_child_text(item, "tracker") or "").strip() in trackers:
                section.remove(item)
    rest: dict[str, list[Any]] = {}
    for child in root:
        if child.tag not in _ALWAYS_REWRITTEN_SECTIONS:
            rest.setdefault(child.tag, []).append(_canonical(child))
    return rest


def _verify(before: str, after: str, plan: list[dict[str, Any]]) -> None:
    rows = [r for r in plan if r["action"] == "create"]
    try:
        stored = parse_filter_rules_xml(after)
    except ValueError as exc:
        raise RuntimeError(f"openvpn.rule.sync verification failed: {exc}") from exc
    for row in rows:
        landed = [r for r in stored if r.get("tracker") == row["ridentifier"]]
        if (
            len(landed) != 1
            or (landed[0].get("descr") or "").strip() != rule_tag(row["name"])
            or _stored_rule_differences(landed[0], _spec(row))
        ):
            raise RuntimeError(
                f"openvpn.rule.sync verification failed for {row['name']!r} "
                f"(ridentifier {row['ridentifier']}): config.xml does not hold the rule as "
                "asked after write_config; the pfSsh.php playback did not persist"
            )
    trackers = {r["ridentifier"] for r in rows}
    if _rest_of_config(before, trackers) != _rest_of_config(after, trackers):
        raise RuntimeError(
            "openvpn.rule.sync: the rules were saved, but other parts of config.xml also "
            "changed while the op ran. Check the pfSense config history before you run "
            "the op again."
        )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _summary(plan: list[dict[str, Any]]) -> dict[str, int]:
    return {a: sum(1 for r in plan if r["action"] == a) for a in _ACTIONS}


async def _plan_from_config(
    self: PfSenseConnector, target: Any, operator: Operator | None, rules: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    before = await _read_config_xml(self, target, operator)
    try:
        stored = parse_filter_rules_xml(before)
    except ValueError as exc:
        raise RuntimeError(f"openvpn.rule.sync: config.xml could not be read: {exc}") from exc
    users = {r["name"] for r in _user_rows(before)}
    servers = parse_openvpn_servers(before)
    entries = parse_csc_entries(before)
    addresses = {
        name: {str(a) for a in user_addresses(servers, entries, name)}
        for name in {r["name"] for r in rules}
    }
    return before, stored, plan_rule_sync(rules, stored, users, addresses)


@serialized_config_write
async def pfsense_openvpn_rule_sync(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.openvpn.rule.sync`` -- add the missing per-user VPN rules."""
    rules = _validate_rules(params.get("rules"))
    compare_only = bool(params.get("compare_only", False))
    before, stored, plan = await _plan_from_config(self, target, operator, rules)
    counts = _summary(plan)
    result: dict[str, Any] = {
        "op_class": "write",
        "resource": "openvpn_rule",
        "status": "compared",
        "compare_only": compare_only,
        "applied": False,
        "verified": False,
        "counts": counts,
        "rules": plan,
        "guidance": None,
    }
    if counts["refused"]:
        if not compare_only:
            refused = "; ".join(
                f"{r['name']} ({r['source']} to {r['destination']}): {r['reason']}"
                for r in plan
                if r["action"] == "refused"
            )
            raise PfSenseRefusedError(
                _OP_ID,
                "refused",
                f"nothing was written. Refused rules: {refused}. Run with compare_only to "
                "see the whole plan.",
            )
        return {
            **result,
            "status": "refused",
            "guidance": "at least one rule was refused; nothing was written (see rules)",
        }
    if counts["create"] == 0:
        return {**result, "status": "nothing_to_do", "verified": True}
    if compare_only:
        return result

    _assign_ridentifiers(plan, stored)
    expected = {
        rule_tag(name): len(_tagged(stored, name))
        for name in dict.fromkeys(r["name"] for r in plan)
    }
    played = await _apply_playback(
        self,
        target,
        script_name=f"meho_openvpn_rule_sync_{secrets.token_hex(4)}",
        script_body=build_rule_sync_playback(plan, expected),
        operator=operator,
    )
    stale = re.search(rf"^{_CHANGED_MARKER}(.*)$", played, re.MULTILINE)
    if stale is not None:
        raise RuntimeError(
            "openvpn.rule.sync: the rules changed after the plan was made "
            f"({stale.group(1).strip()}); nothing was written. Run the op again."
        )
    _verify(before, await _read_config_xml(self, target, operator), plan)
    return {
        **result,
        "status": "synced",
        "applied": True,
        "verified": True,
        "rules": plan,
    }


# ---------------------------------------------------------------------------
# Approval preview -- the plan
# ---------------------------------------------------------------------------


async def _rule_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    try:
        rules = _validate_rules(ctx.params.get("rules"))
    except ValueError:
        return None
    preview: dict[str, Any] = {
        "rules_requested": len(rules),
        "compare_only": bool(ctx.params.get("compare_only", False)),
        "counts": None,
        "rules": None,
    }
    if ctx.connector_instance is None:
        return preview
    connector: PfSenseConnector = ctx.connector_instance  # type: ignore[assignment]
    _, _, plan = await _plan_from_config(connector, ctx.target, ctx.operator, rules)
    return {**preview, "counts": _summary(plan), "rules": plan}


register_preview_builder(_OP_ID, _rule_preview)


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_IPV4_DESCRIPTION: Final = "IPv4 address (one host) or IPv4 network such as '192.0.2.0/24'."

_RULE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "pattern": USER_NAME_RE.pattern,
            "description": "The VPN user the rule is for.",
        },
        "source": {
            "type": "string",
            "description": (
                "The user's own fixed tunnel address (one host), as set with "
                "pfsense.openvpn.csc.sync. Any other source is refused."
            ),
        },
        "destination": {
            "type": "string",
            "description": "Where the user may go. " + _IPV4_DESCRIPTION,
        },
        "protocol": {
            "type": "string",
            "enum": list(_PROTOCOLS),
            "default": "any",
            "description": "IP protocol the rule lets through. Default 'any'.",
        },
        "destination_port": {
            "type": ["string", "integer"],
            "description": "Optional port 'N' or range 'A-B'; needs tcp, udp or tcp/udp.",
        },
    },
    "required": ["name", "source", "destination"],
    "additionalProperties": False,
}

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": _RULE_PARAMETER_SCHEMA,
            "minItems": 1,
            "maxItems": _MAX_RULES,
            "description": "The pass rules you want, one per user, source and destination.",
        },
        "compare_only": {
            "type": "boolean",
            "description": "When true, return the plan and write nothing. Default false.",
        },
    },
    "required": ["rules"],
    "additionalProperties": False,
}

_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op_class": {"type": "string", "enum": ["write"]},
        "resource": {"type": "string", "enum": ["openvpn_rule"]},
        "status": {"type": "string", "enum": ["compared", "synced", "nothing_to_do", "refused"]},
        "compare_only": {"type": "boolean"},
        "applied": {"type": "boolean"},
        "verified": {"type": "boolean"},
        "counts": {
            "type": "object",
            "properties": {a: {"type": "integer"} for a in _ACTIONS},
            "required": list(_ACTIONS),
            "additionalProperties": False,
        },
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "action": {"type": "string", "enum": list(_ACTIONS)},
                    "reason": _NULLABLE_STRING,
                    "ridentifier": _NULLABLE_STRING,
                    "source": _NULLABLE_STRING,
                    "destination": _NULLABLE_STRING,
                    "protocol": _NULLABLE_STRING,
                    "destination_port": _NULLABLE_STRING,
                },
                "required": [
                    "name",
                    "action",
                    "reason",
                    "ridentifier",
                    "source",
                    "destination",
                    "protocol",
                    "destination_port",
                ],
                "additionalProperties": False,
            },
        },
        "guidance": _NULLABLE_STRING,
    },
    "required": [
        "op_class",
        "resource",
        "status",
        "compare_only",
        "applied",
        "verified",
        "counts",
        "rules",
        "guidance",
    ],
    "additionalProperties": False,
}

_WHEN_TO_CALL: Final = (
    "Use to let a VPN user reach the networks they need: one pass rule per user and "
    "destination on the OpenVPN rule tab. Safe to run twice: a rule that is already "
    "there is ``unchanged``. The approval card shows the plan."
)

OPENVPN_RULE_OPS: tuple[PfSenseOp, ...] = (
    PfSenseOp(
        op_id=_OP_ID,
        handler_attr="openvpn_rule_sync",
        summary="Add the missing tagged per-user VPN pass rules; can compare only.",
        description=(
            "Keeps the tagged pass rules of VPN users on the OpenVPN rule tab in line with "
            "a wanted list. The source must be the user's own tunnel address. Adds the "
            "missing rules, reports tagged rules nobody asked for, and never deletes or "
            "edits a rule."
        ),
        parameter_schema={
            **_PARAMETER_SCHEMA,
            "examples": [
                {
                    "rules": [
                        {
                            "name": "<vpn-user>",
                            "source": "198.51.100.2",
                            "destination": "192.0.2.0/24",
                        }
                    ],
                    "compare_only": True,
                }
            ],
        },
        response_schema=_RESPONSE_SCHEMA,
        group_key="vpn",
        tags=("write", "vpn", "firewall", "pfsense"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_call": _WHEN_TO_CALL,
            "when_to_use": _WHEN_TO_CALL,
            "preconditions": (
                "The user exists (``pfsense.openvpn.user.create``) and has a tunnel "
                "address (``pfsense.openvpn.csc.sync``). Run with ``compare_only: true`` "
                "first and read the counts."
            ),
            "parameter_hints": {
                "rules": "Required. One object per user, source and destination.",
                "compare_only": (
                    "Optional. true returns the plan and writes nothing. The call still "
                    "waits for approval."
                ),
            },
            "output_shape": (
                "``{status, compare_only, applied, verified, counts, rules, guidance}``. "
                "``rules`` has one row per wanted rule (``create``, "
                "``unchanged``, ``refused``) and one per tagged rule nobody asked for "
                "(``not_wanted``, with its ``ridentifier``). ``status`` ``refused`` comes "
                "only with ``compare_only``; otherwise a refused rule fails the call and "
                "the error names each refused rule."
            ),
            "next_step": (
                "Remove a ``not_wanted`` rule with ``pfsense.filter.rule.delete`` and its "
                "``ridentifier``. Check the rules with ``pfsense.firewall.rules``."
            ),
        },
    ),
)
