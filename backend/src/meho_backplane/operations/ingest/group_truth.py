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
  "non-destructive", "all ... destructive", "all ... require approval",
  "write operations are approval-required", "writes are dangerous"),
  or the text shares no word with the op ids and summaries.

A "read-only" claim is broken by any op that is not ``safe`` or needs
approval. A "non-destructive" claim is broken only by a ``destructive``
op.

An approval claim ("approval-required", "require(s) approval",
"need(s) approval") and a level claim ("dangerous", "destructive") are
about either every op in the group or only its changes. The subject of
the sentence decides, not the wording of the claim:

* A sentence with "all" or "every" that does not name a change ("All
  operations are approval-required.", "Every call is dangerous.") is
  about every op. The approval claim is broken by any op that does not
  need approval. The level claim is broken by any op at another level,
  higher or lower, so text that understates the risk is flagged too.
* Any other sentence ("Writes are dangerous.", "Every change requires
  approval.", "Delete operations need approval.") is about the changes
  in the group: the ops that are not ``safe`` (or every op, when all of
  them are ``safe``). The approval claim is broken by any change op
  that does not need approval. The level claim is broken by any change
  op below the named level.

So "Every change requires approval." and "All operations are
approval-required." are checked against different ops, and the same
wording is read the same way in both the review advice and the ingest
check.

:func:`drop_unsupported_claims` uses the same checks at ingest (#4971).
It removes each sentence of a new group's text that makes a claim the
group's ops break, so a stored text never promises an approval step
that does not exist.

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
from typing import Protocol

from meho_backplane.operations.ingest.payload import (
    ConnectorReviewGroupAdvisory,
    ConnectorReviewOp,
    GroupSafetyDistribution,
)

__all__ = ["build_group_advisory", "drop_unsupported_claims"]


class _OpRisk(Protocol):
    """The two risk fields the safety-claim checks read from an op."""

    @property
    def safety_level(self) -> str: ...

    @property
    def requires_approval(self) -> bool: ...


_SAFETY_LEVELS = ("safe", "caution", "dangerous", "destructive")

_MAX_FLAGS = 5

_READ_ONLY_RE = re.compile(r"\b(read[- ]?only|no side[- ]effects?|does not (?:change|modify))\b")
_NON_DESTRUCTIVE_RE = re.compile(r"\bnon[- ]?destructive\b")
# The lookbehinds keep "non-destructive" and "non destructive" out of it.
_LEVEL_RE = re.compile(r"(?<!non-)(?<!non )\b(dangerous|destructive)\b")
# The lookbehinds keep "no approval required" and "do not need approval"
# out of it: those say the opposite.
_APPROVAL_RE = re.compile(
    r"(?<!no )\bapproval[- ]required\b|(?<!not )(?<!n't )\b(?:require|need)s? (?:an? )?approval\b"
)
# A sentence with "all" / "every" that names no change is about every op.
_ALL_RE = re.compile(r"\b(?:all|every)\b")
_CHANGE_WORD_RE = re.compile(
    r"\b(?:writes?|changes?|modif\w*|mutat\w*|updates?|edits?|creates?|creation|"
    r"deletes?|deletion|removes?|removal)\b"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

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


def _level_counts(ops: Sequence[_OpRisk], levels: set[str]) -> str:
    """Render ``"caution: 1, dangerous: 2"`` for the ops whose level is in *levels*."""
    parts = []
    for level in _SAFETY_LEVELS:
        count = sum(1 for op in ops if op.safety_level == level)
        if level in levels and count:
            parts.append(f"{level}: {count}")
    return ", ".join(parts)


def _read_only_detail(not_safe: Sequence[_OpRisk]) -> str:
    """Render ``" (dangerous: 1, need approval: 2)"`` for the ops that break a read-only claim.

    Empty when there is nothing to list, so the flag never ends in ``()``.
    """
    parts = [_level_counts(not_safe, {"caution", "dangerous", "destructive"})]
    approval = sum(1 for op in not_safe if op.requires_approval)
    if approval:
        parts.append(f"need approval: {approval}")
    detail = ", ".join(part for part in parts if part)
    return f" ({detail})" if detail else ""


def _changes(ops: Sequence[_OpRisk]) -> list[_OpRisk]:
    """The ops a claim about changes talks about: the non-``safe`` ones, else every op."""
    return [op for op in ops if op.safety_level != "safe"] or list(ops)


def _claim_scope(sentence: str, ops: Sequence[_OpRisk]) -> tuple[bool, list[_OpRisk]]:
    """Which ops a claim in *sentence* is about.

    Returns ``(True, every op)`` for a sentence with "all" or "every"
    that names no change, and ``(False, the change ops)`` otherwise.
    """
    if _ALL_RE.search(sentence) and not _CHANGE_WORD_RE.search(sentence):
        return True, list(ops)
    return False, _changes(ops)


def _approval_flag(sentence: str, ops: Sequence[_OpRisk]) -> str | None:
    """A flag when *sentence* says its ops need approval and one of them does not."""
    if not _APPROVAL_RE.search(sentence):
        return None
    about_all, claimed = _claim_scope(sentence, ops)
    no_approval = sum(1 for op in claimed if not op.requires_approval)
    if not no_approval:
        return None
    if about_all:
        return f"The text says all ops need approval, but {no_approval} op(s) do not."
    return (
        f"The text says changes need approval, but {no_approval} of {len(claimed)} "
        "change op(s) do not."
    )


def _level_flag(sentence: str, ops: Sequence[_OpRisk]) -> str | None:
    """A flag when *sentence* names a level and one of its ops is not at that level."""
    match = _LEVEL_RE.search(sentence)
    if match is None:
        return None
    level = match.group(1)
    about_all, claimed = _claim_scope(sentence, ops)
    if about_all:
        others = [op for op in claimed if op.safety_level != level]
        if not others:
            return None
        return (
            f"The text says all ops are {level}, but {len(others)} op(s) are at another "
            f"level ({_level_counts(others, set(_SAFETY_LEVELS) - {level})})."
        )
    below = set(_SAFETY_LEVELS[: _SAFETY_LEVELS.index(level)])
    lower = [op for op in claimed if op.safety_level in below]
    if not lower:
        return None
    return (
        f"The text says changes are {level}, but {len(lower)} op(s) are lower "
        f"({_level_counts(lower, below)})."
    )


def _sentence_flags(sentence: str, ops: Sequence[_OpRisk]) -> list[str]:
    """Flags where a safety claim in one *sentence* is broken by *ops*."""
    flags: list[str] = []
    if _READ_ONLY_RE.search(sentence):
        not_safe = [op for op in ops if op.safety_level != "safe" or op.requires_approval]
        if not_safe:
            flags.append(
                f"The text says read-only, but {len(not_safe)} op(s) are not safe or need "
                f"approval{_read_only_detail(not_safe)}."
            )
    if _NON_DESTRUCTIVE_RE.search(sentence):
        destructive = sum(1 for op in ops if op.safety_level == "destructive")
        if destructive:
            flags.append(f"The text says non-destructive, but {destructive} op(s) are destructive.")
    for flag in (_level_flag(sentence, ops), _approval_flag(sentence, ops)):
        if flag is not None:
            flags.append(flag)
    return flags


def _posture_flags(prose: str, ops: Sequence[_OpRisk]) -> list[str]:
    """Flags where a safety claim in *prose* is broken by *ops*, one sentence at a time."""
    flags: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(prose):
        flags.extend(_sentence_flags(sentence, ops))
    return flags


def drop_unsupported_claims(when_to_use: str, ops: Sequence[_OpRisk]) -> str:
    """Return *when_to_use* without the sentences whose safety claim *ops* break.

    Each sentence is checked on its own with the same word match the
    review advice uses. A sentence without a broken claim stays as it
    is. With no *ops* there is nothing to check, so the text stays. The
    result is empty when every sentence makes a broken claim.
    """
    if not ops:
        return when_to_use
    kept = [
        sentence
        for sentence in _SENTENCE_SPLIT_RE.split(when_to_use.strip())
        if not _posture_flags(sentence.lower(), ops)
    ]
    return " ".join(kept)


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
