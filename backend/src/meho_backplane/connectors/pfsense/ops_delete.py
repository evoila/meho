# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: declarative op module (parsers + small handlers + agent-facing
# op metadata / JSON schemas / llm_instructions) mirroring the ops_write.py sibling's
# single-module-per-op-group convention; all functions are small + low-complexity.

"""pfSense governed destructive deletes -- NAT-rule + alias delete (#3232).

Adds the connector's first two ``safety_level="destructive"`` typed ops:

* ``pfsense.nat.delete`` -- permanently delete ONE port-forward NAT rule
  (``config.xml`` ``<nat><rule>``) identified by its stable ``<tracker>``
  id. Refuses fail-closed on 0 matches (``not_found``) or >1 matches
  (``ambiguous`` -- a corrupt config with a duplicated tracker), never
  guesses by position or description.
* ``pfsense.alias.delete`` -- permanently delete ONE firewall alias
  (``config.xml`` ``<aliases><alias>``) by exact ``<name>``. Refuses
  fail-closed (``referenced``) when the alias is still referenced by any
  filter rule, NAT rule (port-forward / outbound / 1:1), or other alias --
  naming the referrers -- exactly as pfSense's own delete guard does.

Governed-delete tier (the whole point of #3232)
-----------------------------------------------

Both ops are ``safety_level="destructive"`` + ``requires_approval=True``,
so they ride the hardest gate MEHO has (decision
``docs/decisions/governed-delete-operations.md``): mandatory human approval
always (no agent path, no standing grant -- both op-ids match the
single-source ``*.delete`` delete-shaped classifier
:data:`~meho_backplane.settings._DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS`
*and* carry the ``destructive`` tag -- no self-approval even under
break-glass), a mandatory preview-hash binding, and a mandatory
blast-radius statement. The blast-radius builders here name the exact rule
/ alias (identity + summary) so the four-eyes approver reads *precisely
what dies* before deciding. These run against a **shared perimeter
firewall**, so per-object identity binding -- never bulk, never
device-wide -- is load-bearing (decision requirement 3).

Why config.xml, not ``pfctl``
-----------------------------

The read op ``pfsense.nat.rules`` parses ``pfctl -sn`` -- the *runtime,
compiled* pf table, which carries no stable per-rule identity. A surgical,
identity-bound delete must operate on the **source of truth**
(``/cf/conf/config.xml``), where each port-forward rule carries a stable
``<tracker>`` (a creation-time unix-timestamp id, stable across reordering)
and each alias a unique ``<name>``. The mutation runs through the same
``pfSsh.php playback`` idiom the ``ops_write`` additive writes use, and
applies via ``filter_configure()`` (NAT rules and aliases both compile into
the pf ruleset), then reads ``config.xml`` back to verify.

Surgical single-object contract (adversarial risk #1)
-----------------------------------------------------

The one risk on config-array manipulation is deleting *more than* the one
identified object. Three independent guards enforce single-object deletion:

1. **Python pre-check.** The handler parses ``config.xml`` and requires
   **exactly one** match before staging any playback; 0 → ``not_found``,
   >1 → ``ambiguous`` (fail-closed, refuse).
2. **PHP fragment guard.** The playback removes by exact tracker / name and
   ``write_config()`` runs **only when exactly one** entry was removed
   (``$meho_removed === 1``); a 0- or 2-match removal persists nothing.
3. **Read-back verification.** After the playback the handler re-reads
   ``config.xml`` and asserts the object is **absent** *and* the surviving
   count dropped by **exactly one** -- a bulk deletion (whole array) or a
   no-op is caught and raises rather than reporting a false success.

Stale-approval safety (adversarial risk #5). Identity is the tracker /
name, re-matched at *dispatch* time (post-approval). A rule
deleted-and-recreated between preview and approval gets a **new** tracker,
so the approved delete (old tracker) resolves to ``not_found`` and the
recreated rule is untouched; the #3197 preview-hash binds the params so the
tracker cannot be swapped after approval.

Injection safety
----------------

The tracker is validated digits-only and the alias name against a
word-character allowlist before any SSH round-trip; both are emitted
through :func:`~meho_backplane.connectors.pfsense.ops_write._php_squote`
inside the quoted-heredoc fragment. Neither carries shell metacharacters
or PHP string-literal terminators.

References
----------

* Task: #3232. Parent decision: ``docs/decisions/governed-delete-operations.md``.
* Precedent (first governed delete): #3198 / ``vmware.composite.vm.destroy``.
* Additive-write precedent: :mod:`meho_backplane.connectors.pfsense.ops_write`.
* pfSense config.xml tracker semantics:
  https://forum.netgate.com/topic/82877/purpose-of-tracker-on-pfsense-config-rules
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from defusedxml.ElementTree import ParseError, fromstring

from meho_backplane.connectors.pfsense.ops import PfSenseOp
from meho_backplane.connectors.pfsense.ops_write import (
    _apply_playback,
    _php_squote,
    _read_config_xml,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "DELETE_OPS",
    "find_alias_references",
    "parse_aliases_xml",
    "parse_nat_port_forwards_xml",
    "pfsense_alias_delete",
    "pfsense_nat_delete",
    "register_pfsense_delete_preview_builders",
]

# A pfSense rule ``<tracker>`` is an integer creation-time id (``time()``
# based). Digits-only -- injection-safe, and rejects a caller trying to
# pass a positional index or a description as the identity.
_TRACKER_RE = re.compile(r"^[0-9]{1,20}$")

# pfSense alias names are word characters (its own validator is
# ``^[a-zA-Z0-9_]+$``). Bounded + metacharacter-free, so the value is safe
# to emit into the playback fragment and to compare as a reference token.
_ALIAS_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_tracker(value: Any) -> str:
    """Return *value* if it is a valid NAT-rule tracker id, else raise."""
    if not isinstance(value, str) or not _TRACKER_RE.match(value):
        raise ValueError(
            "tracker must be the rule's stable numeric <tracker> id "
            f"(^[0-9]{{1,20}}$); got {value!r}. Read the current NAT rules "
            "first to find a rule's tracker -- this op never deletes by "
            "position or description."
        )
    return value


def _validate_alias_name(value: Any) -> str:
    """Return *value* if it is a valid pfSense alias name, else raise."""
    if not isinstance(value, str) or not _ALIAS_NAME_RE.match(value):
        raise ValueError(
            "alias name must match ^[A-Za-z0-9_]{1,64}$ (alphanumeric, "
            f"underscore); got {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# config.xml parsing
# ---------------------------------------------------------------------------


def _child_text(element: Any, tag: str) -> str | None:
    """Return the text of *tag* under *element*, or ``None``.

    *element* is a ``defusedxml``-parsed node; defusedxml ships no type
    stubs, so it surfaces as ``Any`` (annotating it with the stdlib
    ``Element`` type would re-introduce the ``xml.etree`` import Semgrep
    flags as an XXE vector). Mirrors
    :func:`meho_backplane.connectors.pfsense.ops_write._child_text`.
    """
    child = element.find(tag)
    return child.text if child is not None else None


def _flatten_endpoint(element: Any) -> str:
    """Summarise a pfSense ``<source>`` / ``<destination>`` block to a string.

    pfSense serialises a rule endpoint as a nested block -- ``<any></any>``,
    ``<network>wanip</network>``, or ``<address>ALIAS_OR_IP</address>`` --
    with an optional ``<port>``. Flattened to a compact human string
    (``"any"`` / ``"10.0.0.0/24:443"`` / ``"WEB_SERVERS"``) for the
    blast-radius summary the approver reads. Returns ``"any"`` for a missing
    or empty block (the pfSense default).
    """
    if element is None:
        return "any"
    if element.find("any") is not None:
        base = "any"
    else:
        base = _child_text(element, "network") or _child_text(element, "address") or "any"
    port = _child_text(element, "port")
    return f"{base}:{port}" if port else base


def parse_nat_port_forwards_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse the ``<nat>`` port-forward rules from ``/cf/conf/config.xml``.

    Returns one dict per ``<nat><rule>`` (port-forward / ``rdr``) child, in
    document order, each carrying the fields the delete identity + the
    blast-radius summary need:

    .. code-block:: python

        {
            "tracker": "1585165239",      # the stable delete identity (or None)
            "associated_rule_id": "nat_...",  # linked filter rule id, or None
            "interface": "wan",
            "protocol": "tcp",
            "source": "any",
            "destination": "wanip:443",
            "target": "10.0.0.5",
            "local_port": "443",
            "descr": "web",
            "position": 1,                # 1-based, for the operator's reference only
        }

    ``position`` is informational -- it is **never** the delete key. Returns
    an empty list on any parse failure or an absent ``<nat>`` block. Only
    the port-forward ``<nat><rule>`` chain is parsed (outbound / 1:1 NAT are
    a separate config sub-tree, out of this op's scope).

    >>> xml = (
    ...     "<pfsense><nat><rule><tracker>1585165239</tracker>"
    ...     "<interface>wan</interface><protocol>tcp</protocol>"
    ...     "<target>10.0.0.5</target><local-port>443</local-port>"
    ...     "<destination><network>wanip</network><port>443</port></destination>"
    ...     "</rule></nat></pfsense>"
    ... )
    >>> parse_nat_port_forwards_xml(xml)[0]["tracker"]
    '1585165239'
    >>> parse_nat_port_forwards_xml(xml)[0]["destination"]
    'wanip:443'
    """
    if not xml_text.strip():
        return []
    try:
        root = fromstring(xml_text)
    except ParseError:
        return []
    nat_root = root if root.tag == "nat" else root.find(".//nat")
    if nat_root is None:
        return []
    rows: list[dict[str, Any]] = []
    for position, item in enumerate(nat_root.findall("rule"), start=1):
        rows.append(
            {
                "tracker": _child_text(item, "tracker"),
                "associated_rule_id": _child_text(item, "associated-rule-id"),
                "interface": _child_text(item, "interface"),
                "protocol": _child_text(item, "protocol"),
                "source": _flatten_endpoint(item.find("source")),
                "destination": _flatten_endpoint(item.find("destination")),
                "target": _child_text(item, "target"),
                "local_port": _child_text(item, "local-port"),
                "descr": _child_text(item, "descr"),
                "position": position,
            }
        )
    return rows


def parse_aliases_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse the ``<aliases>`` block from ``/cf/conf/config.xml``.

    Returns one dict per ``<alias>`` child with ``name``, ``type``,
    ``address``, and ``descr`` keys (``None`` for a missing tag). Returns an
    empty list on any parse failure or an absent ``<aliases>`` block.

    >>> xml = (
    ...     "<pfsense><aliases><alias><name>WEB</name><type>host</type>"
    ...     "<address>192.0.2.10</address></alias></aliases></pfsense>"
    ... )
    >>> parse_aliases_xml(xml)[0]["name"]
    'WEB'
    """
    if not xml_text.strip():
        return []
    try:
        root = fromstring(xml_text)
    except ParseError:
        return []
    aliases_root = root if root.tag == "aliases" else root.find(".//aliases")
    if aliases_root is None:
        return []
    rows: list[dict[str, Any]] = []
    for item in aliases_root.findall("alias"):
        rows.append(
            {
                "name": _child_text(item, "name"),
                "type": _child_text(item, "type"),
                "address": _child_text(item, "address"),
                "descr": _child_text(item, "descr"),
            }
        )
    return rows


def _endpoint_tokens(element: Any) -> list[str]:
    """Return the alias-accepting field values of a ``<source>``/``<destination>``.

    A rule endpoint references an alias by putting the alias name in its
    ``<address>`` (address alias) or ``<port>`` (port alias). Both are
    collected so the reference scan catches either.
    """
    if element is None:
        return []
    tokens: list[str] = []
    for tag in ("address", "port"):
        val = _child_text(element, tag)
        if val:
            tokens.append(val)
    return tokens


def _reference_row(kind: str, element: Any, name_from: str = "tracker") -> dict[str, Any]:
    """Build a named referrer row for the fail-closed refusal message."""
    return {
        "kind": kind,
        "id": _child_text(element, name_from),
        "descr": _child_text(element, "descr"),
    }


def find_alias_references(xml_text: str, alias_name: str) -> list[dict[str, Any]]:
    """Return the config objects that reference *alias_name* (fail-closed guard).

    Scans the same reference surface pfSense's own delete guard checks, so a
    delete that pfSense would refuse ("Cannot delete alias. Currently in use
    by ...") is refused here too -- **before** any mutation:

    * **Other aliases** -- a nested alias whose ``<address>`` lists
      *alias_name* as a space-separated token.
    * **Filter rules** (``<filter><rule>``) -- source / destination
      ``<address>`` or ``<port>``.
    * **NAT port-forward rules** (``<nat><rule>``) -- source / destination
      ``<address>`` / ``<port>``, ``<target>``, ``<local-port>``.
    * **NAT outbound rules** (``<nat><outbound><rule>``) -- source /
      destination ``<network>`` / ``<port>``, ``<sourceport>``,
      ``<dstport>``, ``<target>``.
    * **1:1 NAT** (``<nat><onetoone>``) -- source / destination
      ``<address>`` and ``<external>``.

    Returns one ``{kind, id, descr}`` row per referrer so the refusal can
    name every one (``id`` is the rule tracker or the referring alias name).
    An empty list means the alias is safe to delete. Returns an empty list
    on parse failure only after a successful parse would have found nothing;
    a genuinely unparseable config yields ``[]`` here but the handler's own
    ``config.xml`` read-and-parse guards catch a broken config first.
    """
    if not xml_text.strip():
        return []
    try:
        root = fromstring(xml_text)
    except ParseError:
        return []
    refs: list[dict[str, Any]] = []

    # Nested aliases: another alias whose address lists this name as a token.
    aliases_root = root if root.tag == "aliases" else root.find(".//aliases")
    if aliases_root is not None:
        for alias in aliases_root.findall("alias"):
            this_name = _child_text(alias, "name")
            if this_name == alias_name:
                continue  # the alias being deleted -- not a self-reference
            address = _child_text(alias, "address") or ""
            if alias_name in address.split():
                refs.append(
                    {"kind": "alias", "id": this_name, "descr": _child_text(alias, "descr")}
                )

    # Filter rules.
    filter_root = root.find(".//filter")
    if filter_root is not None:
        for rule in filter_root.findall("rule"):
            tokens = _endpoint_tokens(rule.find("source")) + _endpoint_tokens(
                rule.find("destination")
            )
            if alias_name in tokens:
                refs.append(_reference_row("filter_rule", rule))

    # NAT: port-forward, outbound, and 1:1.
    nat_root = root.find(".//nat")
    if nat_root is not None:
        for rule in nat_root.findall("rule"):
            tokens = (
                _endpoint_tokens(rule.find("source"))
                + _endpoint_tokens(rule.find("destination"))
                + [t for t in (_child_text(rule, "target"), _child_text(rule, "local-port")) if t]
            )
            if alias_name in tokens:
                refs.append(_reference_row("nat_rule", rule))
        outbound = nat_root.find("outbound")
        if outbound is not None:
            for rule in outbound.findall("rule"):
                tokens = [
                    t
                    for t in (
                        _child_text(rule.find("source"), "network")
                        if rule.find("source") is not None
                        else None,
                        _child_text(rule.find("destination"), "network")
                        if rule.find("destination") is not None
                        else None,
                        _child_text(rule, "sourceport"),
                        _child_text(rule, "dstport"),
                        _child_text(rule, "target"),
                    )
                    if t
                ]
                if alias_name in tokens:
                    refs.append(_reference_row("nat_outbound_rule", rule))
        for rule in nat_root.findall("onetoone"):
            tokens = _endpoint_tokens(rule.find("source")) + _endpoint_tokens(
                rule.find("destination")
            )
            external = _child_text(rule, "external")
            if external:
                tokens.append(external)
            if alias_name in tokens:
                refs.append(_reference_row("nat_onetoone_rule", rule))

    return refs


# ---------------------------------------------------------------------------
# pfSsh.php playback fragment construction
# ---------------------------------------------------------------------------


def _build_nat_delete_playback(tracker: str) -> str:
    """Return the raw-PHP playback fragment that deletes ONE NAT rule by tracker.

    Removes only the ``<nat><rule>`` whose ``<tracker>`` matches, reindexes
    the array, and persists **only when exactly one** entry was removed
    (``$meho_removed === 1``) -- so a concurrent 0- or 2-match state
    persists nothing. Applies via ``filter_configure()`` (NAT rules compile
    into the pf ruleset).
    """
    lines = [
        "global $config;",
        f"$meho_tracker = {_php_squote(tracker)};",
        "$meho_removed = 0;",
        "if (is_array($config['nat']) && is_array($config['nat']['rule'])) {",
        "  foreach ($config['nat']['rule'] as $meho_i => $meho_r) {",
        "    if (isset($meho_r['tracker']) && (string)$meho_r['tracker'] === $meho_tracker) {",
        "      unset($config['nat']['rule'][$meho_i]);",
        "      $meho_removed++;",
        "    }",
        "  }",
        "  if ($meho_removed > 0) {",
        "    $config['nat']['rule'] = array_values($config['nat']['rule']);",
        "  }",
        "}",
        "if ($meho_removed === 1) {",
        f"  write_config({_php_squote('meho: delete nat rule tracker ' + tracker)});",
        "  filter_configure();",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _build_alias_delete_playback(name: str) -> str:
    """Return the raw-PHP playback fragment that deletes ONE alias by name.

    Removes only the ``<aliases><alias>`` whose ``<name>`` matches,
    reindexes, and persists **only when exactly one** entry was removed.
    Applies via ``filter_configure()`` (aliases compile into pf tables).
    """
    lines = [
        "global $config;",
        f"$meho_name = {_php_squote(name)};",
        "$meho_removed = 0;",
        "if (is_array($config['aliases']) && is_array($config['aliases']['alias'])) {",
        "  foreach ($config['aliases']['alias'] as $meho_i => $meho_a) {",
        "    if (isset($meho_a['name']) && (string)$meho_a['name'] === $meho_name) {",
        "      unset($config['aliases']['alias'][$meho_i]);",
        "      $meho_removed++;",
        "    }",
        "  }",
        "  if ($meho_removed > 0) {",
        "    $config['aliases']['alias'] = array_values($config['aliases']['alias']);",
        "  }",
        "}",
        "if ($meho_removed === 1) {",
        f"  write_config({_php_squote('meho: delete alias ' + name)});",
        "  filter_configure();",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _script_token(value: str) -> str:
    """Return a filename-safe token for a playback script name."""
    return re.sub(r"[^A-Za-z0-9]", "_", value)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def pfsense_nat_delete(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.nat.delete`` -- governed single-rule NAT delete.

    Sequence: validate ``tracker`` -> read ``config.xml`` -> match
    port-forward rules by tracker. **Fail-closed pre-check:** 0 matches ->
    ``not_found``, >1 matches -> ``ambiguous`` (never guess). Exactly one ->
    stage + play back the delete-by-tracker fragment (which persists only on
    a single removal), then read ``config.xml`` back and verify the rule is
    **absent** *and* the surviving rule count dropped by **exactly one** --
    proving a surgical single-object delete, never a bulk one. Runs at
    dispatch time (post-approval), so a rule recreated with a new tracker
    between park and approval is a different object and resolves to
    ``not_found``.

    Returns ``{op_class, resource, status, tracker, matched, removed,
    rules_before, rules_after, verified, guidance}``. ``removed`` carries
    the deleted rule's identity on success (else ``None``).
    """
    tracker = _validate_tracker(params.get("tracker"))

    before = await _read_config_xml(self, target, operator)
    rules_before = parse_nat_port_forwards_xml(before)
    matches = [r for r in rules_before if r.get("tracker") == tracker]

    result: dict[str, Any] = {
        "op_class": "delete",
        "resource": "nat_rule",
        "tracker": tracker,
        "matched": len(matches),
        "removed": None,
        "rules_before": len(rules_before),
        "rules_after": None,
        "verified": False,
    }
    if not matches:
        return {
            **result,
            "status": "not_found",
            "guidance": (
                f"no port-forward NAT rule with tracker {tracker!r} in config.xml "
                f"({len(rules_before)} port-forward rule(s) present); nothing deleted"
            ),
        }
    if len(matches) > 1:
        return {
            **result,
            "status": "ambiguous",
            "guidance": (
                f"{len(matches)} port-forward NAT rules share tracker {tracker!r} "
                "(corrupt config); refusing to delete -- an operator must "
                "disambiguate the duplicate trackers first"
            ),
        }

    rule = matches[0]
    await _apply_playback(
        self,
        target,
        script_name=f"meho_nat_delete_{_script_token(tracker)}",
        script_body=_build_nat_delete_playback(tracker),
        operator=operator,
    )

    after = await _read_config_xml(self, target, operator)
    rules_after = parse_nat_port_forwards_xml(after)
    still_present = any(r.get("tracker") == tracker for r in rules_after)
    removed_exactly_one = len(rules_after) == len(rules_before) - 1
    if still_present or not removed_exactly_one:
        raise RuntimeError(
            f"nat.delete verification failed for tracker {tracker!r}: "
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
        "guidance": None,
    }


async def pfsense_alias_delete(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.alias.delete`` -- governed single-alias delete.

    Sequence: validate ``name`` -> read ``config.xml`` -> match aliases by
    exact name. **Fail-closed pre-checks:** 0 matches -> ``not_found``, >1 ->
    ``ambiguous``; then a **dependency check** -- if any filter rule, NAT
    rule (port-forward / outbound / 1:1), or other alias still references the
    name, refuse with ``referenced`` and name every referrer, deleting
    nothing (mirrors pfSense's own in-use guard). Only a single,
    unreferenced alias is deleted: stage + play back the delete-by-name
    fragment (persists only on a single removal), then read ``config.xml``
    back and verify the alias is **absent** *and* the alias count dropped by
    **exactly one**. Runs at dispatch time (post-approval), so a reference
    added between park and approval is still caught.

    Returns ``{op_class, resource, status, name, matched, removed,
    references, reference_count, aliases_before, aliases_after, verified,
    guidance}``.
    """
    name = _validate_alias_name(params.get("name"))

    before = await _read_config_xml(self, target, operator)
    aliases_before = parse_aliases_xml(before)
    matches = [a for a in aliases_before if a.get("name") == name]

    result: dict[str, Any] = {
        "op_class": "delete",
        "resource": "alias",
        "name": name,
        "matched": len(matches),
        "removed": None,
        "references": [],
        "reference_count": 0,
        "aliases_before": len(aliases_before),
        "aliases_after": None,
        "verified": False,
    }
    if not matches:
        return {
            **result,
            "status": "not_found",
            "guidance": (
                f"no alias named {name!r} in config.xml "
                f"({len(aliases_before)} alias(es) present); nothing deleted"
            ),
        }
    if len(matches) > 1:
        return {
            **result,
            "status": "ambiguous",
            "guidance": (
                f"{len(matches)} aliases share the name {name!r} (corrupt config); "
                "refusing to delete -- an operator must disambiguate first"
            ),
        }

    references = find_alias_references(before, name)
    if references:
        referrers = ", ".join(
            f"{r['kind']}({r.get('id') or '?'}{': ' + r['descr'] if r.get('descr') else ''})"
            for r in references
        )
        return {
            **result,
            "status": "referenced",
            "references": references,
            "reference_count": len(references),
            "guidance": (
                f"alias {name!r} is still referenced by {len(references)} object(s) "
                f"[{referrers}]; refusing to delete (fail-closed) -- remove or "
                "repoint those references first"
            ),
        }

    alias = matches[0]
    await _apply_playback(
        self,
        target,
        script_name=f"meho_alias_delete_{_script_token(name)}",
        script_body=_build_alias_delete_playback(name),
        operator=operator,
    )

    after = await _read_config_xml(self, target, operator)
    aliases_after = parse_aliases_xml(after)
    still_present = any(a.get("name") == name for a in aliases_after)
    removed_exactly_one = len(aliases_after) == len(aliases_before) - 1
    if still_present or not removed_exactly_one:
        raise RuntimeError(
            f"alias.delete verification failed for {name!r}: present_after={still_present}, "
            f"aliases {len(aliases_before)}->{len(aliases_after)} (expected exactly one "
            "fewer); the pfSsh.php playback did not persist a clean single-alias delete"
        )
    return {
        **result,
        "status": "deleted",
        "removed": alias,
        "aliases_after": len(aliases_after),
        "verified": True,
        "guidance": None,
    }


# ---------------------------------------------------------------------------
# Blast-radius preview builders (#3197 mandatory destructive-tier block)
# ---------------------------------------------------------------------------


async def _read_config_for_preview(ctx: PreviewContext) -> str | None:
    """Read ``config.xml`` for a preview builder; ``None`` on any failure.

    The blast-radius builders are fail-soft: a connector that cannot be
    resolved, or a ``config.xml`` read that faults, declines (``None``) ->
    the park is refused ``blast_radius_required`` (fail-closed), never
    executed.
    """
    if ctx.connector_instance is None:
        return None
    try:
        return await _read_config_xml(
            ctx.connector_instance,  # type: ignore[arg-type]  # PfSenseConnector at runtime
            ctx.target,
            ctx.operator,
        )
    except Exception:
        # Fail-soft: any read fault (transport, non-zero cat, malformed) declines
        # the preview -> the park is refused ``blast_radius_required`` (fail-closed).
        return None


async def _nat_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast-radius builder for ``pfsense.nat.delete`` (#3197 requirement 3).

    Live-reads ``config.xml`` and, when the tracker resolves to **exactly
    one** port-forward rule, populates the mandatory blast-radius block:
    the object identity (tracker + interface / proto / source / destination
    / target / port summary) and, as an enumerated child, the associated
    filter rule (if any) that this op leaves in place. Declines (``None`` ->
    park refused ``blast_radius_required``, fail-closed) when the connector
    can't be read or the tracker does not resolve to a unique rule.
    """
    tracker = ctx.params.get("tracker")
    if not isinstance(tracker, str) or not _TRACKER_RE.match(tracker):
        return None
    xml_text = await _read_config_for_preview(ctx)
    if xml_text is None:
        return None
    matches = [r for r in parse_nat_port_forwards_xml(xml_text) if r.get("tracker") == tracker]
    if len(matches) != 1:
        return None
    rule = matches[0]
    children: list[dict[str, Any]] = []
    if rule.get("associated_rule_id"):
        children.append(
            {
                "kind": "associated_filter_rule",
                "id": rule["associated_rule_id"],
                "note": "left in place -- this op deletes only the NAT rule",
            }
        )
    return {
        "blast_radius": {
            "object": {
                "kind": "nat_rule",
                "tracker": tracker,
                "interface": rule.get("interface"),
                "protocol": rule.get("protocol"),
                "source": rule.get("source"),
                "destination": rule.get("destination"),
                "target": rule.get("target"),
                "local_port": rule.get("local_port"),
                "descr": rule.get("descr"),
                "position": rule.get("position"),
            },
            "children": children,
            "irreversibility": "permanent",
        },
    }


async def _alias_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast-radius builder for ``pfsense.alias.delete`` (#3197 requirement 3).

    Live-reads ``config.xml`` and, when the name resolves to **exactly one**
    alias, populates the mandatory blast-radius block: the object identity
    (name / type / address / descr) plus, as enumerated children, every
    object that references the alias -- with ``reference_count`` surfaced so
    the approver sees at a glance that the delete will be **refused** at
    execution (fail-closed) unless those references are removed first.
    Declines (``None`` -> park refused ``blast_radius_required``) when the
    connector can't be read or the name does not resolve to a unique alias.
    """
    name = ctx.params.get("name")
    if not isinstance(name, str) or not _ALIAS_NAME_RE.match(name):
        return None
    xml_text = await _read_config_for_preview(ctx)
    if xml_text is None:
        return None
    matches = [a for a in parse_aliases_xml(xml_text) if a.get("name") == name]
    if len(matches) != 1:
        return None
    alias = matches[0]
    references = find_alias_references(xml_text, name)
    children = [
        {"kind": "reference", "ref_kind": r["kind"], "id": r.get("id"), "descr": r.get("descr")}
        for r in references
    ]
    return {
        "blast_radius": {
            "object": {
                "kind": "alias",
                "name": name,
                "type": alias.get("type"),
                "address": alias.get("address"),
                "descr": alias.get("descr"),
            },
            "children": children,
            "reference_count": len(references),
            "irreversibility": "permanent",
        },
    }


def register_pfsense_delete_preview_builders() -> None:
    """Wire the destructive-delete blast-radius builders. Import-time.

    Mirrors :func:`meho_backplane.operations._preview._register_builtin_builders`
    and the argocd ``ops_write_preview`` side-effect: called at module import
    (the bottom of this module) so the builders are registered by the time
    the connector package finishes importing and any dispatch can park.
    Idempotent -- re-registration overwrites.
    """
    register_preview_builder("pfsense.nat.delete", _nat_delete_preview)
    register_preview_builder("pfsense.alias.delete", _alias_delete_preview)


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

#: Curated ``when_to_use`` for the ``nat`` group's destructive delete. The
#: authoritative group-level copy lives in ``_WHEN_TO_USE_BY_GROUP`` in
#: ``connector.py`` (the registration walk reads that); this is the per-op
#: ``llm_instructions.when_to_use``.
_NAT_DELETE_WHEN_TO_USE = (
    "Permanently delete ONE pfSense port-forward NAT rule, identified by its "
    "stable ``tracker`` id (read the current rules first to find it). "
    "GOVERNED DESTRUCTIVE DELETE: safety_level=destructive, requires mandatory "
    "human approval (no agent path, no standing grant, no self-approval), a "
    "preview-hash binding, and a blast-radius statement the approver reads. "
    "Fail-closed: refuses on 0 matches (``not_found``) or >1 (``ambiguous``); "
    "never deletes by position or description; deletes exactly one rule and "
    "verifies the count dropped by one. Runs against a SHARED perimeter "
    "firewall -- per-rule identity binding is the point. Leaves any associated "
    "filter rule in place (surfaced in the blast radius)."
)

#: Curated ``when_to_use`` for the ``alias`` group's destructive delete.
_ALIAS_DELETE_WHEN_TO_USE = (
    "Permanently delete ONE pfSense firewall alias by exact name. GOVERNED "
    "DESTRUCTIVE DELETE (same tier + gate as ``pfsense.nat.delete``). "
    "Fail-closed dependency check: refuses (``referenced``) when the alias is "
    "still used by any filter rule, NAT rule (port-forward / outbound / 1:1), "
    "or other alias, naming every referrer -- remove or repoint those first. "
    "Also refuses on 0 matches (``not_found``) or >1 (``ambiguous``). Deletes "
    "exactly one alias and verifies the count dropped by one."
)

_NAT_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tracker": {
            "type": "string",
            "pattern": "^[0-9]{1,20}$",
            "description": (
                "The stable ``<tracker>`` id of the ONE port-forward NAT rule "
                "to delete (a creation-time unix-timestamp id, stable across "
                "reordering). The delete identity -- never a position or "
                "description. A tracker matching 0 rules is ``not_found``; >1 "
                "(corrupt config) is ``ambiguous`` and refused."
            ),
        },
    },
    "required": ["tracker"],
    "additionalProperties": False,
}

_NAT_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op_class": {"type": "string", "enum": ["delete"]},
        "resource": {"type": "string", "enum": ["nat_rule"]},
        "status": {
            "type": "string",
            "enum": ["deleted", "not_found", "ambiguous"],
            "description": (
                "``deleted`` on a verified single-rule delete; ``not_found`` "
                "when no rule carries the tracker; ``ambiguous`` when >1 do "
                "(refused, fail-closed)."
            ),
        },
        "tracker": {"type": "string"},
        "matched": {"type": "integer"},
        "removed": {"type": ["object", "null"]},
        "rules_before": {"type": ["integer", "null"]},
        "rules_after": {"type": ["integer", "null"]},
        "verified": {"type": "boolean"},
        "guidance": {"type": ["string", "null"]},
    },
    "required": ["op_class", "resource", "status", "tracker", "matched", "verified"],
    "additionalProperties": False,
}

_NAT_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": _NAT_DELETE_WHEN_TO_USE,
    "parameter_hints": {
        "tracker": (
            "Required. The rule's stable numeric <tracker> id from config.xml "
            "(not a position or description)."
        ),
    },
    "output_shape": (
        "``{op_class: 'delete', resource: 'nat_rule', status, tracker, "
        "matched, removed, rules_before, rules_after, verified, guidance}``. "
        "``status`` is ``deleted`` / ``not_found`` / ``ambiguous``. On "
        "``deleted``, ``removed`` carries the deleted rule's identity and "
        "``verified`` is true (config read back: rule absent, count -1)."
    ),
}

_ALIAS_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_]{1,64}$",
            "description": (
                "Exact name of the ONE firewall alias to delete. Refused "
                "(``referenced``) when still in use by any rule or other "
                "alias; ``not_found`` when absent."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

_ALIAS_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op_class": {"type": "string", "enum": ["delete"]},
        "resource": {"type": "string", "enum": ["alias"]},
        "status": {
            "type": "string",
            "enum": ["deleted", "not_found", "ambiguous", "referenced"],
            "description": (
                "``deleted`` on a verified single-alias delete; ``not_found`` "
                "when absent; ``ambiguous`` on a duplicate name; ``referenced`` "
                "when still in use (refused, fail-closed, with the referrers "
                "named in ``references``)."
            ),
        },
        "name": {"type": "string"},
        "matched": {"type": "integer"},
        "removed": {"type": ["object", "null"]},
        "references": {"type": "array", "items": {"type": "object"}},
        "reference_count": {"type": "integer"},
        "aliases_before": {"type": ["integer", "null"]},
        "aliases_after": {"type": ["integer", "null"]},
        "verified": {"type": "boolean"},
        "guidance": {"type": ["string", "null"]},
    },
    "required": ["op_class", "resource", "status", "name", "matched", "verified"],
    "additionalProperties": False,
}

_ALIAS_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": _ALIAS_DELETE_WHEN_TO_USE,
    "parameter_hints": {
        "name": "Required. Exact alias name (alphanumeric / underscore).",
    },
    "output_shape": (
        "``{op_class: 'delete', resource: 'alias', status, name, matched, "
        "removed, references, reference_count, aliases_before, aliases_after, "
        "verified, guidance}``. On ``referenced``, ``references`` names every "
        "object still using the alias. On ``deleted``, ``removed`` carries "
        "the deleted alias and ``verified`` is true."
    ),
}

#: The governed destructive-delete ops :class:`PfSenseConnector` registers
#: alongside the read + additive-write surface. Both are
#: ``safety_level='destructive'`` / ``requires_approval=True`` (#3232) --
#: the connector's first ops on the governed-delete tier.
DELETE_OPS: tuple[PfSenseOp, ...] = (
    PfSenseOp(
        op_id="pfsense.nat.delete",
        handler_attr="nat_delete",
        summary="Permanently delete ONE port-forward NAT rule by stable tracker id (governed).",
        description=(
            "Governed destructive delete of a single pfSense port-forward NAT "
            "rule (``config.xml`` ``<nat><rule>``), identified by its stable "
            "``<tracker>`` id. safety_level=destructive: mandatory human "
            "approval (no agent path, no standing grant, no self-approval even "
            "under break-glass), a mandatory preview-hash binding, and a "
            "mandatory blast-radius statement (the exact rule's identity + "
            "interface / proto / source / destination / target / port) the "
            "four-eyes approver reads before deciding. Fail-closed: 0 matches "
            "-> ``not_found``, >1 -> ``ambiguous`` (never guesses by position "
            "or description). Deletes exactly one rule via ``pfSsh.php "
            "playback`` (``write_config`` + ``filter_configure``) and verifies "
            "the rule is gone and the count dropped by exactly one. Leaves any "
            "associated filter rule in place (surfaced in the blast radius). "
            "Operates on a SHARED perimeter firewall -- never bulk, never "
            "device-wide."
        ),
        parameter_schema=_NAT_DELETE_PARAMETER_SCHEMA,
        response_schema=_NAT_DELETE_RESPONSE_SCHEMA,
        group_key="nat",
        tags=("write", "nat", "delete", "destructive", "pfsense"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=_NAT_DELETE_LLM_INSTRUCTIONS,
    ),
    PfSenseOp(
        op_id="pfsense.alias.delete",
        handler_attr="alias_delete",
        summary="Permanently delete ONE firewall alias by exact name (governed, fail-closed).",
        description=(
            "Governed destructive delete of a single pfSense firewall alias "
            "(``config.xml`` ``<aliases><alias>``) by exact ``<name>``. "
            "safety_level=destructive (same tier + gate as "
            "``pfsense.nat.delete``). Fail-closed dependency check: refuses "
            "(``referenced``) when the alias is still used by any filter rule, "
            "NAT rule (port-forward / outbound / 1:1), or other alias -- "
            "naming every referrer -- so a still-referenced alias is never "
            "orphaned out from under a live rule (mirrors pfSense's own "
            "in-use guard). Also refuses on 0 matches (``not_found``) or >1 "
            "(``ambiguous``). The blast-radius statement names the alias + its "
            "reference count. Deletes exactly one alias via ``pfSsh.php "
            "playback`` (``write_config`` + ``filter_configure``) and verifies "
            "it is gone and the count dropped by exactly one."
        ),
        parameter_schema=_ALIAS_DELETE_PARAMETER_SCHEMA,
        response_schema=_ALIAS_DELETE_RESPONSE_SCHEMA,
        group_key="alias",
        tags=("write", "alias", "delete", "destructive", "pfsense"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=_ALIAS_DELETE_LLM_INSTRUCTIONS,
    ),
)


# Import-time side-effect: register the blast-radius builders (mirrors the
# argocd ``ops_write_preview`` import and ``_preview._register_builtin_builders``).
register_pfsense_delete_preview_builders()
