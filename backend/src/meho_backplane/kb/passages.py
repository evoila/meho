# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Split a kb body into heading-aware passages that fit the model window (#3761).

A kb entry is indexed as passages so a search can point at the part of
a long body that matched, not at the whole body. This module is pure:
it gets the body and a token counter and returns passages. It does no
I/O, so it is easy to test with a stub counter.

How a body is split
-------------------

1. **Sections.** Every Markdown ATX heading (``#`` to ``######``,
   outside fenced code blocks) starts a section. The text before the
   first heading is a section with an empty heading path. A section's
   heading path is the titles of its parent headings plus its own.
2. **Header.** Each passage is embedded as ``header + text``. The
   header is the heading path joined with ``" > "`` and a blank line.
   A header that would take more than a quarter of the window is
   left out, so long titles cannot crowd out the text.
3. **Fit.** A section whose ``header + text`` fits the window is one
   passage. Otherwise it is cut at the first level that works:
   paragraphs (blank lines), then lines, then sentences, then words,
   and at last single characters. Neighbouring pieces are packed
   together as long as the packed text still fits.

"Fits" means the real token count of ``header + text``, special tokens
included, is at most the window (see
:class:`~meho_backplane.retrieval.embedding.PassageTokenizer`). The
count is never estimated.

Every passage text is ``body[start:end]`` exactly, with leading and
trailing white space left out. Offsets count Unicode code points (the
Python ``str`` index). The same body, counter and window always give
the same passages in the same order.
"""

from __future__ import annotations

import hashlib
import itertools
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from meho_backplane.kb.schemas import (
    KB_KIND_PASSAGE,
    KB_PASSAGE_SOURCE,
    META_KB_PASSAGE,
)
from meho_backplane.retrieval.indexer import IndexItem

__all__ = [
    "CHUNKER_VERSION",
    "KbPassage",
    "chunk_markdown",
    "index_generation",
    "passage_items",
    "revision_uri",
]

#: Name of the splitting rule. Change it whenever the rule changes:
#: the next write of every entry then builds new passages.
CHUNKER_VERSION: Final[str] = "kb-md-heading-v1"

#: Separator between heading titles in a passage header.
_HEADER_JOIN: Final[str] = " > "

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
_LINE_BREAK = re.compile(r"\n")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_WORD_BREAK = re.compile(r"\s+")

#: Cut levels, coarse to fine. Single characters come after the last.
_BREAKS: Final[tuple[re.Pattern[str], ...]] = (
    _PARAGRAPH_BREAK,
    _LINE_BREAK,
    _SENTENCE_BREAK,
    _WORD_BREAK,
)

Counter = Callable[[str], int]


@dataclass(frozen=True)
class KbPassage:
    """One passage: ``text == body[start:end]``, embedded as ``embed_text``."""

    ordinal: int
    heading_path: tuple[str, ...]
    start: int
    end: int
    text: str
    embed_text: str
    tokens: int


@dataclass(frozen=True)
class _Section:
    heading_path: tuple[str, ...]
    start: int
    end: int


def chunk_markdown(body: str, count: Counter, window: int) -> list[KbPassage]:
    """Return the passages of *body*; each ``header + text`` fits *window* tokens."""
    spans: list[tuple[tuple[str, ...], str, int, int]] = []
    for section in _sections(body):
        header = _header(section.heading_path, count, window)
        fitter = _Fitter(body, header, count, window)
        spans.extend(
            (section.heading_path, header, s, e)
            for s, e in fitter.split(section.start, section.end)
        )
    passages: list[KbPassage] = []
    for ordinal, (path, header, start, end) in enumerate(spans):
        text = body[start:end]
        embed_text = header + text
        passages.append(
            KbPassage(
                ordinal=ordinal,
                heading_path=path,
                start=start,
                end=end,
                text=text,
                embed_text=embed_text,
                tokens=count(embed_text),
            )
        )
    return passages


def _sections(body: str) -> list[_Section]:
    """Cut *body* at every ATX heading outside a fenced code block."""
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    path: tuple[str, ...] = ()
    start = 0
    offset = 0
    fence: str | None = None
    for line in body.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        fence_match = _FENCE.match(stripped)
        if fence_match is not None:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
        elif fence is None and (heading := _HEADING.match(stripped)) is not None:
            if offset > start:
                sections.append(_Section(path, start, offset))
            level = len(heading.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading.group(2).strip()))
            path = tuple(title for _level, title in stack)
            start = offset
        offset += len(line)
    if offset > start:
        sections.append(_Section(path, start, offset))
    return sections


def _header(path: tuple[str, ...], count: Counter, window: int) -> str:
    """Return the embed header for *path*, or ``""`` when it is too long."""
    if not path:
        return ""
    header = _HEADER_JOIN.join(path) + "\n\n"
    return header if count(header) <= window // 4 else ""


class _Fitter:
    """Cut one section into spans whose ``header + text`` fits the window."""

    def __init__(self, body: str, header: str, count: Counter, window: int) -> None:
        self._body = body
        self._header = header
        self._count = count
        self._window = window

    def _trim(self, start: int, end: int) -> tuple[int, int]:
        while start < end and self._body[start].isspace():
            start += 1
        while end > start and self._body[end - 1].isspace():
            end -= 1
        return start, end

    def _fits(self, start: int, end: int) -> bool:
        start, end = self._trim(start, end)
        return self._count(self._header + self._body[start:end]) <= self._window

    def split(self, start: int, end: int, level: int = 0) -> list[tuple[int, int]]:
        start, end = self._trim(start, end)
        if start >= end:
            return []
        if self._fits(start, end):
            return [(start, end)]
        if level == len(_BREAKS):
            return self._split_chars(start, end)
        units = self._units(start, end, _BREAKS[level])
        if len(units) == 1:
            return self.split(start, end, level + 1)
        return self._pack(units, level)

    def _units(self, start: int, end: int, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
        cuts = [m.end() for m in pattern.finditer(self._body, start, end) if start < m.end() < end]
        bounds = [start, *cuts, end]
        return [(a, b) for a, b in itertools.pairwise(bounds) if a < b]

    def _pack(self, units: Sequence[tuple[int, int]], level: int) -> list[tuple[int, int]]:
        """Join neighbouring units while they fit; cut a unit that alone is too big."""
        spans: list[tuple[int, int]] = []
        i = 0
        while i < len(units):
            first = units[i][0]
            if not self._fits(first, units[i][1]):
                spans.extend(self.split(units[i][0], units[i][1], level + 1))
                i += 1
                continue
            last = self._last_fitting(units, i)
            span = self._trim(first, units[last][1])
            if span[0] < span[1]:
                spans.append(span)
            i = last + 1
        return spans

    def _last_fitting(self, units: Sequence[tuple[int, int]], i: int) -> int:
        """Return the largest ``j`` such that ``units[i..j]`` fit together.

        Grows the step 1, 2, 4, ... until a span does not fit, then
        searches between the last fit and the first miss. Every probe
        stays close to the window size, so long sections stay cheap.
        """
        first = units[i][0]
        good, step = i, 1
        bad = len(units)
        while good + step < len(units):
            if self._fits(first, units[good + step][1]):
                good += step
                step *= 2
            else:
                bad = good + step
                break
        while bad - good > 1:
            mid = (good + bad) // 2
            if self._fits(first, units[mid][1]):
                good = mid
            else:
                bad = mid
        return good

    def _split_chars(self, start: int, end: int) -> list[tuple[int, int]]:
        """Last resort: the longest prefix that fits, then the rest, at least one character each."""
        spans: list[tuple[int, int]] = []
        while start < end:
            good, bad = start + 1, end + 1
            while bad - good > 1:
                mid = (good + bad) // 2
                if self._fits(start, mid):
                    good = mid
                else:
                    bad = mid
            if not self._fits(start, good):
                raise ValueError(f"window of {self._window} tokens cannot hold one character")
            span = self._trim(start, good)
            if span[0] < span[1]:
                spans.append(span)
            start, end = self._trim(good, end)
        return spans


def index_generation(embedding_model: str) -> str:
    """Name the indexing generation: the chunker version plus the model.

    The same body indexed under another chunker or model is a new
    generation, so its passages get new ids.
    """
    return f"{CHUNKER_VERSION}+{embedding_model}"


def revision_uri(entry_id: uuid.UUID, revision_id: uuid.UUID) -> str:
    """Return the MCP resource URI that reads one exact revision."""
    return f"meho://kb/id/{entry_id}/revision/{revision_id}"


def _passage_source_id(revision_id: uuid.UUID, generation: str, ordinal: int) -> str:
    tag = hashlib.sha256(generation.encode("utf-8")).hexdigest()[:12]
    return f"{revision_id}:{tag}:{ordinal}"


def passage_items(
    *,
    passages: Sequence[KbPassage],
    entry_id: uuid.UUID,
    revision_id: uuid.UUID,
    slug: str,
    metadata: dict[str, Any],
    embedding_model: str,
) -> list[IndexItem]:
    """Build one :class:`IndexItem` per passage of one revision.

    Each row carries the entry's metadata (so metadata filters match
    passages the same way they matched the entry) plus the locator under
    :data:`META_KB_PASSAGE`.
    """
    generation = index_generation(embedding_model)
    items: list[IndexItem] = []
    for passage in passages:
        locator = {
            "entry_id": str(entry_id),
            "revision_id": str(revision_id),
            "slug": slug,
            "ordinal": passage.ordinal,
            "passage_count": len(passages),
            "heading_path": list(passage.heading_path),
            "start": passage.start,
            "end": passage.end,
            "chunker_version": CHUNKER_VERSION,
            "embedding_model": embedding_model,
            "index_generation": generation,
        }
        items.append(
            IndexItem(
                source=KB_PASSAGE_SOURCE,
                source_id=_passage_source_id(revision_id, generation, passage.ordinal),
                kind=KB_KIND_PASSAGE,
                body=passage.text,
                metadata={**metadata, META_KB_PASSAGE: locator},
                embed_text=passage.embed_text,
                tokens=passage.tokens,
            )
        )
    return items
