# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Boundary cases for :func:`meho_backplane.memory.snippet.query_snippet` (#3751)."""

from __future__ import annotations

import time

from meho_backplane.memory.snippet import (
    SNIPPET_MAX_CHARS,
    SNIPPET_MAX_TERMS,
    SNIPPET_SCAN_MAX_CHARS,
    query_snippet,
)


def test_body_that_fits_is_returned_whole() -> None:
    body = "a" * SNIPPET_MAX_CHARS
    assert query_snippet(body, "zzz") == (body, False)


def test_no_match_starts_at_the_beginning() -> None:
    body = "b" * (SNIPPET_MAX_CHARS + 50)
    snippet, truncated = query_snippet(body, "nothing here")
    assert truncated is True
    assert snippet == "b" * SNIPPET_MAX_CHARS + "…"


def test_match_near_the_end_keeps_a_full_window() -> None:
    body = "c" * 1000 + " needle"
    snippet, truncated = query_snippet(body, "NEEDLE")
    assert truncated is True
    assert snippet.startswith("…")
    assert snippet.endswith("needle")
    assert len(snippet) == SNIPPET_MAX_CHARS + 1


def test_match_in_the_middle_is_marked_on_both_ends() -> None:
    body = "d" * 1000 + " target word " + "e" * 1000
    snippet, _ = query_snippet(body, "word")
    assert "target word" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) == SNIPPET_MAX_CHARS + 2


def test_one_letter_and_regex_characters_in_the_query_are_safe() -> None:
    body = "f" * 500 + " a.b(c) " + "g" * 500
    snippet, _ = query_snippet(body, "a ( ) .*")
    assert snippet == "f" * SNIPPET_MAX_CHARS + "…"


def test_only_the_longest_query_words_are_used() -> None:
    # Nine words of two letters each, plus one longer word. Only the
    # longest SNIPPET_MAX_TERMS words count, so the shortest is dropped.
    short_words = [f"{chr(97 + i)}{chr(97 + i)}" for i in range(SNIPPET_MAX_TERMS)]
    query = " ".join([*short_words, "longword"])
    dropped = sorted(short_words)[-1]
    body = "x" * 1000 + f" {dropped} " + "y" * 1000
    snippet, truncated = query_snippet(body, query)
    assert truncated is True
    assert snippet == "x" * SNIPPET_MAX_CHARS + "…"


def test_a_match_past_the_scan_limit_is_ignored() -> None:
    body = "z" * SNIPPET_SCAN_MAX_CHARS + " needle " + "w" * 1000
    snippet, truncated = query_snippet(body, "needle")
    assert truncated is True
    assert snippet == "z" * SNIPPET_MAX_CHARS + "…"


def test_a_match_just_inside_the_scan_limit_is_found() -> None:
    body = "z" * (SNIPPET_SCAN_MAX_CHARS - 10) + "needle" + "w" * 1000
    snippet, _ = query_snippet(body, "needle")
    assert "needle" in snippet


def test_many_words_and_a_long_body_still_return_a_window() -> None:
    query = " ".join(f"word{i:03d}" for i in range(85))
    body = "q" * (1024 * 1024)
    started = time.perf_counter()
    for _ in range(50):
        snippet, truncated = query_snippet(body, query)
    elapsed = time.perf_counter() - started
    assert truncated is True
    assert snippet == "q" * SNIPPET_MAX_CHARS + "…"
    assert elapsed < 1.0
