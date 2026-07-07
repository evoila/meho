# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tier-1 named-pattern library for the connector-boundary redactor.

Initiative #805 (G11.4 Safety, C1), Task #1070 (T1). This module ships
the **deterministic, hot-path** half of the tiered redaction design
sketched in #805 §Approach: a small, fixed set of regexes targeting
infra-leak shapes (authorization headers, bearer/JWT/API tokens,
kubeconfig, network identifiers). Free-text NER is out of scope here --
that is Tier-2 Microsoft Presidio in C1-c (#1072).

Why named (vs raw inline regex)? Three reasons:

1. **Policy YAML stays readable.** A policy author references
   ``bearer_token`` by name; the regex itself is owned, version-pinned,
   and unit-tested in this module.
2. **Audit manifests carry semantic types**, not opaque match indices.
   Downstream consumers (C1-b audit row, C1-d round-trip CI gate) bin
   by ``pattern.name`` -- which would be brittle if the name were the
   regex source.
3. **Calibration without a YAML rev.** A false-positive against e.g.
   ``fqdn`` is fixed by editing :data:`NAMED_PATTERNS` in this file
   plus a regression test; every policy that references the name picks
   up the fix on the next process boot.

**Pattern contract.** Every entry in :data:`NAMED_PATTERNS` is a
pre-compiled :class:`re.Pattern` with the ``re.IGNORECASE`` flag where
casing varies in the wild (HTTP header names; hex tokens). Patterns are
**anchored to a boundary** (word boundary or punctuation) wherever
possible so they do not over-match in the middle of unrelated text;
the engine then redacts the full match span (group 0). Patterns capture
the *value* shape, not the surrounding context, except for
``authorization_header`` where the redacted span is the whole
``Authorization: ...`` line because the header name itself is the
operator-facing signal.

**False-positive posture.** Tier-1 is the cheap layer. It will
*occasionally* over-redact a benign UUID-shaped or FQDN-shaped string,
and an operator who needs to surface those can scope the rule with
``connector_id`` / ``op`` (see :mod:`.policy`). The opposite mistake
(under-redaction of a real secret) is the one we cannot afford -- the
trust boundary is the API surface, per the parent goal (#800).
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "NAMED_PATTERNS",
    "PATTERN_NAMES",
    "get_pattern",
]


# Secret-*value* character class shared by the labelled-secret patterns
# below. The previous class (alphanumerics plus ``. _ - + / =`` only)
# stopped at the first punctuation byte, so a value like
# ``P@ssw0rd!withMore`` was captured only partially -- or not at all
# when the leading run of in-class bytes was shorter than the
# pattern's minimum -- leaving the
# secret (or its tail) in cleartext past the redaction span. A value is
# now consumed to its *natural delimiter*: whitespace or a quote
# (quotes bound the ``key: 'value'`` shape and terminate JSON string
# leaves). A single negated character class keeps matching linear (no
# nested quantifiers, so no catastrophic backtracking); the explicit
# upper bound keeps a pathological unbroken blob from being swallowed
# wholesale. Opaque-ID false positives (Git SHAs, build hashes) stay
# out because every pattern using this class still requires labelled
# context (``Authorization:`` / ``Bearer`` / ``password=``-style).
_VALUE_CHARS: Final[str] = r"[^\s'\"]"

#: Upper bound on one secret-value run. Generous enough that real
#: credentials (JWTs, PATs, DSNs) are consumed whole -- a cap a secret
#: routinely exceeded would reintroduce the tail-leak the widened class
#: exists to close -- while still bounding the redacted span on
#: degenerate multi-kilobyte unbroken blobs.
_VALUE_MAX: Final[int] = 4096


# -- Authorization-style headers --------------------------------------------
#
# ``Authorization: Bearer <jwt>`` and ``Authorization: Basic
# <base64>`` are the two RFC 7235 shapes operators paste into ticket
# bodies and log lines. We match the whole header line so the audit
# manifest carries the type label ``authorization_header`` rather than
# the inner-token type -- the operator-facing signal is the header
# itself. ``re.IGNORECASE`` covers ``authorization`` / ``Authorization``
# / ``AUTHORIZATION`` (HTTP header names are case-insensitive per
# RFC 7230). The credential part consumes :data:`_VALUE_CHARS` to its
# natural delimiter so schemes carrying punctuation-rich parameters
# (``Digest``, proxy DSNs) are redacted whole.
_AUTHORIZATION_HEADER = re.compile(
    r"Authorization\s*:\s*[A-Za-z]+\s+" + _VALUE_CHARS + "{1," + str(_VALUE_MAX) + "}",
    re.IGNORECASE,
)

# Bare bearer tokens: ``Bearer <opaque>``. Separate from
# authorization_header because operators paste ``Bearer eyJ...`` into
# Slack snippets without the surrounding header name (curl ``-H "Bearer
# ..."`` is wrong but common). The token body consumes
# :data:`_VALUE_CHARS` to whitespace / quote / end so opaque tokens
# carrying punctuation are captured whole; the leading ``\b`` anchor
# prevents matching ``foobarBearer ...`` mid-word.
_BEARER_TOKEN = re.compile(
    r"\bBearer\s+" + _VALUE_CHARS + "{8," + str(_VALUE_MAX) + "}",
    re.IGNORECASE,
)

# JWTs: three base64url segments separated by dots, header starts with
# ``ey`` (the base64 of ``{"``). The 16-char minimum on header/payload
# segments avoids matching arbitrary ``a.b.c`` strings while still
# catching the smallest real JWTs (which run ~80+ chars total).
_JWT = re.compile(
    r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
)

# Generic API keys / secret tokens. This is intentionally narrow: we
# only match labelled secrets followed by ``=`` / ``:`` and a
# non-trivial value. Covered labels:
#
#   ``api[_-]?key``, ``access[_-]?token``, ``refresh[_-]?token``,
#   ``auth[_-]?token``, ``session[_-]?token``, ``token`` (bare),
#   ``secret(?:[_-]?(?:key|id))?``, ``private[_-]?key``,
#   ``password``, ``passwd``, ``pwd``, ``client[_-]?secret``
#
# More-specific ``*_token`` / ``secret[_-]?id`` / ``private[_-]?key``
# members are listed before the broad bare ``token`` member so that the
# leftmost-first alternation rule does not shadow them (both orders
# match identically here since the value tail is group 0, but the
# ordering makes the intent readable).
#
# A naked 40-char base64 string in a paragraph is **not** matched
# because the false-positive rate against opaque IDs (Git SHAs,
# build hashes, CSP nonces) would be intolerable; downstream Tier-2 NER
# can take a second pass on free text.
#
# The value tail consumes :data:`_VALUE_CHARS` to its natural delimiter
# (whitespace / quote / end of blob), so a punctuated value like
# ``password: 'hunter2$plus#tail'`` is redacted whole instead of being
# truncated at the first out-of-class byte -- the label requirement is
# what keeps the false-positive posture, not the value alphabet.
_API_KEY = re.compile(
    r"\b(?:"
    r"api[_-]?key"
    r"|access[_-]?token"
    r"|refresh[_-]?token"
    r"|auth[_-]?token"
    r"|session[_-]?token"
    r"|secret(?:[_-]?(?:key|id))?"
    r"|private[_-]?key"
    r"|password|passwd|pwd"
    r"|client[_-]?secret"
    r"|token"
    r")"
    r"\s*[=:]\s*['\"]?" + _VALUE_CHARS + "{8," + str(_VALUE_MAX) + r"}['\"]?",
    re.IGNORECASE,
)

# Kubeconfig: the giveaway is ``apiVersion: v1`` + ``kind: Config``
# nearby in the same blob. We match the whole document by locking onto
# the YAML preamble and then sweeping until the next blank line OR end
# of string (whichever comes first). Operators paste these into ticket
# bodies wholesale; redacting the file shell + leaving the structure is
# pointless, so we redact the full match.
_KUBECONFIG = re.compile(
    r"apiVersion\s*:\s*v1\s*\n(?:.*\n){0,80}?kind\s*:\s*Config(?:\n(?:.*\n){0,200}?(?:\n|$))?",
    re.IGNORECASE,
)

# UUIDs (RFC 4122 canonical 8-4-4-4-12 hex). UUIDs in operator-facing
# payloads are often resource identifiers (tenant id, agent run id) --
# arguably non-sensitive -- but they are stable per-tenant correlators
# that an LLM context window does not need. Tier-1 redacts; per-op
# scopes (see :mod:`.policy`) opt back in where needed.
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

# IPv4 dotted-quad. The 0-255 per-octet constraint avoids matching
# arbitrary 4-tuples like build version numbers (``1.2.3.4`` parses as a
# valid IP but is also the canonical 4-segment marketing version, so a
# policy that needs to surface it scopes the rule out).
_IPV4 = re.compile(
    r"\b(?:25[0-5]|2[0-4][0-9]|[01]?[0-9]?[0-9])"
    r"(?:\.(?:25[0-5]|2[0-4][0-9]|[01]?[0-9]?[0-9])){3}\b",
)

# IPv6: full + compressed forms. Built from the union of the canonical
# RFC 5952 patterns; intentionally rejects the dual-IPv6/IPv4 embedded
# form (e.g. ``::ffff:192.0.2.1``) to keep the regex tractable -- the
# embedded-IPv4 half is caught by :data:`_IPV4` anyway.
_IPV6 = re.compile(
    r"(?<![:.A-Za-z0-9])"
    r"(?:"
    # 8-group full form (1762:0:0:0:0:B03:1:AF18)
    r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
    r"|"
    # Compressed forms with one ``::``
    r"(?:[0-9A-Fa-f]{1,4}:){1,7}:"
    r"|"
    r":(?::[0-9A-Fa-f]{1,4}){1,7}"
    r"|"
    r"(?:[0-9A-Fa-f]{1,4}:){1,6}(?::[0-9A-Fa-f]{1,4}){1,6}"
    r")"
    r"(?![:.A-Za-z0-9])",
)

# Fully-qualified domain names. We require at least two labels and a
# 2-24 char TLD with no leading digit (the leading-digit clause kills
# version triples like ``1.2.3``). Single-label hostnames (``localhost``,
# ``vcenter``) are out of scope -- they are not "fully qualified" by
# definition. The trailing-dot canonical FQDN form is allowed but
# optional. Labels follow RFC 1035 syntax (LDH; no leading/trailing
# hyphen; 1-63 chars), so we use the standard ``[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?``
# label production rather than free-form ``[A-Za-z0-9\-]+``.
_FQDN = re.compile(
    r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z][A-Za-z0-9\-]{1,23}\b",
)


#: Frozen mapping from pattern name to compiled regex. Insertion order is
#: the canonical evaluation order (engine iterates rules, not patterns,
#: so this is documentation rather than a contract -- the engine applies
#: rules in policy order). Names match the catalogue called out in the
#: task body (#1070).
NAMED_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "authorization_header": _AUTHORIZATION_HEADER,
    "bearer_token": _BEARER_TOKEN,
    "jwt": _JWT,
    "api_key": _API_KEY,
    "kubeconfig": _KUBECONFIG,
    "uuid": _UUID,
    "ipv4": _IPV4,
    "ipv6": _IPV6,
    "fqdn": _FQDN,
}

#: Stable, sorted tuple of pattern names; what the Pydantic policy
#: schema validates rule.pattern against. Sorted for deterministic
#: error messages.
PATTERN_NAMES: Final[tuple[str, ...]] = tuple(sorted(NAMED_PATTERNS))


def get_pattern(name: str) -> re.Pattern[str]:
    """Return the compiled regex for *name*.

    Raises :class:`KeyError` with the known-names list when *name* is
    unknown -- the policy schema rejects unknown names at parse time
    via a field validator, so reaching this branch indicates a
    programmer error (engine wired up against a stale name).
    """
    try:
        return NAMED_PATTERNS[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown named pattern {name!r}; known patterns: {', '.join(PATTERN_NAMES)}",
        ) from exc
