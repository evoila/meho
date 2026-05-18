# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""bind9 record ops -- read (``record.get``) + writes (``record.add`` / ``record.remove``).

G3.4-T2 (#588) of Initiative #367 landed the read op (``dig @localhost
<fqdn> [<type>]``). G3.4-T3 (#589) adds the symmetric write ops:

* ``bind9.record.add <fqdn> <ip> [--zone <name>] [--type A|AAAA]`` --
  atomic stage-validate-commit-reload-verify-rollback against the
  affected zonefile via :mod:`._atomic`. Verify predicate runs ``dig
  @localhost <fqdn>`` and asserts the new IP appears in the answer.
  ``safety_level=caution`` (mutation; the production-path gate is
  G7/G10 policy territory).
* ``bind9.record.remove <fqdn> [--zone <name>]`` -- symmetric remove
  with verify predicate = ``dig`` no longer resolves the FQDN.

``--zone`` is optional. When omitted, the handler resolves the owning
zone from ``named-checkconf -p`` (the T2 zone parser) by longest-suffix
match against the FQDN; ambiguous (the FQDN matches two zones equally
deep) or unresolvable (no zone is a suffix of the FQDN) inputs raise
:class:`ZoneResolutionError` **before** any staging.

Why ``dig @localhost`` rather than zonefile lookup
--------------------------------------------------

The operator's "what does this nameserver return for <fqdn>?" question
is best answered by querying the running daemon, not by hand-resolving
through the zonefile. ``dig @localhost`` exercises the same code path
the rest of the world hits when it asks bind9 to resolve <fqdn>, so:

* Zone delegation works correctly (a query for ``api.evba.lab`` that
  delegates to a child zone returns the delegated answer).
* Views are honoured (a ``$ORIGIN`` inside a view that wraps the same
  zone resolves through the view's RPZ / response-policy rules).
* The cache state shows through (an operator asking about an external
  zone gets bind9's cached answer, not a stub-resolver miss).

The handler does not require a ``zone`` parameter for the same reason:
the operator names what they want resolved, the running daemon resolves
it, and the answer is parsed out of ``dig`` output. The trade-off is the
handler depends on a running named -- but that's the same predicate
``bind9.about`` and the rest of the read group already encode.

References
----------

* Parent task: G3.4-T2 (#588).
* Parent Initiative: G3.4 (#367).
* ``dig`` output reference: https://www.isc.org/dig/.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
from typing import TYPE_CHECKING, Any

import dns.exception
import dns.name
import dns.rdata
import dns.rdataclass
import dns.rdatatype
import dns.zone

from meho_backplane.connectors.bind9._atomic import atomic_apply
from meho_backplane.connectors.bind9.ops import Bind9Op

if TYPE_CHECKING:
    from meho_backplane.connectors.bind9.connector import Bind9Connector

__all__ = [
    "BIND9_RECORD_ADD_LLM_INSTRUCTIONS",
    "BIND9_RECORD_ADD_PARAMETER_SCHEMA",
    "BIND9_RECORD_GET_LLM_INSTRUCTIONS",
    "BIND9_RECORD_GET_PARAMETER_SCHEMA",
    "BIND9_RECORD_REMOVE_LLM_INSTRUCTIONS",
    "BIND9_RECORD_REMOVE_PARAMETER_SCHEMA",
    "RECORD_OPS",
    "ZoneResolutionError",
    "bind9_record_add",
    "bind9_record_get",
    "bind9_record_remove",
    "parse_dig_answer",
    "resolve_zone_for_fqdn",
]


# Supported record types for ``bind9.record.get`` -- the operator-relevant
# set the consumer wrapper's ``--get-a-record`` / ``--get-mx-record`` /
# ``--get-txt-record`` verbs covered. T3 will add the matching record
# *write* ops (A/AAAA only -- bind9's atomic-apply discipline is
# substantially harder for CNAME/MX/TXT and the consumer wrapper covered
# only A/AAAA writes).
_SUPPORTED_RECORD_TYPES: frozenset[str] = frozenset({"A", "AAAA", "CNAME", "MX", "TXT"})


# ``dig`` with ``+noall +answer +nocomments`` emits only the ANSWER
# rows, no section markers, no header / question / authority /
# additional sections, no ``;; Query time`` stats line. The handler
# pins those flags so the parser sees a clean per-line ANSWER list;
# the parser then accepts either:
#
# * the clean ``+noall +answer`` shape (one record per non-empty line,
#   no leading ``;``-comments) -- the handler's canonical input, or
# * the ``;; ANSWER SECTION:`` shape (full ``dig`` defaults) -- so the
#   parser can be unit-tested against captured fixtures from a manual
#   ``dig`` invocation, and so a future change to the handler's flag
#   set doesn't silently regress the parser.
#
# The implementation walks every line, skipping ``;``-comments, blank
# lines, and the ``;; <SECTION>:`` markers (we only want ANSWER rows;
# the additional / authority sections happen to share the row shape
# but the handler's ``+noall +answer`` invocation already excludes
# them at the wire level). The shared row-line shape is what makes
# both invocation modes work through the same parser.
_DIG_SECTION_MARKER_RE = re.compile(r"^\s*;;\s*\w+\s+SECTION:\s*$")


def parse_dig_answer(output: str) -> list[dict[str, Any]]:
    """Parse the ANSWER lines of ``dig`` output into row dicts.

    Returns one dict per ANSWER line; an empty list when there is no
    answer (NXDOMAIN, NODATA, or any other no-answer result -- the
    caller decides whether that's an error or a legitimate empty
    answer). Row shape mirrors :func:`bind9.ops_zone.parse_zonefile`'s
    output:

    .. code-block:: python

       {"name": "www.evba.lab.", "ttl": 3600, "class": "IN",
        "type": "A", "rdata": "10.5.50.2"}

    Pure function -- captured ``dig`` output goes in, structured rows
    come out, no IO. The unit suite pins it against captured fixtures
    for each record type.

    Accepts both ``+noall +answer +nocomments`` output (the handler's
    canonical input -- bare per-record lines) and the default
    ``;; ANSWER SECTION:`` shape (so captured fixtures from manual
    ``dig`` invocations work the same). The shape-tolerance lives in
    one line skip ``if line is comment or section marker`` -- the row
    grammar is identical in both modes.

    ``dig`` emits TXT records with the quotes preserved
    (``"v=spf1 a -all"``); the ``rdata`` field surfaces them verbatim
    because that matches what the zonefile reader returns for the same
    record. MX records ship as ``<priority> <exchange>`` (e.g.
    ``"10 mail.evba.lab."``); CNAME ships as the target FQDN.
    """
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # ``dig`` precedes comment lines with ``;`` -- skip them. The
        # section markers (``;; ANSWER SECTION:``, ``;; AUTHORITY
        # SECTION:``) match this prefix too, so the same predicate
        # covers both. The handler's ``+nocomments`` flag should make
        # the predicate unnecessary in the canonical path; the test
        # path captured from default-flags ``dig`` still relies on it.
        if stripped.startswith(";"):
            continue
        if _DIG_SECTION_MARKER_RE.match(stripped):
            continue
        # Each ANSWER line is ``<name> <ttl> <class> <type> <rdata...>``;
        # the rdata may contain spaces (TXT, MX, SRV) so we split into
        # at most five fields with the rest joined back together.
        parts = stripped.split(None, 4)
        if len(parts) < 5:
            # Defensive -- dig should always emit five+ tokens per
            # ANSWER row, but a malformed response (truncated capture,
            # non-DNS noise spliced in) shouldn't crash the parser.
            continue
        name, ttl_str, rclass, rtype, rdata = parts
        try:
            ttl = int(ttl_str)
        except ValueError:
            continue
        rows.append(
            {
                "name": name,
                "ttl": ttl,
                "class": rclass,
                "type": rtype,
                "rdata": rdata,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def bind9_record_get(
    connector: Bind9Connector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``bind9.record.get``.

    Runs ``dig @localhost <fqdn> [<type>] +noall +answer`` against the
    target's local resolver and parses the answer rows. ``+noall
    +answer`` strips the verbose header / question / authority /
    additional sections so the parser sees only the answer block; this
    keeps the wire output bounded for typed-op response sizes (and
    avoids leaking the resolver's transient cache state in
    ``;; ADDITIONAL``).

    Returns ``{fqdn, type, rows, total}``; ``rows`` is empty when the
    record does not exist (NXDOMAIN / NODATA). The handler does *not*
    raise on NXDOMAIN -- an empty answer is a legitimate result, and
    the agent surface should be able to assert "this record does not
    exist" without a structured error envelope.
    """
    fqdn: str = params["fqdn"]
    record_type: str = params.get("type", "A").upper()
    # Schema constrains ``type`` to the supported set, but defensive
    # check stays here so an out-of-band caller (the dispatcher's
    # validate gate runs in production; direct invocations from
    # internal tests bypass it) cannot smuggle an arbitrary string
    # into the remote command.
    if record_type not in _SUPPORTED_RECORD_TYPES:
        raise ValueError(
            f"unsupported record type {record_type!r}; "
            f"expected one of {sorted(_SUPPORTED_RECORD_TYPES)}"
        )
    # ``shlex.quote`` protects ``fqdn`` from shell-metacharacter
    # injection -- the operator-typed value lands on the remote SSH
    # command line. dig itself accepts the FQDN as a positional
    # argument and would otherwise treat ``;`` / ``$()`` as shell
    # specials when the SSH adapter spawns ``sh -c "<command>"``.
    # +noall +answer trims the wire output to only the answer
    # section so the parser sees a bounded payload.
    cmd = f"dig @localhost {shlex.quote(fqdn)} {record_type} +noall +answer +nocomments"
    proc = await connector._run_command(target, cmd, raw_jwt="")
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    output = stdout if isinstance(stdout, str) else ""
    rows = parse_dig_answer(output)
    return {
        "fqdn": fqdn,
        "type": record_type,
        "rows": rows,
        "total": len(rows),
    }


# ---------------------------------------------------------------------------
# Parameter schema + LLM instructions
# ---------------------------------------------------------------------------


BIND9_RECORD_GET_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fqdn": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Fully-qualified domain name to resolve, e.g. "
                "``www.evba.lab`` or ``mail.evba.lab.``. Trailing dot "
                "optional. Resolved by ``dig @localhost`` so views, "
                "delegations, and cache hits all behave as the rest of "
                "the world sees them."
            ),
        },
        "type": {
            "type": "string",
            "enum": sorted(_SUPPORTED_RECORD_TYPES),
            "default": "A",
            "description": (
                "DNS record type. Defaults to A. AAAA / CNAME / MX / "
                "TXT are the operator-relevant complement. Other types "
                "(SRV, NS, SOA, ...) ride through the zone-read op."
            ),
        },
    },
    "required": ["fqdn"],
    "additionalProperties": False,
}


_BIND9_RECORD_GET_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fqdn": {"type": "string"},
        "type": {"type": "string"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "ttl": {"type": "integer"},
                    "class": {"type": "string"},
                    "type": {"type": "string"},
                    "rdata": {"type": "string"},
                },
                "required": ["name", "ttl", "class", "type", "rdata"],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["fqdn", "type", "rows", "total"],
    "additionalProperties": False,
}


BIND9_RECORD_GET_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator asks 'what's the current value of "
        "<fqdn>?' or 'does <fqdn> resolve?'. Resolves via "
        "``dig @localhost`` so views, delegations, and cache state "
        "behave as the rest of the world sees them. Read-only. Returns "
        "an empty ``rows`` for NXDOMAIN / NODATA -- empty is a "
        "legitimate result, not an error. Pair with ``bind9.zone.read`` "
        "for the operator question 'list every record in zone X'; this "
        "op is the targeted-lookup form."
    ),
    "parameter_hints": {
        "fqdn": ("Required. The FQDN to resolve. Trailing dot optional."),
        "type": ("Optional. One of A / AAAA / CNAME / MX / TXT. Defaults to A."),
    },
    "output_shape": (
        "{'fqdn': <str>, 'type': <str>, 'rows': [{name, ttl, class, "
        "type, rdata}], 'total': <int>}. Empty ``rows`` means the "
        "record does not resolve (NXDOMAIN, NODATA, or filtered by an "
        "RPZ rule)."
    ),
}


# ---------------------------------------------------------------------------
# Zone resolution for write ops (``--zone`` omitted -> longest-suffix match)
# ---------------------------------------------------------------------------


class ZoneResolutionError(ValueError):
    """Owning zone could not be resolved for the requested FQDN.

    Two flavours:

    * **unresolvable** -- no configured zone is a suffix of the FQDN.
      The operator named a record outside any zone bind9 serves.
    * **ambiguous** -- two (or more) configured zones tie for the
      longest-suffix match. Should never happen with a real bind9
      config (zones are unique within a view), but the parser handles
      arbitrary input and the check is cheap defence-in-depth.

    The handler raises this **before** any staging, so the dispatcher's
    ``invalid_params`` envelope reports the rejection with zero side
    effects on the remote tree. The :class:`ValueError` base lets the
    dispatcher's ``connector_error`` branch use its standard exception-
    class extras path without a custom shim.
    """

    def __init__(self, reason: str, fqdn: str, candidates: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason: str = reason
        self.fqdn: str = fqdn
        self.candidates: list[str] = candidates or []


def resolve_zone_for_fqdn(zones: list[str], fqdn: str) -> str:
    """Return the zone whose name is the longest suffix of *fqdn*.

    Pure function -- given the parsed zone-list and an FQDN, returns
    the owning zone name (trailing dot stripped, matching the
    ``named-checkconf -p`` canonical shape T2's parser emits). The
    matching contract:

    * The FQDN's label-sequence must end with the zone's label
      sequence. ``api.evba.lab`` matches ``evba.lab`` but not
      ``ba.lab`` (label boundaries are respected; substring matches
      across labels are rejected).
    * On a tie at the longest suffix, raises :class:`ZoneResolutionError`
      with ``reason="ambiguous"``.
    * On no match, raises :class:`ZoneResolutionError` with
      ``reason="unresolvable"``.

    The root zone (``.``) is excluded from candidates -- a write to
    a root-served record is well outside this connector's scope, and
    treating ``.`` as a match for every FQDN would break the
    longest-suffix invariant (every FQDN trivially ends with ``.``).
    """
    fqdn_normalised = fqdn.rstrip(".")
    fqdn_labels = fqdn_normalised.split(".")
    best_match: str | None = None
    best_label_count = -1
    ties: list[str] = []
    for zone in zones:
        zone_normalised = zone.rstrip(".")
        if not zone_normalised or zone_normalised == ".":
            continue
        zone_labels = zone_normalised.split(".")
        # Label-boundary suffix match: the FQDN's trailing labels must
        # be exactly the zone's labels.
        if len(zone_labels) > len(fqdn_labels):
            continue
        if fqdn_labels[-len(zone_labels) :] != zone_labels:
            continue
        if len(zone_labels) > best_label_count:
            best_label_count = len(zone_labels)
            best_match = zone_normalised
            ties = [zone_normalised]
        elif len(zone_labels) == best_label_count:
            ties.append(zone_normalised)
    if best_match is None:
        raise ZoneResolutionError("unresolvable", fqdn=fqdn)
    if len(ties) > 1:
        raise ZoneResolutionError("ambiguous", fqdn=fqdn, candidates=ties)
    return best_match


async def _resolve_zone_via_checkconf(
    connector: Bind9Connector,
    target: Any,
    fqdn: str,
) -> tuple[str, str]:
    """Locate (zone_name, zonefile_path) for *fqdn* via ``named-checkconf -p``.

    Lazy-imports the zone parser to avoid a circular import (T2's
    ``ops_zone`` imports from ``ops`` which (transitively) imports
    from this module). The lazy-import shape mirrors the registration
    walk in the connector class.

    Raises :class:`ZoneResolutionError` if the FQDN doesn't resolve to
    a unique zone, or if the matched zone has no ``file`` directive.
    """
    from meho_backplane.connectors.bind9.ops_zone import (
        parse_named_checkconf_zones,
    )

    proc = await connector._run_command(target, "named-checkconf -p", raw_jwt="")
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    output = stdout if isinstance(stdout, str) else ""
    rows = parse_named_checkconf_zones(output)
    zone_names = [row["name"] for row in rows]
    zone_name = resolve_zone_for_fqdn(zone_names, fqdn)
    # Pull the zonefile path back out of the parsed rows.
    matching_row = next(
        (row for row in rows if row["name"].rstrip(".") == zone_name),
        None,
    )
    if matching_row is None or not matching_row.get("file"):
        # Best-suffix matched a zone with no ``file`` directive (a
        # hint or forward zone, typically). The write ops only operate
        # on master zonefiles; treat as unresolvable so the
        # ``invalid_params`` envelope carries a coherent message.
        raise ZoneResolutionError("unresolvable", fqdn=fqdn)
    return zone_name, str(matching_row["file"])


# ---------------------------------------------------------------------------
# Zonefile transformation helpers (dnspython round-trip)
# ---------------------------------------------------------------------------


def _bump_soa_serial(zone: dns.zone.Zone) -> None:
    """Increment the zone's SOA serial in place.

    bind9 requires the SOA serial to advance on every zonefile change
    for slaves to pick up the update; ``rndc reload`` of a master
    zone honours the same invariant for in-memory reload. dnspython
    Rdata is immutable, so the bump is a replace-the-rdata operation.
    """
    # ``zone.origin`` is typed ``Name | None`` on dnspython; a zone
    # constructed via :func:`dns.zone.from_text` with an explicit
    # ``origin`` always has a non-None origin, but mypy's ``--strict``
    # walk can't prove that. Assert + assign to a narrowed local so
    # the ``find_rdataset`` call type-checks without an ignore.
    origin = zone.origin
    assert origin is not None, "zone parsed from text must carry an origin"
    soa_rds = zone.find_rdataset(origin, dns.rdatatype.SOA)
    old_soa = soa_rds[0]
    new_serial = old_soa.serial + 1
    new_soa = dns.rdata.from_text(
        dns.rdataclass.IN,
        dns.rdatatype.SOA,
        f"{old_soa.mname} {old_soa.rname} {new_serial} "
        f"{old_soa.refresh} {old_soa.retry} {old_soa.expire} {old_soa.minimum}",
    )
    soa_rds.clear()  # type: ignore[no-untyped-call]
    soa_rds.add(new_soa, ttl=soa_rds.ttl)


def _zonefile_text(zone: dns.zone.Zone) -> str:
    """Render *zone* back to zonefile text with absolute names.

    ``relativize=False`` keeps FQDNs as absolute (``www.evba.lab.``
    rather than ``www``) so the round-tripped file remains
    unambiguous regardless of which ``$ORIGIN`` line happens to be
    in scope. dnspython prepends a ``$ORIGIN`` line via
    ``want_origin=True``; we set it so the file is self-describing.
    """
    return zone.to_text(relativize=False, want_origin=True)


def _add_record_to_zonefile(
    zonefile_text: str,
    *,
    zone_name: str,
    fqdn: str,
    ip: str,
    record_type: str,
    default_ttl: int = 3600,
) -> str:
    """Return new zonefile text with the requested record added.

    Pure transformation -- parses *zonefile_text* with dnspython, adds
    the record, bumps the SOA serial, returns the rendered text. Used
    by :func:`bind9_record_add`.

    If a record with the exact (name, type, rdata) already exists,
    the operation is idempotent: SOA serial bumps once, no duplicate
    rdata. dnspython's ``Rdataset.add`` is a set-add (de-dupes by
    canonical wire form).
    """
    origin = zone_name if zone_name.endswith(".") else zone_name + "."
    zone = dns.zone.from_text(
        zonefile_text,
        origin=origin,
        relativize=False,
        check_origin=False,
    )
    fqdn_abs = fqdn if fqdn.endswith(".") else fqdn + "."
    name = dns.name.from_text(fqdn_abs)
    rdtype = dns.rdatatype.from_text(record_type)
    rds = zone.find_rdataset(name, rdtype, create=True)
    rdata = dns.rdata.from_text(dns.rdataclass.IN, rdtype, ip)
    rds.add(rdata, ttl=default_ttl)
    _bump_soa_serial(zone)
    return _zonefile_text(zone)


def _remove_record_from_zonefile(
    zonefile_text: str,
    *,
    zone_name: str,
    fqdn: str,
) -> str:
    """Return new zonefile text with every A/AAAA record at *fqdn* removed.

    ``record.remove`` removes the *name*'s A and AAAA rrsets. The
    consumer wrapper's verb shape matches: a record-remove for
    ``host.evba.lab`` strips A + AAAA (the v4+v6 pair); leaves
    CNAME / MX / TXT untouched (out of scope for v0.2 -- T2's
    record.get exposes them read-only, but the consumer wrapper
    never wrote CNAME / MX / TXT either).

    No-op if the FQDN has no A/AAAA records -- still bumps SOA so
    the operation is consistently observable in zone-transfer logs.
    """
    origin = zone_name if zone_name.endswith(".") else zone_name + "."
    zone = dns.zone.from_text(
        zonefile_text,
        origin=origin,
        relativize=False,
        check_origin=False,
    )
    fqdn_abs = fqdn if fqdn.endswith(".") else fqdn + "."
    name = dns.name.from_text(fqdn_abs)
    for rdtype in (dns.rdatatype.A, dns.rdatatype.AAAA):
        if zone.get_rdataset(name, rdtype) is not None:
            zone.delete_rdataset(name, rdtype)
    _bump_soa_serial(zone)
    return _zonefile_text(zone)


# ---------------------------------------------------------------------------
# Write handlers
# ---------------------------------------------------------------------------


_WRITE_SUPPORTED_TYPES: frozenset[str] = frozenset({"A", "AAAA"})


def _validate_ip_for_type(ip: str, record_type: str) -> None:
    """Reject *ip* if it doesn't match the requested record type.

    ``record.add`` accepts A (IPv4) and AAAA (IPv6); a type/value
    mismatch is a structural error caught at the API boundary so the
    handler never stages a doomed zonefile. ``ipaddress`` is the
    stdlib parser; raises ``ValueError`` on malformed strings, and
    the per-family check below catches the "valid v4 but routed via
    AAAA" cross-type mistake.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError as exc:
        raise ValueError(f"invalid IP address {ip!r}: {exc}") from exc
    if record_type == "A" and not isinstance(addr, ipaddress.IPv4Address):
        raise ValueError(f"record type A expects an IPv4 address; got {ip!r}")
    if record_type == "AAAA" and not isinstance(addr, ipaddress.IPv6Address):
        raise ValueError(f"record type AAAA expects an IPv6 address; got {ip!r}")


async def _resolve_zone_and_path(
    connector: Bind9Connector,
    target: Any,
    *,
    fqdn: str,
    explicit_zone: str | None,
) -> tuple[str, str]:
    """Return ``(zone_name, zonefile_path)`` for *fqdn*.

    Branches on ``explicit_zone``: when provided, looks up the
    zonefile path directly; otherwise resolves via longest-suffix
    match against ``named-checkconf -p``. Shared by the add / remove
    handlers so the two paths cannot drift.
    """
    if explicit_zone is not None:
        zone_name = explicit_zone.rstrip(".")
        zonefile_path = await _resolve_zonefile_path_for_zone(connector, target, zone_name)
        return zone_name, zonefile_path
    return await _resolve_zone_via_checkconf(connector, target, fqdn)


async def _read_zonefile_text(
    connector: Bind9Connector,
    target: Any,
    zonefile_path: str,
) -> str:
    """Read the current zonefile text via ``cat`` (no sudo needed).

    bind9 zonefiles are world-readable per T2's design. Reuses the
    same shell-quote pattern T2's ``bind9.zone.read`` uses for
    path-safety.
    """
    quoted_path = "'" + zonefile_path.replace("'", "'\\''") + "'"
    cat_proc = await connector._run_command(target, f"cat {quoted_path}", raw_jwt="")
    cat_stdout = (cat_proc.stdout or "") if hasattr(cat_proc, "stdout") else ""
    return cat_stdout if isinstance(cat_stdout, str) else ""


async def bind9_record_add(
    connector: Bind9Connector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``bind9.record.add`` -- atomic A/AAAA record write.

    Sequence:

    1. Resolve owning zone (via ``--zone`` param, or longest-suffix
       match against ``named-checkconf -p``).
    2. Validate the IP matches the requested record type.
    3. Read the current zonefile (``cat <path>``).
    4. Transform via :func:`_add_record_to_zonefile` (dnspython
       parse + add + SOA bump + render).
    5. :func:`atomic_apply` stages the new zonefile, runs
       ``named-checkzone <zone> <path>``, ``rndc reload``, and the
       dig-verify predicate; rolls back on any failure.

    Returns ``{fqdn, ip, type, zone, file, op_class, result_state_before,
    result_state_after}``. ``op_class="write"`` is set explicitly even
    though :func:`~meho_backplane.broadcast.events.classify_op`
    derives the same value from the op-id suffix -- the dual signal
    is what the audit-replay path (G8.2) reads to reconstruct the
    change without re-parsing the op-id namespace.

    Raises :class:`ZoneResolutionError` if ``--zone`` is omitted and
    the FQDN can't be uniquely resolved (pre-stage; no remote IO past
    the ``named-checkconf -p`` lookup itself). Raises
    :class:`AtomicApplyError` on any rollback path.
    """
    fqdn: str = params["fqdn"]
    ip: str = params["ip"]
    record_type: str = params.get("type", "A").upper()
    explicit_zone: str | None = params.get("zone")

    if record_type not in _WRITE_SUPPORTED_TYPES:
        raise ValueError(
            f"record.add only supports A / AAAA; got type={record_type!r}. "
            f"CNAME / MX / TXT writes are out of scope for v0.2."
        )
    _validate_ip_for_type(ip, record_type)

    sudo_password = _sudo_password_from_target(target)
    zone_name, zonefile_path = await _resolve_zone_and_path(
        connector, target, fqdn=fqdn, explicit_zone=explicit_zone
    )
    current_text = await _read_zonefile_text(connector, target, zonefile_path)

    try:
        new_text = _add_record_to_zonefile(
            current_text,
            zone_name=zone_name,
            fqdn=fqdn,
            ip=ip,
            record_type=record_type,
        )
    except dns.exception.DNSException as exc:
        raise ValueError(
            f"failed to parse / transform zonefile for zone {zone_name!r}: {exc}"
        ) from exc

    # Dig-verify predicate: the new IP must appear in the answer.
    # ``dig +short`` emits one rdata per line with no decoration, so
    # ``grep -qxF`` (literal, full-line, quiet) is the right shape for
    # an exact-match assertion that doesn't false-match a substring of
    # another row.
    quoted_fqdn = shlex.quote(fqdn)
    quoted_ip = shlex.quote(ip)
    verify_cmd = f"dig @localhost {quoted_fqdn} {record_type} +short | grep -qxF {quoted_ip}"

    apply_result = await atomic_apply(
        connector,
        target,
        raw_jwt="",
        sudo_password=sudo_password,
        audit_slice_path=zonefile_path,
        zone_name=zone_name,
        staged_bytes=new_text.encode("utf-8"),
        verify_command=verify_cmd,
    )

    return {
        "fqdn": fqdn,
        "ip": ip,
        "type": record_type,
        "zone": zone_name,
        "file": zonefile_path,
        "op_class": "write",
        "result_state_before": apply_result.state_before,
        "result_state_after": apply_result.state_after,
    }


async def bind9_record_remove(
    connector: Bind9Connector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``bind9.record.remove`` -- atomic A/AAAA record remove.

    Sequence: same as :func:`bind9_record_add` but the zonefile
    transform deletes the FQDN's A and AAAA rdatasets, and the
    verify predicate asserts the FQDN no longer resolves
    (``dig @localhost <fqdn> +short`` returns empty stdout).

    Returns the same envelope shape as ``record.add``.
    """
    fqdn: str = params["fqdn"]
    explicit_zone: str | None = params.get("zone")

    sudo_password = _sudo_password_from_target(target)
    zone_name, zonefile_path = await _resolve_zone_and_path(
        connector, target, fqdn=fqdn, explicit_zone=explicit_zone
    )
    current_text = await _read_zonefile_text(connector, target, zonefile_path)

    try:
        new_text = _remove_record_from_zonefile(
            current_text,
            zone_name=zone_name,
            fqdn=fqdn,
        )
    except dns.exception.DNSException as exc:
        raise ValueError(
            f"failed to parse / transform zonefile for zone {zone_name!r}: {exc}"
        ) from exc

    # Verify: dig must return no answer rows for either A or AAAA.
    # ``+short`` exits 0 even on empty output, so we explicitly assert
    # zero lines via ``[ -z "$(dig ...)" ]``.
    quoted_fqdn = shlex.quote(fqdn)
    verify_cmd = (
        f'[ -z "$(dig @localhost {quoted_fqdn} A +short)" ] '
        f'&& [ -z "$(dig @localhost {quoted_fqdn} AAAA +short)" ]'
    )

    apply_result = await atomic_apply(
        connector,
        target,
        raw_jwt="",
        sudo_password=sudo_password,
        audit_slice_path=zonefile_path,
        zone_name=zone_name,
        staged_bytes=new_text.encode("utf-8"),
        verify_command=verify_cmd,
    )

    return {
        "fqdn": fqdn,
        "zone": zone_name,
        "file": zonefile_path,
        "op_class": "write",
        "result_state_before": apply_result.state_before,
        "result_state_after": apply_result.state_after,
    }


async def _resolve_zonefile_path_for_zone(
    connector: Bind9Connector,
    target: Any,
    zone_name: str,
) -> str:
    """Look up the zonefile path for an explicit ``--zone`` arg.

    Lazy-imports T2's ``_resolve_zonefile_path`` to avoid a cycle
    (T2's ``ops_zone`` imports the shared ``ops`` module that imports
    this module). Same shape as the longest-suffix path.
    """
    from meho_backplane.connectors.bind9.ops_zone import (
        ZonefileReadError,
        _resolve_zonefile_path,
    )

    proc = await connector._run_command(target, "named-checkconf -p", raw_jwt="")
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    output = stdout if isinstance(stdout, str) else ""
    try:
        return _resolve_zonefile_path(output, zone_name)
    except ZonefileReadError as exc:
        # Map the missing-zone case to the same structured error the
        # auto-resolve path uses, so callers see a uniform shape.
        raise ZoneResolutionError("unresolvable", fqdn=zone_name) from exc


def _sudo_password_from_target(target: Any) -> str:
    """Read the sudo password from the target's ``secret_ref``.

    The :class:`Target` schema (v0.2) stores SSH credentials on a
    target's ``secret_ref`` dict; the sudo password reuses the SSH
    password by default (consumer-wrapper convention). A future iter
    may key on a dedicated ``sudo_password`` field; the lookup here
    falls back to ``password`` so existing target rows keep working.

    Raises :class:`ValueError` if no password is configured -- the
    safe-sudo primitive's invariant requires a non-empty single-line
    string and the connector cannot legitimately proceed otherwise.
    """
    secret_ref = getattr(target, "secret_ref", None) or {}
    if not isinstance(secret_ref, dict):
        raise ValueError(f"target.secret_ref must be a dict; got {type(secret_ref).__name__}")
    password = secret_ref.get("sudo_password") or secret_ref.get("password")
    if not password:
        raise ValueError(
            "target.secret_ref carries no sudo_password / password; "
            "bind9 write ops require a sudo credential"
        )
    return str(password)


# ---------------------------------------------------------------------------
# Write-op parameter schemas + LLM instructions
# ---------------------------------------------------------------------------


_WRITE_WARNING = (
    "WARNING: this change is global and atomic. The atomic-apply "
    "primitive stages the new zonefile, runs ``named-checkzone``, "
    "``rndc reload``, and a dig-verify predicate; on any failure "
    "the pre-op ``/etc/bind/`` tree is restored byte-identical. On "
    "success the change is live for every consumer of this "
    "nameserver -- DNS has no per-caller scoping. ``safety_level`` "
    "is ``caution`` (the production-path gate is G7/G10 policy "
    "territory keyed on this value)."
)


BIND9_RECORD_ADD_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fqdn": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Fully-qualified domain name to add, e.g. "
                "``api.evba.lab``. Trailing dot optional. The handler "
                "resolves the owning zone from ``named-checkconf -p`` "
                "by longest-suffix match unless ``zone`` is set "
                "explicitly."
            ),
        },
        "ip": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Target IP address. Must be IPv4 for ``type=A`` and "
                "IPv6 for ``type=AAAA``; the handler refuses a "
                "type/family mismatch before any staging."
            ),
        },
        "type": {
            "type": "string",
            "enum": sorted(_WRITE_SUPPORTED_TYPES),
            "default": "A",
            "description": (
                "Record type. Only A and AAAA are supported -- CNAME "
                "/ MX / TXT writes are out of scope for v0.2 (the "
                "consumer wrapper never wrote them either)."
            ),
        },
        "zone": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Optional. Owning zone name. When omitted, the "
                "handler resolves it by longest-suffix match against "
                "``named-checkconf -p``; ambiguous or unresolvable "
                "FQDNs are rejected pre-staging with structured "
                "``invalid_params``."
            ),
        },
    },
    "required": ["fqdn", "ip"],
    "additionalProperties": False,
}


_WRITE_RESPONSE_SCHEMA_PROPERTIES: dict[str, Any] = {
    "fqdn": {"type": "string"},
    "zone": {"type": "string"},
    "file": {"type": "string"},
    "op_class": {"type": "string", "enum": ["write"]},
    "result_state_before": {"type": "string"},
    "result_state_after": {"type": "string"},
}


_BIND9_RECORD_ADD_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_WRITE_RESPONSE_SCHEMA_PROPERTIES,
        "ip": {"type": "string"},
        "type": {"type": "string", "enum": sorted(_WRITE_SUPPORTED_TYPES)},
    },
    "required": [
        "fqdn",
        "ip",
        "type",
        "zone",
        "file",
        "op_class",
        "result_state_before",
        "result_state_after",
    ],
    "additionalProperties": False,
}


BIND9_RECORD_ADD_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Add a forward A or AAAA record to a bind9-served zone. "
        + _WRITE_WARNING
        + " Use ``bind9.record.get`` first to confirm the FQDN is "
        "not already in use (the handler is idempotent for an "
        "identical (name, type, rdata) tuple but the operator "
        "should still see the existing state)."
    ),
    "parameter_hints": {
        "fqdn": "Required. The FQDN to create. Trailing dot optional.",
        "ip": "Required. IPv4 for type=A, IPv6 for type=AAAA.",
        "type": "Optional. ``A`` (default) or ``AAAA``.",
        "zone": (
            "Optional. Owning zone. Omit to let the handler pick "
            "the longest-suffix-matching zone automatically."
        ),
    },
    "output_shape": (
        "{'fqdn', 'ip', 'type', 'zone', 'file', 'op_class': 'write', "
        "'result_state_before': <prior-zonefile-text>, "
        "'result_state_after': <post-write-zonefile-text>}. "
        "``result_state_*`` is the full zonefile content for audit "
        "replay; the staged change is the diff between the two."
    ),
}


BIND9_RECORD_REMOVE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fqdn": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Fully-qualified domain name to remove, e.g. "
                "``api.evba.lab``. Removes the A and AAAA rdatasets "
                "at that name (CNAME / MX / TXT are out of scope)."
            ),
        },
        "zone": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Optional. Owning zone. When omitted, resolved via "
                "longest-suffix match the same way ``record.add`` "
                "does."
            ),
        },
    },
    "required": ["fqdn"],
    "additionalProperties": False,
}


_BIND9_RECORD_REMOVE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _WRITE_RESPONSE_SCHEMA_PROPERTIES,
    "required": [
        "fqdn",
        "zone",
        "file",
        "op_class",
        "result_state_before",
        "result_state_after",
    ],
    "additionalProperties": False,
}


BIND9_RECORD_REMOVE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Remove the A and AAAA records at the given FQDN. "
        + _WRITE_WARNING
        + " Use ``bind9.record.get`` first to confirm the current "
        "state. Removing a record bind9 doesn't actually serve is "
        "a no-op (verify still passes -- the FQDN doesn't resolve "
        "before or after)."
    ),
    "parameter_hints": {
        "fqdn": "Required. The FQDN to clear of A / AAAA records.",
        "zone": (
            "Optional. Owning zone. Omit to let the handler pick "
            "the longest-suffix-matching zone automatically."
        ),
    },
    "output_shape": (
        "{'fqdn', 'zone', 'file', 'op_class': 'write', "
        "'result_state_before': <prior-zonefile-text>, "
        "'result_state_after': <post-remove-zonefile-text>}."
    ),
}


# ---------------------------------------------------------------------------
# Op metadata table
# ---------------------------------------------------------------------------


RECORD_OPS: tuple[Bind9Op, ...] = (
    Bind9Op(
        op_id="bind9.record.get",
        handler_attr="bind9_record_get",
        summary="Resolve a record via ``dig @localhost`` -- A / AAAA / CNAME / MX / TXT.",
        description=(
            "Runs ``dig @localhost <fqdn> <type> +noall +answer "
            "+nocomments`` against the local resolver and parses the "
            "ANSWER section into one row per record value. ``type`` "
            "defaults to A; supported types are A / AAAA / CNAME / MX "
            "/ TXT (the operator-relevant subset; other types ride "
            "through ``bind9.zone.read``). Resolves via the running "
            "daemon so views, delegations, and cache hits behave as "
            "the rest of the world sees them. Empty ``rows`` is a "
            "legitimate NXDOMAIN / NODATA result, not an error."
        ),
        parameter_schema=BIND9_RECORD_GET_PARAMETER_SCHEMA,
        response_schema=_BIND9_RECORD_GET_RESPONSE_SCHEMA,
        group_key="record",
        tags=("read-only", "record", "lookup"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=BIND9_RECORD_GET_LLM_INSTRUCTIONS,
    ),
    Bind9Op(
        op_id="bind9.record.add",
        handler_attr="bind9_record_add",
        summary="Add an A or AAAA record atomically with rollback on any failure.",
        description=(
            "Atomic stage-validate-commit-reload-verify-rollback "
            "write of a forward A/AAAA record. Resolves the owning "
            "zone via ``named-checkconf -p`` longest-suffix match "
            "when ``zone`` is omitted; ambiguous or unresolvable "
            "FQDNs are rejected pre-staging. " + _WRITE_WARNING
        ),
        parameter_schema=BIND9_RECORD_ADD_PARAMETER_SCHEMA,
        response_schema=_BIND9_RECORD_ADD_RESPONSE_SCHEMA,
        group_key="record",
        tags=("write", "record", "atomic-apply"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions=BIND9_RECORD_ADD_LLM_INSTRUCTIONS,
    ),
    Bind9Op(
        op_id="bind9.record.remove",
        handler_attr="bind9_record_remove",
        summary="Remove the A and AAAA records at an FQDN atomically with rollback.",
        description=(
            "Atomic stage-validate-commit-reload-verify-rollback "
            "remove of every A and AAAA record at the given FQDN. "
            "Idempotent when the records are already absent (verify "
            "passes -- the FQDN doesn't resolve before or after). " + _WRITE_WARNING
        ),
        parameter_schema=BIND9_RECORD_REMOVE_PARAMETER_SCHEMA,
        response_schema=_BIND9_RECORD_REMOVE_RESPONSE_SCHEMA,
        group_key="record",
        tags=("write", "record", "atomic-apply"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions=BIND9_RECORD_REMOVE_LLM_INSTRUCTIONS,
    ),
)
