# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Let the ``api_key`` rule hide a command line value it hides whole.

In the default policy the ``command_line_secret`` rule runs right before
the ``api_key`` rule. Where the ``api_key`` rule hides a value whole, the
answer stays as it was without the command line rule. So the two rules
work as a pair, built for one run of the engine by :func:`hand_off`:

* The command line side reads every value (:mod:`.command_secret`). It
  leaves a value to the ``api_key`` rule when that rule hides it whole on
  the text the rule really reads: the text after this rule's marks. A
  value that the rule hides only in part is hidden whole, together with
  that part. This is decided again until no value changes side.
* The ``api_key`` side reads the text between the marks that the command
  line side wrote in the same run. So a value never starts in such a mark
  and never runs over one. Every other text, also a mark that was in the
  text before the run, is read as the ``api_key`` rule always reads it.

The engine runs a rule over the whole payload before the next rule, in
the same order. So the command line side keeps what it hid in each text,
in that order, and the ``api_key`` side takes the entries in turn. It
uses an entry only when the text is the one the engine wrote from it.
"""

from __future__ import annotations

import bisect
import re
from collections import deque
from typing import Final

from meho_backplane.redaction.command_secret import COMMAND_SECRET, merge_spans

__all__ = ["hand_off"]

_MARK: Final = "[REDACTED:command_line_secret]"
#: How often the sides are decided again. A value still left after that is
#: hidden by the command line rule, so a text cannot keep the scan busy.
_MAX_ROUNDS: Final = 8

_Spans = list[tuple[int, int]]


def _overlapping(covers: _Spans, ends: list[int], start: int, end: int) -> _Spans:
    """The *covers* (sorted, with their *ends*) that overlap ``start..end``."""
    first = bisect.bisect_right(ends, start)
    last = first
    while last < len(covers) and covers[last][0] < end:
        last += 1
    return covers[first:last]


def _joined(covers: _Spans, ends: list[int], start: int, end: int) -> tuple[int, int]:
    """``start..end`` together with every match of the rule that overlaps it."""
    over = _overlapping(covers, ends, start, end)
    if not over:
        return start, end
    return min(start, over[0][0]), max(end, over[-1][1])


def _covered(found: _Spans, starts: list[int], start: int, end: int) -> bool:
    """True when one span of *found* (sorted, with their *starts*) holds ``start..end`` whole."""
    at = bisect.bisect_right(starts, start) - 1
    return at >= 0 and end <= found[at][1]


def _between_marks(text: str, marks: _Spans, rule: re.Pattern[str]) -> _Spans:
    """The matches of *rule* in *text*, each inside the text between two *marks*."""
    found: _Spans = []
    pos = 0
    for start, end in [*marks, (len(text), len(text))]:
        found.extend(match.span() for match in rule.finditer(text, pos, start))
        pos = end
    return found


def _read_after_marks(text: str, hidden: _Spans, rule: re.Pattern[str]) -> _Spans:
    """What *rule* hides once *hidden* is replaced by the mark, in places of *text*."""
    parts: list[str] = []
    marks: _Spans = []
    shift: list[tuple[int, int]] = [(0, 0)]
    pos = size = 0
    for start, end in hidden:
        parts.append(text[pos:start])
        size += start - pos
        marks.append((size, size + len(_MARK)))
        parts.append(_MARK)
        size += len(_MARK)
        shift.append((size, end))
        pos = end
    parts.append(text[pos:])
    found = _between_marks("".join(parts), marks, rule)
    starts = [marked for marked, _place in shift]
    out: _Spans = []
    for start, end in found:
        marked, place = shift[bisect.bisect_right(starts, start) - 1]
        out.append((start - marked + place, end - marked + place))
    return out


def _hidden_by_this_rule(text: str, spans: _Spans, rule: re.Pattern[str]) -> _Spans:
    """The values the command line rule hides when *rule* runs right after it."""
    covers = [match.span() for match in rule.finditer(text)]
    ends = [end for _start, end in covers]
    hidden: _Spans = []
    left: _Spans = []
    for start, end in spans:
        over = _overlapping(covers, ends, start, end)
        if len(over) == 1 and over[0][0] <= start and end <= over[0][1]:
            left.append((start, end))
        else:
            hidden.append(_joined(covers, ends, start, end))
    for _round in range(_MAX_ROUNDS):
        hidden = merge_spans(hidden)
        if not left:
            return hidden
        hidden_ends = [end for _start, end in hidden]
        left = [span for span in left if not _overlapping(hidden, hidden_ends, *span)]
        found = _read_after_marks(text, hidden, rule)
        starts = [start for start, _end in found]
        lost = [span for span in left if not _covered(found, starts, *span)]
        if not lost:
            return hidden
        hidden += [_joined(covers, ends, *span) for span in lost]
        left = [span for span in left if span not in lost]
    return merge_spans(hidden + [_joined(covers, ends, *span) for span in left])


def _written_marks(original: str, start: int, end: int) -> list[str]:
    """The ways the engine writes the mark over ``start..end`` of *original*.

    One mark for the value, or, in a list of lines, one mark for each line
    part of it, with the line breaks kept.
    """
    lines = original[start:end].split("\n")
    if len(lines) == 1:
        return [_MARK]
    return ["\n".join(_MARK if part else "" for part in lines), _MARK]


def _marks_in(text: str, original: str, hidden: _Spans) -> _Spans | None:
    """Where the marks written over *hidden* of *original* stand in *text*.

    ``None`` when *text* is not what the engine wrote from *original*.
    """
    marks: _Spans = []
    pos = at = 0
    for start, end in hidden:
        gap = original[pos:start]
        if not text.startswith(gap, at):
            return None
        at += len(gap)
        forms = _written_marks(original, start, end)
        written = next((form for form in forms if text.startswith(form, at)), None)
        if written is None:
            return None
        for part in written.split("\n"):
            if part:
                marks.append((at, at + len(part)))
            at += len(part) + 1
        at -= 1
        pos = end
    return marks if text[at:] == original[pos:] else None


class _CommandSide:
    def __init__(self, rule: re.Pattern[str], written: deque[tuple[str, _Spans]]) -> None:
        self._rule = rule
        self._written = written

    def secret_spans(self, text: str) -> _Spans:
        spans = COMMAND_SECRET.secret_spans(text)
        hidden = _hidden_by_this_rule(text, spans, self._rule) if spans else []
        self._written.append((text, hidden))
        return hidden


class _NextSide:
    def __init__(self, rule: re.Pattern[str], written: deque[tuple[str, _Spans]]) -> None:
        self._rule = rule
        self._written = written

    def secret_spans(self, text: str) -> _Spans:
        marks: _Spans | None = None
        if self._written:
            original, hidden = self._written.popleft()
            if hidden:
                marks = _marks_in(text, original, hidden)
        if not marks:
            return [match.span() for match in self._rule.finditer(text)]
        return _between_marks(text, marks, self._rule)


def hand_off(rule: re.Pattern[str]) -> tuple[_CommandSide, _NextSide]:
    """The command line finder and the finder of *rule* (``api_key``) for one engine run."""
    written: deque[tuple[str, _Spans]] = deque()
    return _CommandSide(rule, written), _NextSide(rule, written)
