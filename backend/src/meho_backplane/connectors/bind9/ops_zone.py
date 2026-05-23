# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: zone.list and zone.read share the named-checkconf
# parser, the zonefile-path resolver, and the response-schema shape
# (rows + total). Splitting forces the parser into a third module that
# both depend on, which (a) obscures the linear top-to-bottom read of
# this module and (b) duplicates the "see ops_zone for parsers" cross-
# reference in every test. The file's 619 lines are dominated by JSON
# Schema blobs and LLM_INSTRUCTIONS prose, not logic.

"""bind9 zone-read ops -- ``bind9.zone.list`` + ``bind9.zone.read``.

G3.4-T2 (#588) of Initiative #367. T1's skeleton (#587) shipped the
``Bind9Connector`` plus the ``bind9.about`` canary op; this module
layers the first two read ops onto that surface:

* ``bind9.zone.list`` -- run ``named-checkconf -p`` and parse the
  canonicalised config dump into one row per ``zone "<name>"`` block.
  Each row is ``{name, file, type}`` where ``type`` is the zone's
  declared role (``master`` / ``slave`` / ``forward`` / ...). No
  per-handler handle truncation -- zone counts in real deployments
  are O(10..100) and well under any reducer threshold.
* ``bind9.zone.read <zone>`` -- locate the zonefile path via
  ``named-checkconf -p`` (so the operator doesn't have to know whether
  the file is under ``/etc/bind/`` or a chrooted ``/var/cache/bind/``
  path), read the file, and parse it with :mod:`dns.zone`. Returns
  ``{rows: [...], total: <int>}`` where each row is
  ``{name, ttl, class, type, rdata}`` -- a flat dict the agent surface
  can render uniformly.

Pure parsers vs handler thin layer
----------------------------------

Following the :mod:`~meho_backplane.connectors.kubernetes.ops_core`
``*_row``-helper convention, the heavy lifting lives in pure functions
:func:`parse_named_checkconf_zones` and :func:`parse_zonefile` that take
captured stdout / file text and return Python data; the bound-method
handlers are the thin SSH-call + parse + shape layer. The unit suite
pins the parsers directly against fixture text without booting an event
loop.

JSONFlux handle pattern -- deferred to the reducer
--------------------------------------------------

The Issue #588 body's acceptance language ("JSONFlux handle when the
parsed record list exceeds ~20 rows / 4 KB") was patterned on Issue
#322's identical clause for the K8s connector. The K8s landing
(``ops_core.py``) **deliberately did not implement** per-handler
threshold logic; that module's docstring spells out the rationale and
points at :mod:`~meho_backplane.operations.reducer` (the v0.2 default is
:class:`PassThroughReducer` -- handle creation is the future real
reducer's job, not the connector's). The bind9 read group adopts the
same posture for the same reasons:

* Coupling every connector to the reducer's threshold calibration
  doubles the spill-path implementation and locks the threshold at the
  connector boundary.
* :class:`~meho_backplane.connectors.schemas.OperationResult` already
  has a dedicated ``handle`` field the dispatcher's reducer slot
  populates; per-handler emission would bypass the reducer's audit /
  TTL / store-routing logic.
* The G3.1-T4 (#304) ``HandleStore`` Task was closed as superseded; no
  shared substrate exists today that a per-handler emission could
  delegate to.

The handler ships the raw row list plus a ``total`` count so a future
JSONFlux reducer can pull both signals (inlined sample size + total)
without re-parsing the response.

References
----------

* Parent task: G3.4-T2 (#588).
* Parent Initiative: G3.4 (#367).
* Sibling precedent: G3.2-T2 K8s core ops (#322 / ``ops_core.py``)
  documents the reducer-side-handle decision verbatim.
* Substrate: :mod:`meho_backplane.operations.reducer` (the
  :class:`~meho_backplane.operations.reducer.Reducer` Protocol) +
  :class:`meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`,
  the live dispatcher default (installed in ``main.py`` via
  ``set_default_reducer``) that satisfies it. See
  ``docs/architecture/jsonflux.md``.
* ISC bind9 9.18 docs:
  https://bind9.readthedocs.io/en/v9.18/manpages.html#named-checkconf
  (named-checkconf), https://bind9.readthedocs.io/en/v9.18/chapter3.html
  (zonefile grammar).
* :mod:`dns.zone` reference: https://dnspython.readthedocs.io/en/stable/zone.html
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import dns.exception
import dns.rdataclass
import dns.rdatatype
import dns.zone

from meho_backplane.connectors.bind9.ops import Bind9Op

if TYPE_CHECKING:
    from meho_backplane.connectors.bind9.connector import Bind9Connector

__all__ = [
    "BIND9_ZONE_LIST_LLM_INSTRUCTIONS",
    "BIND9_ZONE_LIST_PARAMETER_SCHEMA",
    "BIND9_ZONE_READ_LLM_INSTRUCTIONS",
    "BIND9_ZONE_READ_PARAMETER_SCHEMA",
    "ZONE_OPS",
    "ZonefileReadError",
    "bind9_zone_list",
    "bind9_zone_read",
    "parse_named_checkconf_zones",
    "parse_zonefile",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ZonefileReadError(LookupError):
    """The requested zone was not present in ``named-checkconf -p`` output.

    Distinct from a generic :class:`LookupError` so the dispatcher's
    ``connector_error`` envelope carries
    ``extras.exception_class="ZonefileReadError"`` and callers can render
    a "zone not configured" hint without parsing the error string.
    """


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


# ``named-checkconf -p`` emits the canonicalised config: every zone is
# declared with a literal ``zone "<name>" <class>? { ... };`` block on
# its own line (the class clause is optional and defaults to IN). Inside
# the block the file path is on a ``file "<path>";`` line and the role
# is on a ``type <master|slave|forward|...>;`` line. Stripping
# whitespace + leading indentation makes both lines uniformly grep-able.
#
# The regex matches the opening line so the caller can walk between
# matches; the inner ``file`` / ``type`` lines are extracted via a
# linear scan up to the matching ``};`` closer. We deliberately don't
# use a single mega-regex for the whole block -- nested braces (``view``
# blocks wrapping multiple ``zone`` blocks) would force a recursive
# pattern, and the file is line-oriented anyway.
_ZONE_HEADER_RE = re.compile(
    r"""
    ^\s*zone\s+
    (?:"(?P<name_quoted>[^"]+)"|(?P<name_bare>\S+))
    (?:\s+(?P<class>IN|HS|CH))?
    \s*\{\s*$
    """,
    re.VERBOSE,
)
_ZONE_FILE_RE = re.compile(r'^\s*file\s+"([^"]+)"\s*;\s*$')
_ZONE_TYPE_RE = re.compile(r"^\s*type\s+(\w+)\s*;\s*$")


def parse_named_checkconf_zones(output: str) -> list[dict[str, Any]]:
    """Parse ``named-checkconf -p`` output into zone rows.

    Returns one ``{"name": str, "file": str | None, "type": str | None}``
    dict per top-level ``zone "<name>"`` block found in *output*. Pure
    function -- given the same string, returns identical output. The
    unit suite pins it against captured fixtures without invoking
    ``named-checkconf`` itself.

    Brace tracking is line-oriented and uses a simple depth counter:
    every ``{`` increments, every ``}`` decrements. A zone block's body
    ends when the depth returns to the level it had **before** the
    opening ``zone "..." {`` line. This correctly handles a ``view``
    wrapping multiple zone blocks (each zone enters at view-depth+1 and
    closes back at view-depth), which is what bind9 emits for any
    deployment that declares views.

    *output* is the stdout of ``named-checkconf -p`` -- the
    canonicalised form, not the raw ``/etc/bind/named.conf`` source.
    The canonicalised form normalises whitespace and comments away, so
    the parser does not need to handle ``/* ... */`` block comments or
    ``// ...`` line comments at all (``named-checkconf -p`` has already
    stripped them).
    """
    rows: list[dict[str, Any]] = []
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        match = _ZONE_HEADER_RE.match(line)
        if not match:
            # Track depth so we can skip non-zone braces (e.g. ``options``
            # blocks) cheaply; we only need to be at depth 0 (or a
            # ``view`` body) when we hit a ``zone "..."`` line, but the
            # regex anchors on the literal ``zone`` keyword so depth
            # tracking is only required to scan *past* a zone body.
            i += 1
            continue

        name = match.group("name_quoted") or match.group("name_bare")
        depth = 1
        zone_file: str | None = None
        zone_type: str | None = None
        j = i + 1
        while j < len(lines) and depth > 0:
            inner = lines[j]
            # Track brace depth -- zone bodies can contain nested
            # ``masters`` / ``also-notify`` blocks under bind9 9.x; we
            # close the zone block only when the matching ``};`` lands.
            depth += inner.count("{")
            depth -= inner.count("}")
            if zone_file is None:
                file_match = _ZONE_FILE_RE.match(inner)
                if file_match:
                    zone_file = file_match.group(1)
            if zone_type is None:
                type_match = _ZONE_TYPE_RE.match(inner)
                if type_match:
                    zone_type = type_match.group(1)
            j += 1
        rows.append({"name": name, "file": zone_file, "type": zone_type})
        i = j
    return rows


def _rdata_to_text(rdata: Any) -> str:
    """Convert a :mod:`dns.rdata` instance to its zonefile text representation.

    :meth:`dns.rdata.Rdata.to_text` returns the canonical zonefile form
    (e.g. ``"10.5.50.2"`` for an A, ``"10 mail.evba.lab."`` for an MX).
    Quotes in TXT records are preserved verbatim so the agent sees the
    same shape ``dig`` prints.
    """
    return str(rdata.to_text())


def parse_zonefile(text: str, origin: str) -> list[dict[str, Any]]:
    """Parse *text* (a bind9 zonefile) into a list of record-row dicts.

    Returns one row per record (one row per rrset member, not one row
    per rrset -- MX rrsets with multiple priorities or A rrsets with
    multiple addresses yield one row each). Row shape:

    .. code-block:: python

       {
           "name": "www.evba.lab.",     # absolute, trailing-dot
           "ttl": 3600,                 # int seconds
           "class": "IN",               # rdata class string
           "type": "A",                 # rdata type string
           "rdata": "10.5.50.2",        # canonical zonefile text
       }

    Pure function -- delegates to :func:`dns.zone.from_text` for the
    grammar handling and :meth:`dns.zone.Zone.iterate_rdatas` for the
    row walk. ``origin`` is required (we always know the zone we asked
    for from ``bind9.zone.list`` output); ``relativize=False`` keeps the
    ``name`` field as the FQDN every operator expects rather than the
    relativised form ``dnspython`` uses internally.

    Raises :class:`dns.exception.DNSException` on malformed zonefile
    content -- callers (the handler) catch and rewrap into a
    structured-error envelope via the dispatcher's ``connector_error``
    branch.
    """
    # ``check_origin=False`` -- ``dns.zone.from_text`` otherwise demands
    # an SOA + NS rrset at the origin to "validate" the zone; that's a
    # publishing-correctness check, not a parse-correctness check. The
    # operator wants to see every record bind9 has, including the
    # transient state of a zone in mid-edit; we trust ``named -t`` /
    # ``named-checkconf`` to be the authoritative validator.
    zone = dns.zone.from_text(
        text,
        origin=origin,
        relativize=False,
        check_origin=False,
    )
    rows: list[dict[str, Any]] = []
    for name, ttl, rdata in zone.iterate_rdatas():
        rows.append(
            {
                "name": str(name),
                "ttl": int(ttl),
                "class": dns.rdataclass.to_text(rdata.rdclass),
                "type": dns.rdatatype.to_text(rdata.rdtype),
                "rdata": _rdata_to_text(rdata),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Handlers -- module-level free functions; the bound-method shims on
# Bind9Connector forward into these so a future per-op-handler-file
# split keeps the registration API stable.
# ---------------------------------------------------------------------------


async def bind9_zone_list(
    connector: Bind9Connector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``bind9.zone.list``.

    Runs ``named-checkconf -p`` on *target* and parses every top-level
    ``zone "..."`` block into a row. Returns ``{rows, total}``; the
    response stays inline (no handle) per the module docstring's
    reducer-side-handle decision.
    """
    del params  # schema declares the param object empty
    proc = await connector._run_command(target, "named-checkconf -p", raw_jwt="")
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    output = stdout if isinstance(stdout, str) else ""
    rows = parse_named_checkconf_zones(output)
    return {"rows": rows, "total": len(rows)}


def _resolve_zonefile_path(checkconf_output: str, zone_name: str) -> str:
    """Locate the zonefile path for *zone_name* in ``named-checkconf -p`` output.

    Walks the parsed zone-row list, matching by name (the canonicalised
    form has no trailing dot, so we accept both ``"evba.lab"`` and
    ``"evba.lab."`` from the caller). Raises :class:`ZonefileReadError`
    if the zone is not configured or carries no ``file`` directive
    (the master / slave / forward types always carry a ``file``;
    type ``hint`` for the root zone may not, but that's not a zone the
    operator would call ``zone.read`` on in v0.2).
    """
    normalised = zone_name.rstrip(".")
    for row in parse_named_checkconf_zones(checkconf_output):
        if row["name"].rstrip(".") == normalised:
            zonefile = row.get("file")
            if not zonefile:
                raise ZonefileReadError(
                    f"zone {zone_name!r} is configured but has no ``file`` "
                    f"directive (type={row.get('type')!r}); cannot read"
                )
            return str(zonefile)
    raise ZonefileReadError(f"zone {zone_name!r} not configured on this nameserver")


async def bind9_zone_read(
    connector: Bind9Connector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``bind9.zone.read``.

    Two SSH round-trips (the asyncssh pool reuses one connection):

    1. ``named-checkconf -p`` -- resolve the zonefile path for *zone*.
    2. ``cat <zonefile>`` -- read the file content for parsing.

    Why two round-trips: the operator passes the zone *name*, not the
    file path; the path varies by deployment (Debian default
    ``/etc/bind/...``, chroot ``/var/cache/bind/...``, custom roots in
    operator-managed views) and the source of truth is what
    ``named-checkconf -p`` already canonicalised. The alternative
    (assuming a fixed path layout) would route around the operator's
    own configuration.

    Returns ``{zone, file, rows, total}``. The full row list lands
    inline -- the dispatcher's default
    :class:`meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
    spills large zonefiles to handles per the module-docstring
    rationale; this handler emits the raw shape so the reducer has the
    inlined sample size + total count to drive its threshold check.
    """
    zone_name: str = params["zone"]
    # Step 1 -- locate the zonefile path. Reuses the same
    # named-checkconf invocation ``bind9.zone.list`` uses; cheap on
    # bind9 (parse-only, no zone transfer), so a duplicate call is
    # acceptable in v0.2.
    checkconf = await connector._run_command(target, "named-checkconf -p", raw_jwt="")
    checkconf_stdout = (checkconf.stdout or "") if hasattr(checkconf, "stdout") else ""
    checkconf_output = checkconf_stdout if isinstance(checkconf_stdout, str) else ""
    zonefile_path = _resolve_zonefile_path(checkconf_output, zone_name)

    # Step 2 -- read the file. ``cat`` is the canonical read primitive;
    # bind9 zonefiles are world-readable on every supported distribution
    # (the daemon needs to ``open(2)`` them as the ``bind`` user, so
    # they ship 644 by default). No sudo needed for reads.
    #
    # The path is single-quoted to defend against shell-metacharacters
    # in operator-managed zonefile paths (``$INCLUDE``-aliased subtrees
    # under ``/var/cache/bind/views/``, etc.). bind9 + named-checkconf
    # never emit single quotes in a ``file "..."`` directive, so the
    # quote-and-escape shape is a defence-in-depth measure rather than
    # a load-bearing safety primitive (the safe-sudo primitive in
    # :mod:`connector` carries that role for write paths).
    quoted_path = "'" + zonefile_path.replace("'", "'\\''") + "'"
    cat_proc = await connector._run_command(target, f"cat {quoted_path}", raw_jwt="")
    cat_stdout = (cat_proc.stdout or "") if hasattr(cat_proc, "stdout") else ""
    zonefile_text = cat_stdout if isinstance(cat_stdout, str) else ""

    # ``origin`` for ``dns.zone.from_text`` -- always pass the normalised
    # zone name with a trailing dot; absolute origin is the safe default
    # so ``@`` and bare names in the zonefile resolve correctly.
    origin = zone_name if zone_name.endswith(".") else zone_name + "."
    try:
        rows = parse_zonefile(zonefile_text, origin=origin)
    except dns.exception.DNSException:
        # Surfacing the original exception class lets the dispatcher's
        # ``connector_error`` envelope carry the dnspython error type
        # in ``extras.exception_class`` so callers can distinguish
        # "zonefile syntax broken" (operator should fix on the
        # nameserver) from "ssh unreachable" (different remediation).
        raise

    return {
        "zone": zone_name,
        "file": zonefile_path,
        "rows": rows,
        "total": len(rows),
    }


# ---------------------------------------------------------------------------
# Parameter schemas + LLM instructions
# ---------------------------------------------------------------------------


BIND9_ZONE_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


_BIND9_ZONE_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "file": {"type": ["string", "null"]},
                    "type": {"type": ["string", "null"]},
                },
                "required": ["name", "file", "type"],
                "additionalProperties": False,
            },
            "description": (
                "One row per zone declared in the active bind9 "
                "configuration. Order follows the ``named-checkconf -p`` "
                "canonical output (typically declaration order)."
            ),
        },
        "total": {
            "type": "integer",
            "description": (
                "Row count emitted in ``rows``. Useful as the "
                "pre-reduction count -- the dispatcher's default "
                "JsonFluxReducer tracks both the inlined sample size "
                "and this total."
            ),
        },
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


BIND9_ZONE_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator asks 'what zones does this nameserver "
        "serve?' or needs the zone -> zonefile path mapping before "
        "issuing ``bind9.zone.read`` / ``bind9.config.show``. Read-only; "
        "parses ``named-checkconf -p`` output rather than walking the "
        "filesystem so the result reflects the active config, not the "
        "on-disk file tree (the two diverge when an operator stages a "
        "fragment without reloading)."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'rows': [{name, file, type}], 'total': <int>}. ``type`` is "
        "the zone's declared role (``master`` / ``slave`` / "
        "``forward`` / ...); ``file`` is the zonefile path as bind9 "
        "sees it (absolute, post-chroot resolution)."
    ),
}


BIND9_ZONE_READ_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "zone": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Zone name as it appears in ``named-checkconf -p``. "
                "Trailing dot optional -- the handler normalises. "
                "Examples: ``evba.lab``, ``50.5.10.in-addr.arpa``."
            ),
        },
    },
    "required": ["zone"],
    "additionalProperties": False,
}


_BIND9_ZONE_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "zone": {"type": "string"},
        "file": {"type": "string"},
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
    "required": ["zone", "file", "rows", "total"],
    "additionalProperties": False,
}


BIND9_ZONE_READ_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator asks 'what records are in zone X?' or "
        "'show me the zonefile for X'. Read-only. The handler resolves "
        "the zonefile path via ``named-checkconf -p`` so the operator "
        "passes the zone *name*, not the file path. Each rrset member "
        "lands as its own row (an MX rrset with two priorities yields "
        "two rows, not one row with a list rdata). For large zones the "
        "full row list lands inline; the dispatcher's default "
        "JsonFluxReducer wraps a zone with thousands of records in a "
        "``ResultHandle`` and the agent drills in via "
        "``result_query`` / ``result_aggregate`` rather than receiving "
        "the full row list in the inline result."
    ),
    "parameter_hints": {
        "zone": (
            "Required. The zone name as bind9 sees it; trailing dot "
            "optional. Look it up via ``bind9.zone.list`` if the "
            "operator's question doesn't name a zone explicitly."
        ),
    },
    "output_shape": (
        "{'zone': <str>, 'file': <str>, 'rows': [{name, ttl, class, "
        "type, rdata}], 'total': <int>}. ``name`` is the FQDN "
        "(trailing-dot absolute); ``ttl`` is integer seconds; ``class`` "
        "/ ``type`` are the canonical strings (``IN`` / ``A`` / "
        "``AAAA`` / ``CNAME`` / ``MX`` / ``TXT`` / ``SOA`` / ``NS`` / "
        "...). ``rdata`` is the canonical zonefile text representation "
        "of the record's value."
    ),
}


# ---------------------------------------------------------------------------
# Op metadata table
# ---------------------------------------------------------------------------


ZONE_OPS: tuple[Bind9Op, ...] = (
    Bind9Op(
        op_id="bind9.zone.list",
        handler_attr="bind9_zone_list",
        summary="List zones declared on the bind9 nameserver with zonefile path + role.",
        description=(
            "Runs ``named-checkconf -p`` over SSH and parses the "
            "canonicalised config dump into one row per top-level "
            '``zone "<name>" { ... };`` block. Each row carries the '
            "zone name, its zonefile path (the post-canonicalisation "
            "value bind9 resolves at zone-load time), and the declared "
            "role (``master`` / ``slave`` / ``forward`` / ``stub`` / "
            "``hint`` / ...). The handler scopes itself to the active "
            "config, not the on-disk file tree -- a fragment staged "
            "under ``/etc/bind/`` but not yet referenced from "
            "``named.conf`` does not appear. Read-only; safe to call on "
            "any healthy bind9 target."
        ),
        parameter_schema=BIND9_ZONE_LIST_PARAMETER_SCHEMA,
        response_schema=_BIND9_ZONE_LIST_RESPONSE_SCHEMA,
        group_key="zone",
        tags=("read-only", "zone", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=BIND9_ZONE_LIST_LLM_INSTRUCTIONS,
    ),
    Bind9Op(
        op_id="bind9.zone.read",
        handler_attr="bind9_zone_read",
        summary="Read the records of a zone -- {name, ttl, class, type, rdata} rows.",
        description=(
            "Resolves the zonefile path for the requested zone via "
            "``named-checkconf -p`` (so the operator passes the zone "
            "name, not the path), reads the file with ``cat``, and "
            "parses it via dnspython's ``dns.zone.from_text``. Returns "
            "one row per rrset member -- an A rrset with three "
            "addresses yields three rows, an MX rrset with two "
            "priorities yields two rows. Useful for the operator "
            "questions 'what's the current value of <fqdn>?' and "
            "'list everything in zone X'. The handler emits the full "
            "row list inline; the dispatcher's default JsonFluxReducer "
            "wraps large zones in a result handle that the "
            "agent drills into via ``result_query`` / "
            "``result_aggregate`` rather than receiving the full row "
            "list. Read-only; safe against any zone bind9 has loaded."
        ),
        parameter_schema=BIND9_ZONE_READ_PARAMETER_SCHEMA,
        response_schema=_BIND9_ZONE_READ_RESPONSE_SCHEMA,
        group_key="zone",
        tags=("read-only", "zone", "records"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=BIND9_ZONE_READ_LLM_INSTRUCTIONS,
    ),
)
