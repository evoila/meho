# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tier-1 redaction engine -- Initiative #805, Task #1070.

The deterministic, sub-ms half of the C1 sanitization middleware:
walks a nested dict/list/str payload and applies a
:class:`~meho_backplane.redaction.policy.RedactionPolicy`'s rules,
returning ``(redacted_value, manifest)``. The middleware (#1071,
C1-b) calls :func:`redact` once per connector response between
"capture raw" and "JSONFlux reduce"; the audit row carries the
manifest verbatim.

**Pure and side-effect-free.** No I/O, no logging, no clocks --
deterministic for identical inputs. The engine is therefore safely
callable from CI fixtures (C1-d round-trip gate), unit tests, and the
production hot path without any setup. Callers that need timing or
logging wrap the call, not patch the engine.

**Walking strategy.** Strings are matched directly. Mappings are walked
key-by-key; the key itself is not redacted (operator-facing keys are
schema, not secrets), but its value is. Sequences are walked
positionally. Anything that is not a str / Mapping / Sequence (numbers,
booleans, None, bytes) is passed through verbatim -- regex redaction
on numerics is nonsensical and bytes are not expected at the
JSONFlux boundary.

**Manifest contract.** :class:`RedactionManifestEntry` per match:

* ``rule`` -- the firing rule's name (the operator-facing handle).
* ``pattern`` -- the named pattern that matched (``bearer_token``,
  ``uuid``, ...). Audit consumers can bin by pattern across rules.
* ``action`` -- which action ran (``redact`` / ``mask`` / ``hash``).
* ``count`` -- number of matches in this leaf. Multiple matches in one
  leaf collapse into one manifest entry (count tracks them); the audit
  row stays compact even on a paste-heavy payload.
* ``span`` -- ``(start, end)`` of the *first* match in the original
  string, byte-indexed against the pre-redaction value. For
  diagnostic display only; the count is the load-bearing quantity.
* ``reason`` -- the rule's ``reason`` string, propagated verbatim.
* ``path`` -- dotted path to the leaf within the payload tree
  (``"items.3.password"``). Consumers correlate this with the raw row.

Multiple rules firing on the same leaf emit one manifest entry per
firing rule, in policy order. A subsequent rule sees the
already-redacted leaf produced by the previous rule, so wide rules
(``fqdn``) should come **after** narrow ones (``api_key``) -- the
policy is responsible for ordering.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.redaction.patterns import get_pattern
from meho_backplane.redaction.policy import RedactionPolicy, RedactionRule

__all__ = [
    "RedactionManifestEntry",
    "RedactionResult",
    "redact",
]


#: Length of the hex suffix the ``hash`` action exposes. 12 hex
#: characters = 48 bits -- enough that two distinct secrets cannot
#: practically collide in one audit row, short enough that the
#: hashed-view string stays compact.
_HASH_SUFFIX_LEN: Final[int] = 12

#: Length of the value suffix the ``mask`` action preserves to aid
#: correlation. 4 chars matches the credit-card convention; for hex
#: tokens it carries enough to disambiguate without leaking more than
#: log2(16^4) = 16 bits.
_MASK_SUFFIX_LEN: Final[int] = 4


class RedactionManifestEntry(BaseModel):
    """One firing of one rule on one leaf. See module docstring."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: str
    pattern: str
    action: str
    count: int = Field(ge=1)
    span: tuple[int, int]
    reason: str
    path: str


class RedactionResult(BaseModel):
    """Engine return value: ``(redacted, manifest)``.

    The redacted payload is structurally identical to the input (same
    nesting; only string leaves change) so callers can hand it to the
    JSONFlux reducer without further shape-fixing. The manifest is
    ordered by traversal: stable for a given input, which lets the
    C1-d round-trip CI gate diff manifest-to-manifest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    redacted: object
    manifest: tuple[RedactionManifestEntry, ...]


def redact(
    payload: object,
    policy: RedactionPolicy,
    *,
    connector_id: str | None = None,
    tenant: str | None = None,
    op: str | None = None,
) -> RedactionResult:
    """Apply *policy* to *payload*; return ``(redacted, manifest)``.

    *connector_id*, *tenant*, *op* are the call-site labels the
    middleware passes through; rules with a non-empty
    :class:`~meho_backplane.redaction.policy.RedactionScope` use them
    to short-circuit non-matching calls. All three default to ``None``
    for test convenience -- a scope-less rule fires regardless.

    Rule ordering: rules apply in policy order. Each rule walks the
    full payload before the next rule runs; later rules see the
    already-redacted output of earlier ones (see module docstring).
    """
    # Pre-filter rules by scope so the inner walk does not re-evaluate
    # the predicate per leaf. The remaining rules ``apply`` unconditionally
    # at every string leaf.
    applicable_rules = tuple(
        rule
        for rule in policy.rules
        if rule.scope.matches(connector_id=connector_id, tenant=tenant, op=op)
    )

    manifest: list[RedactionManifestEntry] = []
    current = payload
    for rule in applicable_rules:
        current = _walk_and_apply(current, rule, manifest, path="")

    return RedactionResult(redacted=current, manifest=tuple(manifest))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _walk_and_apply(
    node: object,
    rule: RedactionRule,
    manifest: list[RedactionManifestEntry],
    *,
    path: str,
) -> object:
    """Recurse through *node*, applying *rule* at every string leaf.

    The walk is shape-preserving: mappings stay mappings, sequences
    stay (the same) sequence type by way of ``type(node)(...)`` where
    safe, falling back to ``list`` for unrecognized sequence subtypes.
    The Pydantic schema validates payloads only at the connector
    boundary, not here -- the engine is duck-typed against ``Mapping``
    and ``Sequence`` (excluding ``str`` / ``bytes``).
    """
    if isinstance(node, str):
        return _apply_to_str(node, rule, manifest, path=path)
    if isinstance(node, Mapping):
        return {
            key: _walk_and_apply(
                value,
                rule,
                manifest,
                path=_join_path(path, str(key)),
            )
            for key, value in node.items()
        }
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        return [
            _walk_and_apply(
                item,
                rule,
                manifest,
                path=_join_path(path, str(index)),
            )
            for index, item in enumerate(node)
        ]
    # Numbers, booleans, None, bytes, custom scalars: pass through.
    # Regex redaction on these is nonsensical; the C1-b middleware is
    # the boundary that asserts what shapes the engine ever sees.
    return node


def _apply_to_str(
    leaf: str,
    rule: RedactionRule,
    manifest: list[RedactionManifestEntry],
    *,
    path: str,
) -> str:
    """Match *rule* against *leaf*; emit manifest + return new string.

    The named-pattern lookup raises :class:`KeyError` on an unknown
    name, but the policy schema rejects unknown names at parse time
    so that branch is unreachable in production. Defensively, an
    unknown name here would crash policy load before any traffic, not
    a request mid-flight.
    """
    pattern = get_pattern(rule.pattern)
    matches = list(pattern.finditer(leaf))
    if not matches:
        return leaf

    redacted = pattern.sub(
        lambda m: _replacement_for(rule, m),
        leaf,
    )
    first = matches[0]
    manifest.append(
        RedactionManifestEntry(
            rule=rule.name,
            pattern=rule.pattern,
            action=rule.action,
            count=len(matches),
            span=(first.start(), first.end()),
            reason=rule.reason,
            path=path,
        ),
    )
    return redacted


def _replacement_for(rule: RedactionRule, match: re.Match[str]) -> str:
    """Return the per-match replacement string for *rule*'s action.

    Centralised so the three actions stay alignable; adding a new
    action (e.g. ``tokenize`` for the secret-broker integration #581)
    is one literal here plus one entry in the
    :data:`~meho_backplane.redaction.policy.RedactionAction` union.
    """
    matched = match.group(0)
    if rule.action == "redact":
        return f"[REDACTED:{rule.pattern}]"
    if rule.action == "mask":
        # Length-preserving asterisk run + last-N suffix. When the
        # match is shorter than the suffix we want to preserve, fall
        # back to a pure asterisk run -- exposing the whole value as
        # "suffix" defeats the masking.
        if len(matched) <= _MASK_SUFFIX_LEN:
            return "*" * len(matched)
        return "*" * (len(matched) - _MASK_SUFFIX_LEN) + matched[-_MASK_SUFFIX_LEN:]
    if rule.action == "hash":
        # SHA-256 is overkill cryptographically for what is really a
        # stable correlator, but it costs ~microseconds on the leaf
        # sizes the connector boundary sees and keeps the audit
        # column type-stable across rule revisions. UTF-8 encoding is
        # explicit so the hashed value is stable across platforms.
        digest = hashlib.sha256(matched.encode("utf-8")).hexdigest()
        return f"sha256:{digest[:_HASH_SUFFIX_LEN]}"
    # The Literal union on RedactionAction excludes any other value
    # at the schema layer; the catch-all is defensive against future
    # action additions that forget to extend this match.
    raise RuntimeError(f"unsupported redaction action {rule.action!r}")


def _join_path(parent: str, child: str) -> str:
    """Build dotted JSON-ish paths for manifest entries.

    Root leaves get the empty path ``""`` so a top-level string
    payload (uncommon but legal) emits ``path=""`` rather than a
    leading dot.
    """
    if not parent:
        return child
    return f"{parent}.{child}"
