# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: per-op modules colocate handler + parameter_schema +
# LLM_INSTRUCTIONS + Bind9Op metadata so all of `bind9.record.*`'s
# operator/agent surface is grep-able in one file; splitting would
# scatter the load-bearing G3.4-T3 atomic-apply audit-payload binding
# across multiple translation units. Function-size warnings on
# bind9_record_add / bind9_record_remove accepted: each is a linear
# "resolve zone → read zonefile → transform → atomic_apply" sequence
# whose readability would degrade if split for cosmetics.

"""bind9 record ops -- read (``record.get``) + writes (``record.add`` / ``record.remove``).

G3.4-T2 (#588) of Initiative #367 landed the read op (``dig @localhost
<fqdn> [<type>]``). G3.4-T3 (#589) adds the symmetric write ops:

* ``bind9.record.add <fqdn> <ip> [--zone <name>] [--type A|AAAA]
  [--view <name>]`` -- atomic stage-validate-commit-reload-verify-
  rollback against the affected zonefile via :mod:`._atomic`. Verify
  predicate runs ``dig @localhost <fqdn>`` and asserts the new IP
  appears in the answer. ``safety_level=caution`` (mutation; the
  production-path gate is G7/G10 policy territory).
* ``bind9.record.remove <fqdn> [--zone <name>] [--view <name>]`` --
  symmetric whole-name clear (every A + AAAA at the FQDN) with verify
  predicate = ``dig`` no longer resolves the FQDN. Promoted to
  ``safety_level=destructive`` + ``requires_approval=True`` by the #3247
  operator ruling: as the broadest DNS removal MEHO exposes it rides the
  same governed-delete gate as ``record.delete`` (park + human approval +
  preview-hash binding + a whole-name blast-radius statement, built by
  :func:`._ops_record_delete_preview._bind9_record_remove_preview`).

``--zone`` is optional. When omitted, the handler resolves the owning
zone from ``named-checkconf -p`` (the T2 zone parser) by longest-suffix
match against the FQDN; ambiguous (the FQDN matches two zones equally
deep) or unresolvable (no zone is a suffix of the FQDN) inputs raise
:class:`ZoneResolutionError` **before** any staging.

Split-horizon (#2897)
---------------------

On a nameserver that declares the same zone in more than one ``view``,
:func:`resolve_zone_target` disambiguates on the optional ``--view``
param: without it a multi-view zone is rejected ``ambiguous_view`` (the
error names the candidate views); with it the matching view's zonefile
is edited. A caller-supplied ``view`` also switches the verify predicate
from ``dig @localhost`` (view-blind -- answered by whichever view the
loopback source matches) to ``rndc zonestatus <zone> IN <view>``, which
confirms the staged serial loaded into *that* view.

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
import structlog.contextvars

from meho_backplane.connectors._shared.vault_creds import strip_credential_value
from meho_backplane.connectors.bind9._atomic import atomic_apply
from meho_backplane.connectors.bind9.ops import Bind9Op

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.bind9.connector import Bind9Connector

__all__ = [
    "BIND9_RECORD_ADD_LLM_INSTRUCTIONS",
    "BIND9_RECORD_ADD_PARAMETER_SCHEMA",
    "BIND9_RECORD_DELETE_LLM_INSTRUCTIONS",
    "BIND9_RECORD_DELETE_PARAMETER_SCHEMA",
    "BIND9_RECORD_GET_LLM_INSTRUCTIONS",
    "BIND9_RECORD_GET_PARAMETER_SCHEMA",
    "BIND9_RECORD_REMOVE_LLM_INSTRUCTIONS",
    "BIND9_RECORD_REMOVE_PARAMETER_SCHEMA",
    "RECORD_OPS",
    "RemoteCommandError",
    "ZoneResolutionError",
    "bind9_record_add",
    "bind9_record_delete",
    "bind9_record_get",
    "bind9_record_remove",
    "parse_dig_answer",
    "resolve_zone_for_fqdn",
    "resolve_zone_target",
]


# ---------------------------------------------------------------------------
# Remote-command failure surface
# ---------------------------------------------------------------------------


class RemoteCommandError(RuntimeError):
    """A remote ``_run_command`` invocation exited non-zero.

    The pre-T3 shape parsed ``proc.stdout`` regardless of
    ``proc.exit_status`` -- a failing ``named-checkconf -p`` (named
    not running, missing config, perms) degraded into "empty zone
    list" which then surfaced as :class:`ZoneResolutionError`
    ``unresolvable`` even though the FQDN itself was fine. CodeRabbit
    flagged the four sites where the shape leaked operational faults
    into the wrong error class; this is the typed surface every site
    now raises so the dispatcher's ``connector_error`` branch carries
    the real failure shape (command + exit_status + stderr) rather
    than guessing.
    """

    def __init__(self, command: str, exit_status: int, stderr: str) -> None:
        super().__init__(
            f"remote command {command!r} exited {exit_status}: {stderr.strip() or '<no stderr>'}"
        )
        self.command: str = command
        self.exit_status: int = exit_status
        self.stderr: str = stderr


def _require_zero_exit(proc: Any, *, command: str) -> str:
    """Return ``proc.stdout`` decoded as text, or raise on non-zero exit.

    Centralises the exit-status check so every remote-command call
    site routes failures through the same typed surface. The check
    runs **before** the caller parses stdout, so a failing command
    cannot silently degrade into "the parse returned empty".
    """
    exit_status = getattr(proc, "exit_status", 0)
    if exit_status != 0:
        stderr_raw = getattr(proc, "stderr", "")
        stderr_text = stderr_raw if isinstance(stderr_raw, str) else ""
        raise RemoteCommandError(command, int(exit_status), stderr_text)
    stdout_raw = getattr(proc, "stdout", "")
    return stdout_raw if isinstance(stdout_raw, str) else ""


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
    operator: Operator | None = None,
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
    proc = await connector._run_command(target, cmd, operator=operator)
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

    Four flavours:

    * **unresolvable** -- no configured zone is a suffix of the FQDN
      (or the matched zone carries no ``file`` directive). The operator
      named a record outside any writable zone bind9 serves.
    * **ambiguous** -- two (or more) *distinct* configured zone names
      tie for the longest-suffix match. Should never happen with a real
      bind9 config, but the parser handles arbitrary input and the
      check is cheap defence-in-depth.
    * **ambiguous_view** -- the FQDN resolves to a single zone name,
      but that zone is declared in more than one ``view`` (split-horizon
      DNS) and no ``view`` was supplied to disambiguate. ``candidates``
      carries the view names. The caller passes ``view`` to pick one.
    * **view_not_found** -- a ``view`` was supplied but the zone is not
      declared in it. ``candidates`` carries the views the zone *is*
      declared in.

    The handler raises this **before** any staging, so the dispatcher's
    ``invalid_params`` envelope reports the rejection with zero side
    effects on the remote tree. The :class:`ValueError` base lets the
    dispatcher's ``connector_error`` branch use its standard exception-
    class extras path without a custom shim. ``str(exc)`` is a
    human-actionable sentence (not the bare reason code) so the
    surfaced ``exception_message`` tells the operator what to do next.
    """

    def __init__(self, reason: str, fqdn: str, candidates: list[str] | None = None) -> None:
        self.reason: str = reason
        self.fqdn: str = fqdn
        self.candidates: list[str] = candidates or []
        super().__init__(self._describe())

    def _describe(self) -> str:
        joined = ", ".join(self.candidates)
        if self.reason == "ambiguous_view":
            return (
                f"the zone owning {self.fqdn!r} is declared in multiple views "
                f"({joined}); pass ``view`` to pick the split-horizon copy to edit"
            )
        if self.reason == "view_not_found":
            return (
                f"no zone owning {self.fqdn!r} is declared in the requested view; "
                f"the zone is declared in view(s): {joined or '<none>'}"
            )
        if self.reason == "ambiguous":
            return f"multiple configured zones tie as the longest suffix of {self.fqdn!r}: {joined}"
        return f"no writable bind9 zone resolves for {self.fqdn!r}"


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

    DNS names are case-insensitive (RFC 1035 §2.3.3): ``API.EVBA.lab``
    must match the zone ``evba.lab`` the same way the running daemon
    treats them as equivalent. We lower-case both sides before the
    label comparison so a mixed-case operator input is not rejected
    as unresolvable. The returned zone name preserves the canonical
    lower-case form (zonefile paths derived from this value flow
    through ``named-checkconf -p`` which already normalises to
    lower-case in its output, so the round-trip is stable).
    """
    fqdn_normalised = fqdn.rstrip(".").lower()
    fqdn_labels = fqdn_normalised.split(".")
    best_match: str | None = None
    best_label_count = -1
    ties: list[str] = []
    for zone in zones:
        zone_normalised = zone.rstrip(".").lower()
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


def resolve_zone_target(
    rows: list[dict[str, Any]],
    *,
    fqdn: str,
    explicit_zone: str | None,
    explicit_view: str | None,
) -> tuple[str, str, str | None]:
    """Resolve ``(zone_name, zonefile_path, view)`` for a write target.

    Pure function over the parsed ``named-checkconf -p`` rows (T2's
    :func:`~meho_backplane.connectors.bind9.ops_zone.parse_named_checkconf_zones`
    shape ``{name, file, type, view}``). Split-horizon aware: the same
    zone name may appear once per ``view``.

    Zone selection:

    * ``explicit_zone`` set -> that zone name (trailing-dot / case
      normalised).
    * otherwise -> longest-suffix match via :func:`resolve_zone_for_fqdn`
      over the **distinct** zone names, so a zone declared in N views no
      longer ties with itself and spuriously reports ``ambiguous`` (the
      #2897 failure mode).

    View disambiguation, over the rows whose name matches the zone and
    that carry a ``file`` directive:

    * ``explicit_view`` set -> restrict to that view; no match raises
      ``view_not_found``.
    * ``explicit_view`` unset and the zone lives in exactly one view
      (or none) -> that row.
    * ``explicit_view`` unset and the zone lives in >1 view -> raise
      ``ambiguous_view`` (the caller must pass ``view``).

    ``view`` in the returned tuple is the enclosing view name, or
    ``None`` for a zone declared outside any view (a no-views
    deployment). It drives the view-aware verify predicate the handlers
    build.
    """

    def _name(row: dict[str, Any]) -> str:
        return str(row["name"]).rstrip(".").lower()

    if explicit_zone is not None:
        zone_name = explicit_zone.rstrip(".").lower()
    else:
        zone_name = resolve_zone_for_fqdn(sorted({_name(row) for row in rows}), fqdn)

    candidates = [row for row in rows if _name(row) == zone_name and row.get("file")]
    if not candidates:
        # No matching zone with a writable ``file`` directive -- a hint /
        # forward zone, an unconfigured explicit ``zone``, or an FQDN
        # outside every served zone.
        raise ZoneResolutionError("unresolvable", fqdn=fqdn)

    declared_views = sorted({str(r["view"]) for r in candidates if r.get("view")})
    if explicit_view is not None:
        matched = [row for row in candidates if row.get("view") == explicit_view]
        if not matched:
            raise ZoneResolutionError("view_not_found", fqdn=fqdn, candidates=declared_views)
        chosen = matched[0]
    elif len({row.get("view") for row in candidates}) > 1:
        raise ZoneResolutionError("ambiguous_view", fqdn=fqdn, candidates=declared_views)
    else:
        chosen = candidates[0]

    view = chosen.get("view")
    return zone_name, str(chosen["file"]), (str(view) if view is not None else None)


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
# Scoped single-record delete (governed-delete tier, #3231)
# ---------------------------------------------------------------------------
#
# ``bind9.record.delete`` (safety_level=destructive) deletes exactly ONE
# record scoped by (zone, name, type, and — where the name carries more than
# one value of that type — rdata). It is deliberately narrower than
# ``bind9.record.remove`` (which clears every A + AAAA at a name in one
# write): a governed delete on the destructive tier must name the single
# record that dies, never a zone-wide or multi-value sweep. Both now sit on
# the destructive tier (``record.remove`` promoted by #3247) — ``delete`` is
# the scalpel, ``remove`` the whole-name shear.


def _canon_rdata(record_type: str, value: str) -> str:
    """Return the canonical wire-form text of *value* for *record_type*.

    dnspython normalises rdata to a canonical form (IPv6 zero-compression,
    lower-case hex, etc.), so ``2001:DB8:0:0::1`` and ``2001:db8::1`` compare
    equal. Raises :class:`dns.exception.DNSException` on a value that is not
    valid rdata for the type.
    """
    rdtype = dns.rdatatype.from_text(record_type)
    return dns.rdata.from_text(dns.rdataclass.IN, rdtype, value).to_text()


def _rdata_matches(candidate: str, target: str, record_type: str) -> bool:
    """``True`` when *candidate* and *target* are the same rdata value.

    Compares the raw strings first (cheap, exact), then falls back to the
    canonical wire form so an operator-typed value in a non-canonical
    spelling still matches the stored record. A *target* that is not valid
    rdata for the type never matches (fail-closed: an unparseable
    disambiguator resolves to "no such record", not a wrong deletion).
    """
    if candidate == target:
        return True
    try:
        return _canon_rdata(record_type, candidate) == _canon_rdata(record_type, target)
    except dns.exception.DNSException:
        return False


def _find_record_matches(
    zonefile_text: str,
    *,
    zone_name: str,
    fqdn: str,
    record_type: str,
) -> list[str]:
    """Return the rdata values currently at ``(fqdn, record_type)`` in the zone.

    Pure function over the zonefile text. One entry per record value in the
    rrset (canonical wire form), in a stable sorted order so the blast-radius
    children and the ambiguity candidates are deterministic. An absent name /
    rrset yields ``[]`` (a legitimate "record does not exist" statement, not
    an error). Used by both the preview builder (blast radius + ambiguity
    visibility) and the handler (fail-closed re-read) so the two paths cannot
    drift on what "matches".
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
    rds = zone.get_rdataset(name, rdtype)
    if rds is None:
        return []
    return sorted(rd.to_text() for rd in rds)


def _resolve_delete_target(
    matches: list[str],
    *,
    record_type: str,
    rdata_param: str | None,
) -> tuple[str | None, str]:
    """Resolve the single rdata value to delete, or a refusal status.

    Returns ``(target_rdata, status)`` where ``status`` is one of:

    * ``"ok"`` — exactly one record is targeted; ``target_rdata`` is the
      stored value to remove.
    * ``"not_found"`` — no record matches (name/type absent, or a supplied
      ``rdata`` is not among the values).
    * ``"ambiguous"`` — the name carries more than one value of the type and
      no ``rdata`` was supplied to pick one; ``target_rdata`` is ``None`` and
      the caller names ``matches`` as the candidates.

    Fail-closed by construction: it never returns ``"ok"`` for anything but a
    single, uniquely-identified record.
    """
    if not matches:
        return None, "not_found"
    if rdata_param is not None:
        hits = [m for m in matches if _rdata_matches(rdata_param, m, record_type)]
        if len(hits) == 1:
            return hits[0], "ok"
        if not hits:
            return None, "not_found"
        # More than one stored value canonicalises to the same rdata — not a
        # shape a real zonefile produces (dnspython de-dupes rrsets), but the
        # branch stays fail-closed rather than deleting an arbitrary one.
        return None, "ambiguous"
    if len(matches) == 1:
        return matches[0], "ok"
    return None, "ambiguous"


def _delete_one_record_from_zonefile(
    zonefile_text: str,
    *,
    zone_name: str,
    fqdn: str,
    record_type: str,
    rdata: str,
) -> str:
    """Return new zonefile text with exactly the one ``(fqdn, type, rdata)`` gone.

    Pure transformation — parses with dnspython, removes the single rdata
    from the rrset (deleting the whole rrset only when that was its last
    value), bumps the SOA serial, renders. Every other record at the name —
    including other values of the *same* type — is preserved. The caller
    (:func:`bind9_record_delete`) resolves *rdata* to a value it has already
    confirmed is present via :func:`_resolve_delete_target`, so this never
    silently no-ops.

    Raises :class:`ValueError` if the resolved rdata is unexpectedly absent
    (a between-read race the handler's re-read is meant to catch) rather than
    bumping the serial on a phantom delete.
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
    rds = zone.get_rdataset(name, rdtype)
    if rds is None:
        raise ValueError(f"no {record_type} rrset at {fqdn!r} to delete from")
    victim = next(
        (rd for rd in rds if _rdata_matches(rdata, rd.to_text(), record_type)),
        None,
    )
    if victim is None:
        raise ValueError(f"{record_type} value {rdata!r} not present at {fqdn!r}")
    rds.remove(victim)  # type: ignore[no-untyped-call]
    if len(rds) == 0:
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


# bind9 master zonefiles the write ops edit are Internet (IN) class.
# ``rndc`` requires the class token *before* a view name
# (``rndc reload <zone> IN <view>`` / ``rndc zonestatus <zone> IN <view>``);
# CH / HS zones are exotic and out of scope for record writes.
_ZONE_CLASS = "IN"


def _soa_serial_from_text(zonefile_text: str, zone_name: str) -> int:
    """Return the SOA serial of the rendered *zonefile_text*.

    The write transforms bump the serial in place; the view-aware verify
    predicate asserts the running daemon loaded *this* revision into the
    target view. Re-parsing with dnspython (rather than a regex over the
    multi-form SOA grammar) reuses the same round-trip the transform
    used, so the serial we assert on is exactly the one named reports.
    """
    origin = zone_name if zone_name.endswith(".") else zone_name + "."
    zone = dns.zone.from_text(zonefile_text, origin=origin, relativize=False, check_origin=False)
    zone_origin = zone.origin
    assert zone_origin is not None, "zone parsed from text must carry an origin"
    return int(zone.find_rdataset(zone_origin, dns.rdatatype.SOA)[0].serial)


def _zonestatus_serial_verify(zone_name: str, view: str, serial: int) -> str:
    """View-precise verify predicate for a write into a named ``view``.

    ``dig @localhost`` is view-*blind*: on a split-horizon server it is
    answered by whichever ``view`` matches the loopback source address,
    which need not be the view whose zonefile we staged -- the #2897
    verify-rollback failure. ``rndc zonestatus <zone> IN <view>`` names
    the view explicitly and reports the serial it currently serves, so
    asserting it equals the staged serial confirms named loaded our
    revision into *that* view (``named-checkzone`` in the validate step
    already proved the record is in the file). A bounded poll absorbs
    the small window between ``rndc reload`` returning and the zone
    being live. On failure the last ``zonestatus`` output is echoed so
    the surfaced ``AtomicApplyError`` detail is diagnosable -- the
    silent ``grep -q`` predicate is why #2897 saw an empty detail.
    """
    quoted_zone = shlex.quote(zone_name)
    quoted_view = shlex.quote(view)
    zonestatus = f"rndc zonestatus {quoted_zone} {_ZONE_CLASS} {quoted_view}"
    # Anchored, whitespace-tolerant so ``serial: <N>`` matches whatever
    # indentation the rndc build emits and never a ``signed serial:`` line.
    serial_re = f"^[[:space:]]*serial: {serial}[[:space:]]*$"
    # ``if ... then exit 0; fi`` rather than ``... && exit 0`` so the
    # predicate is unambiguously safe under the pipeline's ``set -e``:
    # a grep miss must fall through to the next poll and, after the
    # loop, to the diagnostic -- never abort the script early.
    return (
        "STATUS=''; "
        "for _ in 1 2 3 4 5; do "
        f"STATUS=$({zonestatus} 2>&1) || true; "
        f'if printf "%s\\n" "$STATUS" | grep -qE "{serial_re}"; then exit 0; fi; '
        "sleep 0.3; "
        "done; "
        'printf "zone %s view %s not at staged serial '
        f'{serial} after reload; last rndc zonestatus:\\n%s\\n" '
        f'{quoted_zone} {quoted_view} "$STATUS"; '
        "exit 1"
    )


def _dig_add_verify(fqdn: str, ip: str, record_type: str) -> str:
    """Record-level verify predicate for a no-views / single-view add.

    ``dig @localhost <fqdn> <type> +short`` emits one rdata per line;
    ``grep -qxF`` (literal, whole-line) asserts the new IP is present
    without false-matching a substring. On mismatch the observed
    ``dig`` output is echoed so the ``AtomicApplyError`` detail carries
    the real reason (#2897 -- the prior silent predicate surfaced an
    empty detail).
    """
    quoted_fqdn = shlex.quote(fqdn)
    quoted_ip = shlex.quote(ip)
    # ``if ... then exit 0; fi`` so a grep miss falls through to the
    # diagnostic under the pipeline's ``set -e`` (not an early abort).
    return (
        f"ANSWER=$(dig @localhost {quoted_fqdn} {record_type} +short 2>&1) || true; "
        f'if printf "%s\\n" "$ANSWER" | grep -qxF {quoted_ip}; then exit 0; fi; '
        f'printf "expected {record_type} %s in the answer for %s; dig +short returned:'
        f'\\n%s\\n" {quoted_ip} {quoted_fqdn} "$ANSWER"; '
        "exit 1"
    )


def _dig_remove_verify(fqdn: str) -> str:
    """Record-level verify predicate for a no-views / single-view remove.

    The FQDN must resolve to neither an A nor an AAAA answer. ``+short``
    exits 0 on empty output, so emptiness is asserted explicitly. On
    failure the residual answers are echoed for a diagnosable detail
    (#2897).
    """
    quoted_fqdn = shlex.quote(fqdn)
    return (
        f"A=$(dig @localhost {quoted_fqdn} A +short 2>&1) || true; "
        f"AAAA=$(dig @localhost {quoted_fqdn} AAAA +short 2>&1) || true; "
        'if [ -z "$A" ] && [ -z "$AAAA" ]; then exit 0; fi; '
        'printf "%s still resolves after remove (A: %s AAAA: %s)\\n" '
        f'{quoted_fqdn} "$A" "$AAAA"; '
        "exit 1"
    )


def _dig_value_absent_verify(fqdn: str, record_type: str, rdata: str) -> str:
    """Scoped verify predicate for ``record.delete`` — one value gone, others kept.

    The post-delete verification read for a single-record scoped delete: the
    deleted ``(type, rdata)`` value must be absent from
    ``dig @localhost <fqdn> <type> +short``, while any *other* values at the
    same name/type legitimately remain (this is why ``record.remove``'s
    "resolves to nothing" predicate is wrong here). ``grep -qxF`` is literal
    whole-line so a substring cannot false-match. ``record_type`` is one of
    the enum-validated A / AAAA tokens (never operator-free-text), so it is
    safe to interpolate; ``fqdn`` / ``rdata`` are ``shlex.quote``-wrapped. On
    a still-present value the observed ``dig`` output is echoed so the
    ``AtomicApplyError`` detail is diagnosable (the #2897 discipline).
    """
    quoted_fqdn = shlex.quote(fqdn)
    quoted_rdata = shlex.quote(rdata)
    # ``if <present> then <diagnose>; exit 1; fi; exit 0`` — a grep miss (the
    # value is gone, the success case) falls through to ``exit 0`` and never
    # aborts early under the pipeline's ``set -e``.
    return (
        f"ANSWER=$(dig @localhost {quoted_fqdn} {record_type} +short 2>&1) || true; "
        f'if printf "%s\\n" "$ANSWER" | grep -qxF {quoted_rdata}; then '
        f'printf "{record_type} value %s still resolves for %s after delete; '
        f'dig +short returned:\\n%s\\n" {quoted_rdata} {quoted_fqdn} "$ANSWER"; '
        "exit 1; fi; "
        "exit 0"
    )


async def _resolve_zone_and_path(
    connector: Bind9Connector,
    target: Any,
    *,
    fqdn: str,
    explicit_zone: str | None,
    explicit_view: str | None = None,
    operator: Operator | None = None,
) -> tuple[str, str, str | None]:
    """Return ``(zone_name, zonefile_path, view)`` for *fqdn*.

    Runs ``named-checkconf -p`` once, parses the zone rows with T2's
    view-attributing parser, and delegates the split-horizon-aware
    selection to the pure :func:`resolve_zone_target`. Shared by the
    add / remove handlers so the two paths cannot drift. ``view`` is
    the enclosing view name (``None`` on a no-views deployment) and is
    what the handlers thread into the view-aware verify predicate.

    Lazy-imports the zone parser to avoid a circular import (T2's
    ``ops_zone`` imports from ``ops`` which transitively imports this
    module); the lazy shape mirrors the connector's registration walk.
    """
    from meho_backplane.connectors.bind9.ops_zone import (
        parse_named_checkconf_zones,
    )

    cmd = "named-checkconf -p"
    proc = await connector._run_command(target, cmd, operator=operator)
    output = _require_zero_exit(proc, command=cmd)
    rows = parse_named_checkconf_zones(output)
    return resolve_zone_target(
        rows, fqdn=fqdn, explicit_zone=explicit_zone, explicit_view=explicit_view
    )


async def _read_zonefile_text(
    connector: Bind9Connector,
    target: Any,
    zonefile_path: str,
    operator: Operator | None = None,
) -> str:
    """Read the current zonefile text via ``cat`` (no sudo needed).

    bind9 zonefiles are world-readable per T2's design. Reuses the
    same shell-quote pattern T2's ``bind9.zone.read`` uses for
    path-safety.
    """
    quoted_path = "'" + zonefile_path.replace("'", "'\\''") + "'"
    cmd = f"cat {quoted_path}"
    # ``cat`` is non-zero on missing-file / permission-denied; a silent
    # degrade to "" would let the dnspython parser surface the read
    # failure as a malformed-zonefile error and lose the real root
    # cause (the file isn't readable). Route through the typed surface
    # so the dispatcher's connector_error envelope carries the actual
    # remote exit status + stderr.
    cat_proc = await connector._run_command(target, cmd, operator=operator)
    return _require_zero_exit(cat_proc, command=cmd)


async def bind9_record_add(
    connector: Bind9Connector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``bind9.record.add`` -- atomic A/AAAA record write.

    Sequence:

    1. Resolve owning zone (via ``zone`` param, or longest-suffix
       match against ``named-checkconf -p``) and its ``view`` --
       split-horizon aware, so a zone declared in multiple views is
       disambiguated by the optional ``view`` param.
    2. Validate the IP matches the requested record type.
    3. Read the current zonefile (``cat <path>``).
    4. Transform via :func:`_add_record_to_zonefile` (dnspython
       parse + add + SOA bump + render).
    5. :func:`atomic_apply` stages the new zonefile, runs
       ``named-checkzone <zone> <path>``, ``rndc reload``, and the
       view-aware verify predicate; rolls back on any failure.

    Returns ``{fqdn, ip, type, zone, file, view, op_class,
    result_state_before, result_state_after}``. ``op_class="write"`` is
    set explicitly even though
    :func:`~meho_backplane.broadcast.events.classify_op` derives the
    same value from the op-id suffix -- the dual signal is what the
    audit-replay path (G8.2) reads to reconstruct the change without
    re-parsing the op-id namespace.

    Raises :class:`ZoneResolutionError` when the FQDN can't be uniquely
    resolved -- including ``ambiguous_view`` when the zone is declared in
    multiple views and no ``view`` was supplied (pre-stage; no remote IO
    past the ``named-checkconf -p`` lookup). Raises
    :class:`AtomicApplyError` on any rollback path.
    """
    fqdn: str = params["fqdn"]
    ip: str = params["ip"]
    record_type: str = params.get("type", "A").upper()
    explicit_zone: str | None = params.get("zone")
    explicit_view: str | None = params.get("view")

    if record_type not in _WRITE_SUPPORTED_TYPES:
        raise ValueError(
            f"record.add only supports A / AAAA; got type={record_type!r}. "
            f"CNAME / MX / TXT writes are out of scope for v0.2."
        )
    _validate_ip_for_type(ip, record_type)

    sudo_password = await _sudo_password_from_target(connector, target, operator)
    zone_name, zonefile_path, view = await _resolve_zone_and_path(
        connector,
        target,
        fqdn=fqdn,
        explicit_zone=explicit_zone,
        explicit_view=explicit_view,
        operator=operator,
    )
    current_text = await _read_zonefile_text(connector, target, zonefile_path, operator)

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

    # A caller-supplied ``view`` means split-horizon disambiguation:
    # ``dig @localhost`` may be answered by a different view than the one
    # we edited (#2897), so assert the staged serial loaded into the
    # named view via ``rndc zonestatus``. On a no-views / single-view
    # target keep the stronger record-level dig check.
    if explicit_view is not None:
        verify_cmd = _zonestatus_serial_verify(
            zone_name, explicit_view, _soa_serial_from_text(new_text, zone_name)
        )
    else:
        verify_cmd = _dig_add_verify(fqdn, ip, record_type)

    apply_result = await atomic_apply(
        connector,
        target,
        operator=operator,
        sudo_password=sudo_password,
        audit_slice_path=zonefile_path,
        zone_name=zone_name,
        staged_bytes=new_text.encode("utf-8"),
        verify_command=verify_cmd,
    )

    # Chassis-shape audit enrichment: bind ``audit_state_before`` /
    # ``audit_state_after`` so the dispatcher's audit-row payload
    # carries the zonefile-slice snapshots the G8.2 audit-replay
    # consumer reads. Mirrors the FastAPI middleware's ``audit_*``
    # contextvar pattern (see _resolve_audit_extras_from_contextvars
    # in operations/_audit.py).
    structlog.contextvars.bind_contextvars(
        audit_state_before=apply_result.state_before,
        audit_state_after=apply_result.state_after,
    )
    return {
        "fqdn": fqdn,
        "ip": ip,
        "type": record_type,
        "zone": zone_name,
        "file": zonefile_path,
        "view": view,
        "op_class": "write",
        "result_state_before": apply_result.state_before,
        "result_state_after": apply_result.state_after,
    }


async def bind9_record_remove(
    connector: Bind9Connector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``bind9.record.remove`` -- atomic A/AAAA record remove.

    Sequence: same as :func:`bind9_record_add` but the zonefile
    transform deletes the FQDN's A and AAAA rdatasets, and the verify
    predicate asserts the FQDN no longer resolves. Split-horizon aware:
    the optional ``view`` param disambiguates a zone declared in
    multiple views, and a caller-supplied ``view`` switches verify to
    the view-precise ``rndc zonestatus`` serial check (``dig @localhost``
    is view-blind -- #2897).

    Returns the same envelope shape as ``record.add`` (minus ``ip`` /
    ``type``), including the resolved ``view``.
    """
    fqdn: str = params["fqdn"]
    explicit_zone: str | None = params.get("zone")
    explicit_view: str | None = params.get("view")

    sudo_password = await _sudo_password_from_target(connector, target, operator)
    zone_name, zonefile_path, view = await _resolve_zone_and_path(
        connector,
        target,
        fqdn=fqdn,
        explicit_zone=explicit_zone,
        explicit_view=explicit_view,
        operator=operator,
    )
    current_text = await _read_zonefile_text(connector, target, zonefile_path, operator)

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

    # View-aware verify -- see bind9_record_add for the rationale. For a
    # targeted view the removal is confirmed by the staged serial being
    # loaded into that view; otherwise the FQDN must resolve to neither
    # an A nor an AAAA answer via the local resolver.
    if explicit_view is not None:
        verify_cmd = _zonestatus_serial_verify(
            zone_name, explicit_view, _soa_serial_from_text(new_text, zone_name)
        )
    else:
        verify_cmd = _dig_remove_verify(fqdn)

    apply_result = await atomic_apply(
        connector,
        target,
        operator=operator,
        sudo_password=sudo_password,
        audit_slice_path=zonefile_path,
        zone_name=zone_name,
        staged_bytes=new_text.encode("utf-8"),
        verify_command=verify_cmd,
    )

    # Chassis audit enrichment — see bind9_record_add for rationale.
    structlog.contextvars.bind_contextvars(
        audit_state_before=apply_result.state_before,
        audit_state_after=apply_result.state_after,
    )
    return {
        "fqdn": fqdn,
        "zone": zone_name,
        "file": zonefile_path,
        "view": view,
        "op_class": "write",
        "result_state_before": apply_result.state_before,
        "result_state_after": apply_result.state_after,
    }


def _delete_refusal(
    status: str,
    *,
    fqdn: str,
    record_type: str,
    rdata: str | None,
    zone: str | None,
    view: str | None,
    guidance: str,
    candidates: list[str] | None = None,
) -> dict[str, Any]:
    """Build a structured, fail-closed ``record.delete`` refusal envelope.

    Mirrors the destructive-tier refusal shape of ``vmware.composite.vm.destroy``
    (#3198): a ``status`` naming the refusal reason, ``deleted=False`` (never
    a silent success), and an operator-actionable ``guidance`` sentence.
    ``candidates`` carries the competing rdata values on the ``ambiguous``
    path so the operator can re-issue with the ``rdata`` that picks one.
    """
    return {
        "status": status,
        "deleted": False,
        "fqdn": fqdn,
        "type": record_type,
        "rdata": rdata,
        "zone": zone,
        "file": None,
        "view": view,
        "candidates": candidates or [],
        "op_class": "write",
        "result_state_before": None,
        "result_state_after": None,
        "guidance": guidance,
    }


async def bind9_record_delete(
    connector: Bind9Connector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``bind9.record.delete`` -- governed single-record delete (#3231).

    The bind9 arm of the governed-delete tier (decision
    ``docs/decisions/governed-delete-operations.md``): ``safety_level=
    destructive`` + ``requires_approval=True``, so it rides the hardest gate
    MEHO has — mandatory human approval (no agent path, no standing grant, no
    self-approval even under break-glass), a mandatory preview-hash binding,
    and a mandatory blast-radius statement (built by
    :func:`._ops_record_delete_preview._bind9_record_delete_preview`).

    Deletes **exactly one** record scoped by ``(zone, name, type, rdata?)`` —
    never zone-wide, never a multi-value sweep. ``type`` is required (unlike
    ``record.remove``, which clears both A and AAAA at a name); ``rdata`` is
    required only to disambiguate a name that carries more than one value of
    that type.

    **Fail-closed re-read at dispatch time (post-approval).** Like
    ``vm.destroy``'s power-state re-read, the handler re-resolves the target
    against live state — a record added/changed between park and approval is
    caught — and returns a structured refusal rather than a broad or wrong
    deletion:

    * ``unmanaged_zone`` — no writable zone this server serves owns the FQDN
      (or a split-horizon zone needs a ``view``); nothing is touched.
    * ``not_found`` — the ``(name, type[, rdata])`` resolves to no record;
      ``deleted=False`` (never a silent success).
    * ``ambiguous`` — the name carries multiple values of the type and no
      ``rdata`` was supplied; refused with the candidate values named.

    On a uniquely-resolved target it removes that one value via the atomic
    stage-validate-commit-reload-verify-rollback primitive. The verify
    predicate is the **post-delete verification read** — the deleted value
    must be absent from ``dig`` (a ``view`` switches it to the view-precise
    ``rndc zonestatus`` serial check, #2897) — so ``deleted=True`` is
    returned only after the record is confirmed gone; any verify miss rolls
    the zone back byte-identical and raises :class:`AtomicApplyError`.
    """
    fqdn: str = params["fqdn"]
    record_type: str = params["type"].upper()
    rdata_param: str | None = params.get("rdata")
    explicit_zone: str | None = params.get("zone")
    explicit_view: str | None = params.get("view")

    if record_type not in _WRITE_SUPPORTED_TYPES:
        raise ValueError(
            f"record.delete only supports A / AAAA; got type={record_type!r}. "
            f"CNAME / MX / TXT deletes are out of scope."
        )

    sudo_password = await _sudo_password_from_target(connector, target, operator)

    try:
        zone_name, zonefile_path, view = await _resolve_zone_and_path(
            connector,
            target,
            fqdn=fqdn,
            explicit_zone=explicit_zone,
            explicit_view=explicit_view,
            operator=operator,
        )
    except ZoneResolutionError as exc:
        return _delete_refusal(
            "unmanaged_zone",
            fqdn=fqdn,
            record_type=record_type,
            rdata=rdata_param,
            zone=explicit_zone,
            view=explicit_view,
            guidance=str(exc),
            candidates=exc.candidates,
        )

    current_text = await _read_zonefile_text(connector, target, zonefile_path, operator)
    try:
        matches = _find_record_matches(
            current_text, zone_name=zone_name, fqdn=fqdn, record_type=record_type
        )
    except dns.exception.DNSException as exc:
        raise ValueError(f"failed to parse zonefile for zone {zone_name!r}: {exc}") from exc

    target_rdata, status = _resolve_delete_target(
        matches, record_type=record_type, rdata_param=rdata_param
    )
    if status == "not_found":
        detail = f" with rdata {rdata_param!r}" if rdata_param is not None else ""
        return _delete_refusal(
            "not_found",
            fqdn=fqdn,
            record_type=record_type,
            rdata=rdata_param,
            zone=zone_name,
            view=view,
            guidance=(
                f"no {record_type} record at {fqdn!r}{detail} in zone {zone_name!r}; "
                "nothing deleted"
            ),
        )
    if status == "ambiguous":
        return _delete_refusal(
            "ambiguous",
            fqdn=fqdn,
            record_type=record_type,
            rdata=rdata_param,
            zone=zone_name,
            view=view,
            candidates=matches,
            guidance=(
                f"{fqdn!r} carries {len(matches)} {record_type} records "
                f"({', '.join(matches)}); pass ``rdata`` to name the single "
                "record to delete — this op never deletes more than one"
            ),
        )

    assert target_rdata is not None  # status == "ok" ⇒ a resolved value
    try:
        new_text = _delete_one_record_from_zonefile(
            current_text,
            zone_name=zone_name,
            fqdn=fqdn,
            record_type=record_type,
            rdata=target_rdata,
        )
    except (dns.exception.DNSException, ValueError) as exc:
        raise ValueError(f"failed to transform zonefile for zone {zone_name!r}: {exc}") from exc

    # View-aware verify — see bind9_record_add for the #2897 rationale. The
    # no-views path asserts the deleted VALUE is gone (others at the name may
    # remain), not that the name stops resolving.
    if explicit_view is not None:
        verify_cmd = _zonestatus_serial_verify(
            zone_name, explicit_view, _soa_serial_from_text(new_text, zone_name)
        )
    else:
        verify_cmd = _dig_value_absent_verify(fqdn, record_type, target_rdata)

    apply_result = await atomic_apply(
        connector,
        target,
        operator=operator,
        sudo_password=sudo_password,
        audit_slice_path=zonefile_path,
        zone_name=zone_name,
        staged_bytes=new_text.encode("utf-8"),
        verify_command=verify_cmd,
    )

    # Chassis audit enrichment — see bind9_record_add for rationale.
    structlog.contextvars.bind_contextvars(
        audit_state_before=apply_result.state_before,
        audit_state_after=apply_result.state_after,
    )
    return {
        "status": "deleted",
        "deleted": True,
        "fqdn": fqdn,
        "type": record_type,
        "rdata": target_rdata,
        "zone": zone_name,
        "file": zonefile_path,
        "view": view,
        "candidates": [],
        "op_class": "write",
        "result_state_before": apply_result.state_before,
        "result_state_after": apply_result.state_after,
        "guidance": None,
    }


async def _sudo_password_from_target(
    connector: Bind9Connector, target: Any, operator: Operator | None = None
) -> str:
    """Resolve the sudo password from the target's Vault secret.

    ``target.secret_ref`` is a Vault KV-v2 path string (#2155);
    resolution goes through the SSH adapter's
    :meth:`~meho_backplane.connectors.adapters.ssh.SshConnector._resolve_secret`
    under the operator's identity — the same seam SSH auth uses. The
    sudo password reuses the SSH password by default
    (consumer-wrapper convention): the lookup keys on a dedicated
    ``sudo_password`` field first and falls back to ``password`` so
    existing Vault secrets keep working.

    Raises :class:`ValueError` if no password is configured -- the
    safe-sudo primitive's invariant requires a non-empty single-line
    string and the connector cannot legitimately proceed otherwise.
    """
    secret = await connector._resolve_secret(target, operator)
    password = secret.get("sudo_password") or secret.get("password")
    if not password:
        raise ValueError(
            "the target's Vault secret carries no sudo_password / password; "
            "bind9 write ops require a sudo credential"
        )
    return strip_credential_value(password)


# ---------------------------------------------------------------------------
# Write-op parameter schemas + LLM instructions
# ---------------------------------------------------------------------------


_WRITE_WARNING = (
    "WARNING: this change is global and atomic. The atomic-apply "
    "primitive stages the new zonefile, runs ``named-checkzone``, "
    "``rndc reload``, and a verify predicate (a ``dig`` record check, "
    "or a view-precise ``rndc zonestatus`` serial check when a ``view`` "
    "is given); on any failure the pre-op ``/etc/bind/`` tree is "
    "restored byte-identical. On success the change is live for every "
    "consumer of this nameserver -- DNS has no per-caller scoping. "
    "``safety_level`` is ``caution`` (the production-path gate is "
    "G7/G10 policy territory keyed on this value)."
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
        "view": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Optional. The ``view`` block that owns the zone on a "
                "split-horizon nameserver, e.g. ``internal``. Required "
                "only when the zone is declared in more than one view "
                "(otherwise the resolve step rejects it with "
                "``ambiguous_view`` and lists the candidate views). "
                "Selects which view's zonefile is edited and switches "
                "verification to a view-precise ``rndc zonestatus`` "
                "check, since ``dig @localhost`` cannot target a view."
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
    "view": {"type": ["string", "null"]},
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
        "view",
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
        "view": (
            "Optional. Split-horizon view that owns the zone. Needed "
            "only when the zone is declared in multiple views; the "
            "resolve step rejects a multi-view zone with "
            "``ambiguous_view`` and names the candidate views to pass "
            "here."
        ),
    },
    "output_shape": (
        "{'fqdn', 'ip', 'type', 'zone', 'file', 'view', "
        "'op_class': 'write', "
        "'result_state_before': <prior-zonefile-text>, "
        "'result_state_after': <post-write-zonefile-text>}. "
        "``view`` is the resolved view (``null`` outside any view). "
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
        "view": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Optional. Split-horizon view that owns the zone, the "
                "same way ``record.add`` uses it. Required only when the "
                "zone is declared in more than one view."
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
        "view",
        "op_class",
        "result_state_before",
        "result_state_after",
    ],
    "additionalProperties": False,
}


_REMOVE_WARNING = (
    "GOVERNED WHOLE-NAME CLEAR (destructive tier, #3247). This op clears "
    "EVERY A and AAAA record at the name in one write — it is the broadest "
    "DNS removal MEHO exposes, so it rides the same hardest gate as "
    "``bind9.record.delete``: ``safety_level=destructive`` + "
    "``requires_approval=True`` — mandatory human approval always (no agent "
    "path, no standing grant, no self-approval even under break-glass), a "
    "mandatory preview-hash binding, and a mandatory blast-radius statement "
    "(the name plus EVERY A / AAAA value that dies) the four-eyes approver "
    "reads before deciding. Change is global and atomic: the atomic-apply "
    "primitive stages the new zonefile, runs ``named-checkzone`` + "
    "``rndc reload`` + a verify predicate that confirms the name no longer "
    "resolves, and rolls the ``/etc/bind/`` tree back byte-identical on any "
    "failure. When you need to retire a SINGLE record (one value, not the "
    "whole name), use ``bind9.record.delete`` instead — it is scoped to one "
    "(zone, name, type, rdata?) record."
)


BIND9_RECORD_REMOVE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Clear the entire name — every A and AAAA record at the given FQDN "
        "— under governance (an environment teardown that retires a host's "
        "whole forward resolution). " + _REMOVE_WARNING + " Use "
        "``bind9.record.get`` first to confirm the current state; removing a "
        "record bind9 doesn't actually serve is a no-op (verify still passes "
        "-- the FQDN doesn't resolve before or after)."
    ),
    "parameter_hints": {
        "fqdn": "Required. The FQDN to clear of ALL A / AAAA records.",
        "zone": (
            "Optional. Owning zone. Omit to let the handler pick "
            "the longest-suffix-matching zone automatically."
        ),
        "view": (
            "Optional. Split-horizon view that owns the zone. Needed "
            "only when the zone is declared in multiple views."
        ),
    },
    "output_shape": (
        "{'fqdn', 'zone', 'file', 'view', 'op_class': 'write', "
        "'result_state_before': <prior-zonefile-text>, "
        "'result_state_after': <post-remove-zonefile-text>}. "
        "``view`` is the resolved view (``null`` outside any view)."
    ),
}


_DELETE_WARNING = (
    "GOVERNED DELETE (destructive tier). This op deletes exactly ONE record "
    "scoped by (zone, name, type, and — when the name carries more than one "
    "value of that type — rdata); it never deletes zone-wide, a wildcard "
    "match, or a whole rrset. It is ``safety_level=destructive`` + "
    "``requires_approval=True``: the hardest gate MEHO has — mandatory human "
    "approval always (no agent path, no standing grant, no self-approval even "
    "under break-glass), a mandatory preview-hash binding, and a mandatory "
    "blast-radius statement (the exact zone / name / type / rdata that dies, "
    "plus the record's sibling values) the four-eyes approver reads before "
    "deciding. Change is global and atomic: the atomic-apply primitive stages, "
    "runs ``named-checkzone`` + ``rndc reload`` + a verify predicate that "
    "confirms the deleted value is gone, and rolls the ``/etc/bind/`` tree "
    "back byte-identical on any failure. Prefer ``bind9.record.delete`` over "
    "``bind9.record.remove`` when a shared zone must retire a single record "
    "under governance (environment teardown); ``record.remove`` is the "
    "governed destructive-tier whole-name clear (every A + AAAA at the name, "
    "#3247) — same gate, broader blast radius."
)


BIND9_RECORD_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fqdn": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Fully-qualified domain name of the record to delete, e.g. "
                "``api.example.test``. Trailing dot optional. The handler "
                "resolves the owning zone from ``named-checkconf -p`` by "
                "longest-suffix match unless ``zone`` is set explicitly; an "
                "FQDN outside every writable zone is refused "
                "``unmanaged_zone``."
            ),
        },
        "type": {
            "type": "string",
            "enum": sorted(_WRITE_SUPPORTED_TYPES),
            "description": (
                "Record type to delete — required. Only A and AAAA are "
                "supported. Required (unlike ``record.remove``, which clears "
                "both A and AAAA) because a governed delete must name the "
                "single record type that dies."
            ),
        },
        "rdata": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Optional. The exact record value to delete (e.g. the IP for "
                "an A record). Required only when the name carries more than "
                "one value of the type: without it a multi-value name is "
                "refused ``ambiguous`` with the candidate values named. "
                "Compared in canonical form, so a non-canonical spelling "
                "still matches."
            ),
        },
        "zone": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Optional. Owning zone name. When omitted, resolved by "
                "longest-suffix match against ``named-checkconf -p`` the same "
                "way ``record.add`` / ``record.remove`` do."
            ),
        },
        "view": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Optional. Split-horizon view that owns the zone. Required "
                "only when the zone is declared in more than one view "
                "(otherwise the resolve step refuses ``unmanaged_zone`` and "
                "lists the candidate views). Selects which view's zonefile is "
                "edited and switches verification to the view-precise "
                "``rndc zonestatus`` check."
            ),
        },
    },
    "required": ["fqdn", "type"],
    "additionalProperties": False,
}


_BIND9_RECORD_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["deleted", "not_found", "ambiguous", "unmanaged_zone"],
            "description": (
                "``'deleted'`` on a successful single-record delete; "
                "``'not_found'`` when the (name, type[, rdata]) resolves to no "
                "record (deleted=False, no silent success); ``'ambiguous'`` "
                "when the name carries multiple values of the type and no "
                "``rdata`` was given (candidates named); ``'unmanaged_zone'`` "
                "when no writable zone this server serves owns the FQDN."
            ),
        },
        "deleted": {
            "type": "boolean",
            "description": (
                "``True`` only after the record is verified gone (the "
                "atomic-apply verify predicate passed); ``False`` on every "
                "refusal path."
            ),
        },
        "fqdn": {"type": "string"},
        "type": {"type": "string", "enum": sorted(_WRITE_SUPPORTED_TYPES)},
        "rdata": {
            "type": ["string", "null"],
            "description": (
                "The deleted value on success; the requested rdata (or null) on a refusal."
            ),
        },
        "zone": {"type": ["string", "null"]},
        "file": {"type": ["string", "null"]},
        "view": {"type": ["string", "null"]},
        "candidates": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The competing rdata values on the ``ambiguous`` refusal; empty otherwise."
            ),
        },
        "op_class": {"type": "string", "enum": ["write"]},
        "result_state_before": {"type": ["string", "null"]},
        "result_state_after": {"type": ["string", "null"]},
        "guidance": {"type": ["string", "null"]},
    },
    "required": ["status", "deleted", "fqdn", "type"],
    "additionalProperties": False,
}


BIND9_RECORD_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Delete a single DNS record from a bind9-served zone under "
        "governance — the DNS-retirement leg of an environment teardown in a "
        "shared zone. " + _DELETE_WARNING
    ),
    "parameter_hints": {
        "fqdn": "Required. The FQDN of the record to delete. Trailing dot optional.",
        "type": "Required. ``A`` or ``AAAA`` — the single record type to delete.",
        "rdata": (
            "Optional. The exact value to delete; required only to "
            "disambiguate a name with more than one value of the type (the "
            "``ambiguous`` refusal names the candidates)."
        ),
        "zone": (
            "Optional. Owning zone. Omit to let the handler pick the "
            "longest-suffix-matching zone automatically."
        ),
        "view": (
            "Optional. Split-horizon view that owns the zone. Needed only "
            "when the zone is declared in multiple views."
        ),
    },
    "output_shape": (
        "{'status', 'deleted', 'fqdn', 'type', 'rdata', 'zone', 'file', "
        "'view', 'candidates', 'op_class': 'write', 'result_state_before', "
        "'result_state_after', 'guidance'}. ``deleted`` is True only after "
        "the delete is verified; ``status`` names the refusal on the "
        "not_found / ambiguous / unmanaged_zone paths."
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
            "FQDNs are rejected pre-staging. Split-horizon aware: on a "
            "zone declared in multiple views, pass ``view`` to pick the "
            "copy to edit (verification then targets that view via "
            "``rndc zonestatus``). " + _WRITE_WARNING
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
        summary="Clear ALL A + AAAA at an FQDN — governed destructive tier.",
        description=(
            "Atomic stage-validate-commit-reload-verify-rollback "
            "remove of every A and AAAA record at the given FQDN — a "
            "whole-name clear, not a single-record delete. Idempotent when "
            "the records are already absent (verify passes -- the FQDN "
            "doesn't resolve before or after). Split-horizon aware: pass "
            "``view`` to disambiguate a zone declared in multiple views. " + _REMOVE_WARNING
        ),
        parameter_schema=BIND9_RECORD_REMOVE_PARAMETER_SCHEMA,
        response_schema=_BIND9_RECORD_REMOVE_RESPONSE_SCHEMA,
        group_key="record",
        tags=("write", "record", "delete", "destructive", "atomic-apply"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=BIND9_RECORD_REMOVE_LLM_INSTRUCTIONS,
    ),
    Bind9Op(
        op_id="bind9.record.delete",
        handler_attr="bind9_record_delete",
        summary="Delete ONE governed record (zone/name/type[/rdata]) — destructive tier.",
        description=(
            "The bind9 arm of the governed-delete tier (#3231, decision "
            "docs/decisions/governed-delete-operations.md). Deletes exactly "
            "one record scoped by (zone, name, type, and — where the name "
            "carries multiple values of that type — rdata); never zone-wide, "
            "never a wildcard match, never a whole-rrset sweep. Atomic "
            "stage-validate-commit-reload-verify-rollback; the verify "
            "predicate confirms the deleted value is gone (deleted=True only "
            "then). Fail-closed structured refusals: ``not_found`` (no such "
            "record), ``ambiguous`` (multiple values, no rdata — candidates "
            "named), ``unmanaged_zone`` (no writable zone owns the FQDN). " + _DELETE_WARNING
        ),
        parameter_schema=BIND9_RECORD_DELETE_PARAMETER_SCHEMA,
        response_schema=_BIND9_RECORD_DELETE_RESPONSE_SCHEMA,
        group_key="record",
        tags=("write", "record", "delete", "destructive", "atomic-apply"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=BIND9_RECORD_DELETE_LLM_INSTRUCTIONS,
    ),
)
