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
  makes a safety claim the ops break (for example "read-only", "all ...
  destructive", "all ... require approval"), or the text shares no word
  with the op ids and summaries.

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

_SAFETY_RANK: dict[str, int] = {"safe": 0, "caution": 1, "dangerous": 2, "destructive": 3}

_MAX_FLAGS = 5

_READ_ONLY_RE = re.compile(
    r"\b(read[- ]?only|no side[- ]effects?|non[- ]destructive|does not (?:change|modify))\b"
)
# "all ... dangerous" / "every ... destructive" inside one sentence.
_ALL_LEVEL_RE = re.compile(r"\b(?:all|every)\b[^.;]*?\b(dangerous|destructive)\b")
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
    counts = dict.fromkeys(_SAFETY_RANK, 0)
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
    for level in _SAFETY_RANK:
        count = sum(1 for op in ops if op.safety_level == level)
        if level in levels and count:
            parts.append(f"{level}: {count}")
    return ", ".join(parts)


def _posture_flags(prose: str, ops: Sequence[ConnectorReviewOp]) -> list[str]:
    """Flags where a safety claim in *prose* is broken by *ops*."""
    flags: list[str] = []
    if _READ_ONLY_RE.search(prose):
        not_safe = [op for op in ops if op.safety_level != "safe" or op.requires_approval]
        if not_safe:
            flags.append(
                f"The text says read-only, but {len(not_safe)} op(s) are not safe or need "
                f"approval ({_level_counts(not_safe, {'caution', 'dangerous', 'destructive'})})."
            )
    for match in _ALL_LEVEL_RE.finditer(prose):
        level = match.group(1)
        below = [op for op in ops if _SAFETY_RANK.get(op.safety_level, 0) < _SAFETY_RANK[level]]
        if below:
            flags.append(
                f"The text says all ops are {level}, but {len(below)} op(s) are lower "
                f"({_level_counts(below, {'safe', 'caution', 'dangerous'})})."
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
