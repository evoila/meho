# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check a group's ``when_to_use`` text against its ops (#3671).

An LLM writes each group's ``when_to_use`` text at ingest. The operator
reads it before moving the group from ``staged`` to ``enabled``. Nothing
checked that text against what the ops in the group really do. A group
whose text says "read-only inventory" could hold ops that delete things.

:func:`build_group_advisory` gives the review payload two things per
group:

* a count of the enabled ops per ``safety_level``, plus how many of them
  need approval (:class:`GroupSafetyDistribution`);
* short, plain flags where the text does not match the ops: the text
  makes a safety claim the ops break (for example "read-only",
  "non-destructive", "all ... destructive", "all ... require approval"),
  or the text shares no word with the op ids and summaries.

A "read-only" claim is broken by any op that is not ``safe`` or needs
approval. A "non-destructive" claim is broken only by a ``destructive``
op. An "all ... <level>" claim is broken by any op at another level,
higher or lower, so text that understates the risk is flagged too.

Ops in a ``staged`` group are all disabled until the operator enables
the group. So when no op in the group is enabled, the flags check the
ops that enabling the group would turn on (every op in the group) and
say so. The counts always cover enabled ops only.

This is advice only. It never gates dispatch, ingest, or enablement.
The check is a simple word match, not a language parser.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from meho_backplane.operations.ingest.payload import (
    ConnectorReviewGroupAdvisory,
    ConnectorReviewOp,
    GroupSafetyDistribution,
)

__all__ = ["build_group_advisory"]

_SAFETY_LEVELS = ("safe", "caution", "dangerous", "destructive")

_MAX_FLAGS = 5

_READ_ONLY_RE = re.compile(r"\b(read[- ]?only|no side[- ]effects?|does not (?:change|modify))\b")
_NON_DESTRUCTIVE_RE = re.compile(r"\bnon[- ]?destructive\b")
# "all ... dangerous" / "every ... destructive" inside one sentence. The
# lookbehinds keep "non-destructive" and "non destructive" out of it.
_ALL_LEVEL_RE = re.compile(r"\b(?:all|every)\b[^.;]*?(?<!non-)(?<!non )\b(dangerous|destructive)\b")
_ALL_APPROVAL_RE = re.compile(r"\b(?:all|every)\b[^.;]*?\b(?:require|need)s? approval\b")

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD_RE = re.compile(r"[a-z0-9]+")

# Words that say nothing about what a group is for.
_STOPWORDS = frozenset(
    {
        "about",
        "all",
        "also",
        "and",
        "any",
        "api",
        "are",
        "can",
        "does",
        "each",
        "endpoint",
        "for",
        "from",
        "group",
        "has",
        "have",
        "here",
        "into",
        "its",
        "need",
        "not",
        "one",
        "operation",
        "such",
        "that",
        "the",
        "their",
        "them",
        "these",
        "this",
        "those",
        "use",
        "used",
        "using",
        "when",
        "which",
        "with",
        "you",
        "your",
    }
)


def _words(text: str) -> set[str]:
    """Lowercase content words of *text*, with a plain trailing ``s`` removed."""
    words: set[str] = set()
    for raw in _WORD_RE.findall(_CAMEL_RE.sub(" ", text).lower()):
        word = raw[:-1] if len(raw) > 3 and raw.endswith("s") and not raw.endswith("ss") else raw
        if len(word) >= 3 and word not in _STOPWORDS:
            words.add(word)
    return words


def _distribution(enabled: Sequence[ConnectorReviewOp]) -> GroupSafetyDistribution:
    """Count *enabled* ops per ``safety_level`` and per approval flag."""
    counts = dict.fromkeys(_SAFETY_LEVELS, 0)
    for op in enabled:
        if op.safety_level in counts:
            counts[op.safety_level] += 1
    return GroupSafetyDistribution(
        enabled_op_count=len(enabled),
        requires_approval_count=sum(1 for op in enabled if op.requires_approval),
        **counts,
    )


def _level_counts(ops: Sequence[ConnectorReviewOp], levels: set[str]) -> str:
    """Render ``"caution: 1, dangerous: 2"`` for the ops whose level is in *levels*."""
    parts = []
    for level in _SAFETY_LEVELS:
        count = sum(1 for op in ops if op.safety_level == level)
        if level in levels and count:
            parts.append(f"{level}: {count}")
    return ", ".join(parts)


def _read_only_detail(not_safe: Sequence[ConnectorReviewOp]) -> str:
    """Render ``" (dangerous: 1, need approval: 2)"`` for the ops that break a read-only claim.

    Empty when there is nothing to list, so the flag never ends in ``()``.
    """
    parts = [_level_counts(not_safe, {"caution", "dangerous", "destructive"})]
    approval = sum(1 for op in not_safe if op.requires_approval)
    if approval:
        parts.append(f"need approval: {approval}")
    detail = ", ".join(part for part in parts if part)
    return f" ({detail})" if detail else ""


def _posture_flags(prose: str, ops: Sequence[ConnectorReviewOp]) -> list[str]:
    """Flags where a safety claim in *prose* is broken by *ops*."""
    flags: list[str] = []
    if _READ_ONLY_RE.search(prose):
        not_safe = [op for op in ops if op.safety_level != "safe" or op.requires_approval]
        if not_safe:
            flags.append(
                f"The text says read-only, but {len(not_safe)} op(s) are not safe or need "
                f"approval{_read_only_detail(not_safe)}."
            )
    if _NON_DESTRUCTIVE_RE.search(prose):
        destructive = sum(1 for op in ops if op.safety_level == "destructive")
        if destructive:
            flags.append(f"The text says non-destructive, but {destructive} op(s) are destructive.")
    for match in _ALL_LEVEL_RE.finditer(prose):
        level = match.group(1)
        others = [op for op in ops if op.safety_level != level]
        if others:
            flags.append(
                f"The text says all ops are {level}, but {len(others)} op(s) are at another "
                f"level ({_level_counts(others, set(_SAFETY_LEVELS) - {level})})."
            )
    if _ALL_APPROVAL_RE.search(prose):
        no_approval = sum(1 for op in ops if not op.requires_approval)
        if no_approval:
            flags.append(f"The text says all ops need approval, but {no_approval} op(s) do not.")
    return flags


def _overlap_flag(prose: str, ops: Sequence[ConnectorReviewOp]) -> str | None:
    """A flag when *prose* shares no content word with the ops' ids and summaries."""
    op_words: set[str] = set()
    for op in ops:
        op_words |= _words(op.op_id)
        op_words |= _words(op.summary or "")
    if not op_words or _words(prose) & op_words:
        return None
    return (
        "The text shares no word with the op ids or summaries. Check that it describes these ops."
    )


def build_group_advisory(
    when_to_use: str, ops: Sequence[ConnectorReviewOp]
) -> ConnectorReviewGroupAdvisory:
    """Build the review advice for one group from its already-loaded *ops*."""
    enabled = [op for op in ops if op.is_enabled]
    checked = enabled or list(ops)
    if not checked:
        return ConnectorReviewGroupAdvisory(distribution=_distribution(enabled))
    prose = when_to_use.lower()
    flags = _posture_flags(prose, checked)
    overlap = _overlap_flag(when_to_use, checked)
    if overlap is not None:
        flags.append(overlap)
    if not enabled:
        note = f" (No op is enabled yet; checked all {len(checked)} op(s) in the group.)"
        flags = [flag + note for flag in flags]
    return ConnectorReviewGroupAdvisory(
        distribution=_distribution(enabled),
        flags=list(dict.fromkeys(flags))[:_MAX_FLAGS],
    )
