# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Boundary cases for :func:`meho_backplane.memory.snippet.query_snippet` (#3751)."""

from __future__ import annotations

from meho_backplane.memory.snippet import SNIPPET_MAX_CHARS, query_snippet


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
