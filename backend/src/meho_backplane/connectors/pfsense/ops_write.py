# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: declarative op module (parsers + small handlers + agent-facing
# op metadata / JSON schemas / llm_instructions) mirroring the ops_read.py sibling's
# single-module-per-op-group convention; all functions are small + low-complexity.

"""pfSense write ops -- named gateway + static-route add (#3090).

Adds the first two mutating typed ops to :class:`PfSenseConnector`:

* ``pfsense.gateway.add`` -- append a named ``gateway_item`` (interface,
  gateway IP, name, optional ``monitor_disable`` for a pre-staged gateway
  whose upstream device does not exist yet) to ``config.xml`` and call
  ``write_config()``.
* ``pfsense.route.static.add`` -- append a ``staticroutes/route`` entry
  (CIDR network -> gateway name) to ``config.xml``, then ``write_config()``
  followed by ``system_routing_configure()`` to apply the new route.

The config mutation runs through pfSense's ``pfSsh.php playback`` idiom --
the same mechanism the read op ``pfsense.gateway.list`` already uses to
read live ``dpinger`` status (``pfSsh.php playback gatewaystatus``).
``pfSsh.php playback`` resolves its argument through ``basename()`` against
``/etc/phpshellsessions/``, so the write path stages a raw-PHP fragment
into that directory, plays it back, and removes it. The fragment is
prepended by ``playback_text`` with ``require_once`` of the pfSense config
libraries and ``eval``-ed inside a function scope, so the fragment opens
with ``global $config;`` (mirroring the shipped ``gatewaystatus`` script's
``global $argv;``) and carries no ``<?php`` tag and no trailing ``exec``.

Guarded + idempotent
--------------------

Both handlers read ``/cf/conf/config.xml`` first (via the same
``cat /cf/conf/config.xml`` the read ops use) and parse the relevant block
before staging any change. Adding a gateway whose ``name`` already exists,
or a route whose (canonicalised) ``network`` already exists, is reported as
a structured already-present outcome (``existed_before=True``,
``applied=False``, ``existing`` carrying the pre-existing row) and stages
no playback -- no duplicate is ever appended. A gateway add that is
actually applied is read back afterwards; a fragment that failed to persist
(non-zero playback exit, or a silent ``write_config`` failure that leaves
the entry absent) raises rather than reporting a false success.

Injection safety
----------------

Every operator-supplied value is validated and re-serialised before it
reaches the PHP fragment: names/interfaces are matched against a strict
character-class allowlist, IPs are parsed and re-emitted through
:mod:`ipaddress`, and CIDRs are normalised to their canonical network
form. The validated tokens carry none of the shell metacharacters or PHP
string-literal terminators an interpolation attack would need; on top of
that the fragment is transmitted inside a quoted heredoc (no shell
expansion) and each value is emitted through :func:`_php_squote`
(backslash + single-quote escaped). Anything outside the allowlist is
rejected before a single SSH round-trip.

Surgical contract
-----------------

The handlers touch only ``gateways/gateway_item`` and
``staticroutes/route``. They never re-enumerate or reconfigure interfaces:
the perimeter pfSense frequently carries the operator's own access path, so
an interface stun would sever the very session issuing the change.
``pfsense.route.static.add`` calls ``system_routing_configure()`` (route
table apply only); ``pfsense.gateway.add`` calls neither that nor any
interface apply -- a pre-staged gateway is inert config until a route or
policy references it.

References
----------

* Task: #3090.
* Read-op precedent: G3.7 (#844 / #847 / #850);
  :mod:`meho_backplane.connectors.pfsense.ops_read`.
* Classification precedent (additive config write, ``caution`` /
  no-approval): :mod:`meho_backplane.connectors.bind9.ops_record`
  (``bind9.record.add``).
* pfSense PHP shell:
  https://docs.netgate.com/pfsense/en/latest/development/php-shell.html
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING, Any

from defusedxml.ElementTree import ParseError, fromstring

from meho_backplane.connectors.pfsense.ops import PfSenseOp
from meho_backplane.connectors.pfsense.ops_read import parse_gateways_xml

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "WRITE_OPS",
    "parse_static_routes_xml",
    "pfsense_gateway_add",
    "pfsense_route_static_add",
]

# pfSense stages playback scripts under this directory; ``pfSsh.php
# playback <name>`` resolves the argument through ``basename()`` against
# it, so only a bare filename (no path) is ever accepted.
_PHPSHELLSESSIONS_DIR = "/etc/phpshellsessions"

# Quoted heredoc delimiter -- the single quotes make the remote shell
# treat the body verbatim (no ``$config`` / backtick expansion), so the
# PHP fragment crosses the wire byte-for-byte.
_HEREDOC_DELIM = "MEHO_PFSENSE_PLAYBACK_EOF"

# Strict input allowlists. pfSense gateway names use alphanumerics, dash,
# and underscore (its own defaults look like ``WAN_DHCP``); logical
# interface names are alphanumerics + underscore (``wan`` / ``lan`` /
# ``opt1``). Anything else is rejected before any SSH round-trip.
_GATEWAY_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_INTERFACE_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_gateway_name(value: Any) -> str:
    """Return *value* if it is a valid pfSense gateway name, else raise.

    Allowlist: ``^[A-Za-z0-9_-]{1,64}$`` (alphanumeric, dash, underscore).
    """
    if not isinstance(value, str) or not _GATEWAY_NAME_RE.match(value):
        raise ValueError(
            "gateway name must match ^[A-Za-z0-9_-]{1,64}$ "
            f"(alphanumeric, dash, underscore); got {value!r}"
        )
    return value


def _validate_interface(value: Any) -> str:
    """Return *value* if it is a valid pfSense logical interface, else raise.

    Allowlist: ``^[A-Za-z0-9_]{1,32}$`` -- the internal pfSense interface
    handle (``wan`` / ``lan`` / ``opt1``), not the OS device name.
    """
    if not isinstance(value, str) or not _INTERFACE_RE.match(value):
        raise ValueError(
            "interface must match ^[A-Za-z0-9_]{1,32}$ "
            "(pfSense logical interface handle, e.g. 'wan', 'lan', 'opt1'); "
            f"got {value!r}"
        )
    return value


def _validate_gateway_ip(value: Any) -> tuple[str, str]:
    """Parse *value* as an IP address; return ``(canonical_ip, ipprotocol)``.

    ``ipprotocol`` is ``inet`` for IPv4 and ``inet6`` for IPv6 -- the value
    pfSense stores on ``gateway_item`` to select the address family.
    """
    if not isinstance(value, str):
        raise ValueError(f"gateway must be a string IP address; got {value!r}")
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise ValueError(f"gateway must be a valid IPv4 or IPv6 address; got {value!r}") from exc
    return str(addr), ("inet6" if addr.version == 6 else "inet")


def _validate_network_cidr(value: Any) -> str:
    """Parse *value* as a CIDR network; return its canonical string form.

    ``strict=False`` accepts a host-bit-set input (``10.9.0.5/24``) and
    canonicalises it to the network address (``10.9.0.0/24``) -- the form a
    static route stores and the form idempotency compares against.
    """
    if not isinstance(value, str):
        raise ValueError(f"network must be a string CIDR; got {value!r}")
    try:
        net = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as exc:
        raise ValueError(
            f"network must be a valid CIDR, e.g. '10.9.0.0/24'; got {value!r}"
        ) from exc
    return str(net)


def _php_squote(value: str) -> str:
    """Return *value* as a single-quoted PHP string literal.

    Defence-in-depth: the callers already validate every interpolated value
    against a metacharacter-free allowlist, so the escape is a no-op in
    practice, but emitting through here keeps the fragment safe even if a
    future caller widens an allowlist.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


# ---------------------------------------------------------------------------
# config.xml parsing (idempotency guard)
# ---------------------------------------------------------------------------


def parse_static_routes_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse the ``<staticroutes>`` block from ``/cf/conf/config.xml``.

    Accepts either the full ``config.xml`` text or a ``<staticroutes>``
    snippet. Returns one dict per ``<route>`` child with ``network``,
    ``gateway``, and ``descr`` keys (``None`` for a missing tag). Returns an
    empty list on any parse failure. Mirrors
    :func:`meho_backplane.connectors.pfsense.ops_read.parse_gateways_xml`.

    >>> xml = (
    ...     "<staticroutes><route>"
    ...     "<network>10.9.0.0/24</network><gateway>LAB_GW</gateway>"
    ...     "</route></staticroutes>"
    ... )
    >>> parse_static_routes_xml(xml)[0]["network"]
    '10.9.0.0/24'
    """
    if not xml_text.strip():
        return []
    try:
        root = fromstring(xml_text)
    except ParseError:
        return []
    sr_root = root if root.tag == "staticroutes" else root.find(".//staticroutes")
    if sr_root is None:
        return []
    rows: list[dict[str, Any]] = []
    for item in sr_root.findall("route"):
        rows.append(
            {
                "network": _child_text(item, "network"),
                "gateway": _child_text(item, "gateway"),
                "descr": _child_text(item, "descr"),
            }
        )
    return rows


def _child_text(element: Any, tag: str) -> str | None:
    """Return the text of *tag* under *element*, or ``None``.

    *element* is a ``defusedxml``-parsed node; defusedxml ships no type
    stubs, so it surfaces as ``Any`` (annotating it with the stdlib
    ``Element`` type would re-introduce the ``xml.etree`` import Semgrep
    flags as an XXE vector).
    """
    child = element.find(tag)
    return child.text if child is not None else None


def _find_gateway(xml_text: str, name: str) -> dict[str, Any] | None:
    """Return the ``gateway_item`` row named *name*, or ``None``."""
    for row in parse_gateways_xml(xml_text):
        if row.get("name") == name:
            return row
    return None


def _find_static_route(xml_text: str, network: str) -> dict[str, Any] | None:
    """Return the ``route`` row whose canonical network equals *network*.

    Both the stored network and the query are canonicalised before
    comparison, so ``10.9.0.5/24`` and ``10.9.0.0/24`` match the same route.
    """
    for row in parse_static_routes_xml(xml_text):
        stored = row.get("network")
        if not isinstance(stored, str):
            continue
        try:
            if str(ipaddress.ip_network(stored.strip(), strict=False)) == network:
                return row
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# pfSsh.php playback fragment construction + apply
# ---------------------------------------------------------------------------


def _build_gateway_playback(
    *,
    name: str,
    interface: str,
    gateway: str,
    ipprotocol: str,
    monitor_disable: bool,
) -> str:
    """Return the raw-PHP playback fragment that appends one gateway_item."""
    lines = [
        "global $config;",
        "if (!is_array($config['gateways'])) { $config['gateways'] = array(); }",
        "if (!is_array($config['gateways']['gateway_item'])) "
        "{ $config['gateways']['gateway_item'] = array(); }",
        "$gw = array();",
        f"$gw['interface'] = {_php_squote(interface)};",
        f"$gw['gateway'] = {_php_squote(gateway)};",
        f"$gw['name'] = {_php_squote(name)};",
        "$gw['weight'] = '1';",
        f"$gw['ipprotocol'] = {_php_squote(ipprotocol)};",
        f"$gw['descr'] = {_php_squote('meho: pre-staged gateway ' + name)};",
    ]
    if monitor_disable:
        lines.append("$gw['monitor_disable'] = '';")
    lines.append("$config['gateways']['gateway_item'][] = $gw;")
    lines.append(f"write_config({_php_squote('meho: add gateway ' + name)});")
    return "\n".join(lines) + "\n"


def _build_route_playback(*, network: str, gateway: str) -> str:
    """Return the raw-PHP playback fragment that appends one static route."""
    lines = [
        "global $config;",
        "if (!is_array($config['staticroutes'])) { $config['staticroutes'] = array(); }",
        "if (!is_array($config['staticroutes']['route'])) "
        "{ $config['staticroutes']['route'] = array(); }",
        "$route = array();",
        f"$route['network'] = {_php_squote(network)};",
        f"$route['gateway'] = {_php_squote(gateway)};",
        f"$route['descr'] = {_php_squote('meho: static route ' + network)};",
        "$config['staticroutes']['route'][] = $route;",
        f"write_config({_php_squote('meho: add static route ' + network)});",
        "system_routing_configure();",
    ]
    return "\n".join(lines) + "\n"


def _install_script_command(script_name: str, script_body: str) -> str:
    """Return the shell command that stages *script_body* for playback.

    A quoted-delimiter heredoc writes the fragment verbatim into
    ``/etc/phpshellsessions/<script_name>``; the single-quoted delimiter
    stops the remote shell expanding ``$config`` or anything else.
    """
    path = f"{_PHPSHELLSESSIONS_DIR}/{script_name}"
    return f"cat > {path} <<'{_HEREDOC_DELIM}'\n{script_body}{_HEREDOC_DELIM}\n"


async def _read_config_xml(self: PfSenseConnector, target: Any, operator: Operator | None) -> str:
    """Read ``/cf/conf/config.xml`` and return its text; raise on read failure."""
    proc = await self._run_command(target, "cat /cf/conf/config.xml", operator=operator)
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    content = stdout if isinstance(stdout, str) else ""
    if proc.exit_status != 0 and not content.strip():
        raise RuntimeError(f"cat /cf/conf/config.xml exit {proc.exit_status}")
    return content


async def _apply_playback(
    self: PfSenseConnector,
    target: Any,
    *,
    script_name: str,
    script_body: str,
    operator: Operator | None,
) -> None:
    """Stage, play back, and clean up a pfSsh.php playback fragment.

    Raises :exc:`RuntimeError` when staging or playback exits non-zero. The
    staged script is removed in a ``finally`` so a failed playback never
    leaves the fragment behind.
    """
    path = f"{_PHPSHELLSESSIONS_DIR}/{script_name}"
    staged = await self._run_command(
        target, _install_script_command(script_name, script_body), operator=operator
    )
    if staged.exit_status != 0:
        raise RuntimeError(
            f"failed to stage pfSense playback script {path!r}: exit {staged.exit_status}"
        )
    try:
        played = await self._run_command(
            target, f"pfSsh.php playback {script_name}", operator=operator
        )
    finally:
        await self._run_command(target, f"rm -f {path}", operator=operator)
    if played.exit_status != 0:
        raise RuntimeError(f"pfSsh.php playback {script_name!r} failed: exit {played.exit_status}")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def pfsense_gateway_add(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.gateway.add`` -- guarded, idempotent gateway write.

    Sequence: validate inputs -> read ``config.xml`` -> if a gateway with
    the same ``name`` already exists, return the already-present outcome
    without staging anything -> otherwise stage + play back the append
    fragment, then read ``config.xml`` back and confirm the row landed.

    Returns ``{op_class, resource, name, interface, gateway, ipprotocol,
    monitor_disable, existed_before, applied, existing}``. ``existing`` is
    the pre-existing ``gateway_item`` row when ``existed_before`` is true
    (surfacing a name collision whose interface / IP may differ from the
    request), else ``None``.
    """
    name = _validate_gateway_name(params.get("name"))
    interface = _validate_interface(params.get("interface"))
    gateway, ipprotocol = _validate_gateway_ip(params.get("gateway"))
    monitor_disable = bool(params.get("monitor_disable", False))

    before = await _read_config_xml(self, target, operator)
    existing = _find_gateway(before, name)
    result: dict[str, Any] = {
        "op_class": "write",
        "resource": "gateway",
        "name": name,
        "interface": interface,
        "gateway": gateway,
        "ipprotocol": ipprotocol,
        "monitor_disable": monitor_disable,
    }
    if existing is not None:
        return {**result, "existed_before": True, "applied": False, "existing": existing}

    body = _build_gateway_playback(
        name=name,
        interface=interface,
        gateway=gateway,
        ipprotocol=ipprotocol,
        monitor_disable=monitor_disable,
    )
    await _apply_playback(
        self,
        target,
        script_name=f"meho_gateway_add_{name}",
        script_body=body,
        operator=operator,
    )

    after = await _read_config_xml(self, target, operator)
    if _find_gateway(after, name) is None:
        raise RuntimeError(
            f"gateway {name!r} was not present in config.xml after write_config; "
            "the pfSsh.php playback did not persist"
        )
    return {**result, "existed_before": False, "applied": True, "existing": None}


async def pfsense_route_static_add(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.route.static.add`` -- guarded, idempotent route write.

    Sequence: validate inputs -> read ``config.xml`` -> require the named
    gateway to already exist (a route to an undefined gateway is a
    guaranteed misconfiguration; pre-stage it with ``pfsense.gateway.add``
    first) -> if a route with the same canonical ``network`` already exists,
    return the already-present outcome without staging anything -> otherwise
    stage + play back the append fragment (which also runs
    ``system_routing_configure()``), then read ``config.xml`` back and
    confirm the row landed.

    Returns ``{op_class, resource, network, gateway, existed_before,
    applied, existing}``.
    """
    network = _validate_network_cidr(params.get("network"))
    gateway = _validate_gateway_name(params.get("gateway"))

    before = await _read_config_xml(self, target, operator)
    if _find_gateway(before, gateway) is None:
        raise ValueError(
            f"static route references gateway {gateway!r}, which is not defined "
            "in config.xml; add it first with pfsense.gateway.add"
        )
    existing = _find_static_route(before, network)
    result: dict[str, Any] = {
        "op_class": "write",
        "resource": "static_route",
        "network": network,
        "gateway": gateway,
    }
    if existing is not None:
        return {**result, "existed_before": True, "applied": False, "existing": existing}

    body = _build_route_playback(network=network, gateway=gateway)
    await _apply_playback(
        self,
        target,
        script_name=f"meho_route_add_{_route_script_token(network)}",
        script_body=body,
        operator=operator,
    )

    after = await _read_config_xml(self, target, operator)
    if _find_static_route(after, network) is None:
        raise RuntimeError(
            f"static route {network!r} was not present in config.xml after "
            "write_config; the pfSsh.php playback did not persist"
        )
    return {**result, "existed_before": False, "applied": True, "existing": None}


def _route_script_token(network: str) -> str:
    """Return a filename-safe token for a CIDR (``/``, ``.``, ``:`` -> ``_``)."""
    return re.sub(r"[^A-Za-z0-9]", "_", network)


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

#: Curated ``when_to_use`` for the ``routing`` group -- the operator-facing
#: prose ``list_operation_groups`` surfaces. Mirrored into
#: ``_WHEN_TO_USE_BY_GROUP`` in ``connector.py`` (the authoritative copy the
#: registration walk reads).
_WHEN_TO_USE_ROUTING = (
    "Use for pfSense routing-plane provisioning writes: appending a named "
    "gateway (``pfsense.gateway.add``) or a static route "
    "(``pfsense.route.static.add``) to ``config.xml``. Call "
    "``pfsense.gateway.add`` to define a next-hop -- including a pre-staged "
    "one (``monitor_disable``) whose upstream device does not exist yet -- "
    "then ``pfsense.route.static.add`` to point a CIDR at it. Both are "
    "idempotent: re-adding an existing gateway name or route network is a "
    "reported no-op, never a duplicate. Both are surgical -- they touch only "
    "the gateway / static-route config and never re-enumerate interfaces, so "
    "they are safe to run against a perimeter firewall that carries the "
    "operator's own access path. Read the current state first with "
    "``pfsense.gateway.list`` / ``pfsense.config.show``."
)

_GATEWAY_ADD_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_-]{1,64}$",
            "description": (
                "Gateway name, e.g. ``LAB_GW``. Alphanumeric, dash, and "
                "underscore only. Idempotency key: a gateway with this name "
                "already present is a reported no-op."
            ),
        },
        "interface": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_]{1,32}$",
            "description": (
                "pfSense logical interface handle the gateway lives on, e.g. "
                "``wan`` / ``lan`` / ``opt1`` (not the OS device name)."
            ),
        },
        "gateway": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Next-hop IP address (IPv4 or IPv6). Parsed and "
                "re-serialised; the address family selects ``ipprotocol``."
            ),
        },
        "monitor_disable": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, set ``monitor_disable`` on the gateway so "
                "``dpinger`` does not mark it down -- use for a pre-staged "
                "gateway whose upstream device is not up yet."
            ),
        },
    },
    "required": ["name", "interface", "gateway"],
    "additionalProperties": False,
}

_GATEWAY_ADD_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op_class": {"type": "string", "enum": ["write"]},
        "resource": {"type": "string", "enum": ["gateway"]},
        "name": {"type": "string"},
        "interface": {"type": "string"},
        "gateway": {"type": "string"},
        "ipprotocol": {"type": "string", "enum": ["inet", "inet6"]},
        "monitor_disable": {"type": "boolean"},
        "existed_before": {"type": "boolean"},
        "applied": {"type": "boolean"},
        "existing": {"type": ["object", "null"]},
    },
    "required": [
        "op_class",
        "resource",
        "name",
        "interface",
        "gateway",
        "ipprotocol",
        "monitor_disable",
        "existed_before",
        "applied",
        "existing",
    ],
    "additionalProperties": False,
}

_GATEWAY_ADD_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Add a named routing gateway to pfSense. Guarded + idempotent: the "
        "handler reads ``config.xml`` first and, if a gateway with the same "
        "``name`` already exists, returns ``existed_before=true`` / "
        "``applied=false`` without appending a duplicate. WARNING: "
        "``write_config()`` persists to the live ``config.xml`` for the whole "
        "firewall. safety_level=caution. The op is surgical -- it never "
        "touches interface config, so it will not stun the operator's own "
        "access path on a perimeter firewall. Use ``pfsense.gateway.list`` "
        "first to see existing gateways."
    ),
    "parameter_hints": {
        "name": "Required. Gateway name (alphanumeric / dash / underscore).",
        "interface": "Required. pfSense logical interface handle (``wan`` / ``lan`` / ``opt1``).",
        "gateway": "Required. Next-hop IPv4 or IPv6 address.",
        "monitor_disable": (
            "Optional (default false). Disable dpinger monitoring for a "
            "pre-staged gateway whose upstream is not up yet."
        ),
    },
    "output_shape": (
        "``{op_class: 'write', resource: 'gateway', name, interface, "
        "gateway, ipprotocol, monitor_disable, existed_before, applied, "
        "existing}``. ``ipprotocol`` is ``inet`` / ``inet6`` from the IP "
        "family. When ``existed_before`` is true, ``applied`` is false and "
        "``existing`` carries the pre-existing gateway row (whose interface "
        "/ IP may differ from the request); otherwise ``existing`` is null."
    ),
}

_ROUTE_ADD_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "network": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Destination network in CIDR form, e.g. ``10.9.0.0/24``. "
                "Canonicalised to the network address; the idempotency key."
            ),
        },
        "gateway": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_-]{1,64}$",
            "description": (
                "Name of an already-defined gateway the route points at. "
                "Must exist in ``config.xml`` (pre-stage it with "
                "``pfsense.gateway.add``); a route to an undefined gateway is "
                "rejected."
            ),
        },
    },
    "required": ["network", "gateway"],
    "additionalProperties": False,
}

_ROUTE_ADD_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op_class": {"type": "string", "enum": ["write"]},
        "resource": {"type": "string", "enum": ["static_route"]},
        "network": {"type": "string"},
        "gateway": {"type": "string"},
        "existed_before": {"type": "boolean"},
        "applied": {"type": "boolean"},
        "existing": {"type": ["object", "null"]},
    },
    "required": [
        "op_class",
        "resource",
        "network",
        "gateway",
        "existed_before",
        "applied",
        "existing",
    ],
    "additionalProperties": False,
}

_ROUTE_ADD_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Add a static route (CIDR -> named gateway) to pfSense and apply it "
        "(``write_config()`` + ``system_routing_configure()``). Guarded + "
        "idempotent: the handler reads ``config.xml`` first, requires the "
        "named gateway to already exist, and returns ``existed_before=true`` "
        "/ ``applied=false`` when a route for the same network is already "
        "present rather than appending a duplicate. WARNING: this changes "
        "the live routing table for the whole firewall. safety_level=caution. "
        "Surgical -- it touches only the static-route config, never "
        "interfaces."
    ),
    "parameter_hints": {
        "network": "Required. Destination CIDR, e.g. ``10.9.0.0/24`` (canonicalised).",
        "gateway": (
            "Required. Name of an existing gateway (add it first with ``pfsense.gateway.add``)."
        ),
    },
    "output_shape": (
        "``{op_class: 'write', resource: 'static_route', network, gateway, "
        "existed_before, applied, existing}``. ``network`` is the "
        "canonical CIDR. When ``existed_before`` is true, ``applied`` is "
        "false and ``existing`` carries the pre-existing route row; "
        "otherwise ``existing`` is null."
    ),
}

#: The write ops :class:`PfSenseConnector` registers alongside the read
#: surface. Both are additive config mutations classified
#: ``safety_level='caution'`` / ``requires_approval=False`` -- the same
#: posture ``bind9.record.add`` / ``windns.record.add`` carry for an
#: additive, recoverable, idempotent write.
WRITE_OPS: tuple[PfSenseOp, ...] = (
    PfSenseOp(
        op_id="pfsense.gateway.add",
        handler_attr="gateway_add",
        summary="Append a named routing gateway to config.xml (guarded, idempotent).",
        description=(
            "Reads ``/cf/conf/config.xml`` for the ``<gateways>`` block, and "
            "if no ``gateway_item`` with the requested ``name`` exists, "
            "stages a ``pfSsh.php playback`` fragment that appends one "
            "(interface, gateway IP, name, ``ipprotocol`` from the IP family, "
            "optional ``monitor_disable`` for a pre-staged gateway) and calls "
            "``write_config()``, then reads the config back to confirm the "
            "row landed. A name that already exists is a reported no-op "
            "(``existed_before=true``, ``applied=false``) -- never a "
            "duplicate. Surgical: touches only ``gateways/gateway_item``, "
            "never interface config. Inputs are validated (name / interface "
            "allowlist, IP parsed) before any SSH round-trip. "
            "safety_level=caution; recoverable by removing the gateway_item "
            "and re-running write_config."
        ),
        parameter_schema=_GATEWAY_ADD_PARAMETER_SCHEMA,
        response_schema=_GATEWAY_ADD_RESPONSE_SCHEMA,
        group_key="routing",
        tags=("write", "routing", "gateway", "pfsense"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions=_GATEWAY_ADD_LLM_INSTRUCTIONS,
    ),
    PfSenseOp(
        op_id="pfsense.route.static.add",
        handler_attr="route_static_add",
        summary="Append a static route (CIDR -> gateway) to config.xml (guarded, idempotent).",
        description=(
            "Reads ``/cf/conf/config.xml`` for the ``<staticroutes>`` block, "
            "requires the named gateway to already exist, and if no route for "
            "the same canonical ``network`` exists, stages a ``pfSsh.php "
            "playback`` fragment that appends one (network -> gateway name), "
            "calls ``write_config()`` then ``system_routing_configure()`` to "
            "apply it, and reads the config back to confirm. A route for a "
            "network already present is a reported no-op "
            "(``existed_before=true``, ``applied=false``) -- never a "
            "duplicate. Surgical: touches only ``staticroutes/route``. The "
            "CIDR is parsed and canonicalised, the gateway name allowlisted, "
            "before any SSH round-trip. safety_level=caution."
        ),
        parameter_schema=_ROUTE_ADD_PARAMETER_SCHEMA,
        response_schema=_ROUTE_ADD_RESPONSE_SCHEMA,
        group_key="routing",
        tags=("write", "routing", "static-route", "pfsense"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions=_ROUTE_ADD_LLM_INSTRUCTIONS,
    ),
)
