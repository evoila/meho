# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""bind9 record-read op -- ``bind9.record.get``.

G3.4-T2 (#588) of Initiative #367. Read-only DNS lookup: ``dig
@localhost <fqdn> [<type>]`` against the local resolver, parsed into a
structured record list.

T3 (#589) and T4 (#590) will append write ops (``bind9.record.add`` /
``bind9.record.remove``) to this module against the same registration
shape.

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

import re
import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.bind9.ops import Bind9Op

if TYPE_CHECKING:
    from meho_backplane.connectors.bind9.connector import Bind9Connector

__all__ = [
    "BIND9_RECORD_GET_LLM_INSTRUCTIONS",
    "BIND9_RECORD_GET_PARAMETER_SCHEMA",
    "RECORD_OPS",
    "bind9_record_get",
    "parse_dig_answer",
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
)
