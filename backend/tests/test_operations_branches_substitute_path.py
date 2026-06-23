# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``_substitute_path`` RFC6570 expansion semantics (#2003).

The ingested-op path substituter honours the RFC6570 expression
operator so a curated/ingested op's path template expands with the
encoding its author intended:

* simple expansion ``{var}`` (§3.2.2) percent-encodes reserved chars,
* reserved expansion ``{+var}`` / ``{#var}`` (§3.2.3) lets reserved
  structural chars (``/``, ``:``, ``,`` ...) pass through literal.

These tests pin the divergence between the two forms — the defect that
blocked vRLI's ``/api/v2/events/{+constraints}`` constraint queries, where
``{constraints}`` mangled the slash-delimited constraint chain into
``%2F``-soup — plus the latent ``KeyError`` a literal ``{+var}`` template
would have hit before the operator was stripped from the lookup name.
"""

from __future__ import annotations

import pytest

from meho_backplane.operations._branches import _substitute_path


def test_simple_expansion_encodes_reserved_slash() -> None:
    """``{var}`` percent-encodes ``/`` in the value (simple expansion)."""
    result = _substitute_path("/api/v2/events/{constraints}", {"constraints": "a/b"})
    assert result == "/api/v2/events/a%2Fb"


def test_reserved_expansion_keeps_reserved_slash_literal() -> None:
    """``{+var}`` lets ``/`` pass through literal (reserved expansion)."""
    result = _substitute_path("/api/v2/events/{+constraints}", {"constraints": "a/b"})
    assert result == "/api/v2/events/a/b"


def test_simple_and_reserved_diverge_on_same_value() -> None:
    """The two forms diverge: simple encodes ``/``, reserved keeps it.

    The load-bearing assertion of #2003 — same value, same param name,
    different operator, different wire encoding.
    """
    value = "text/CONTAINS error/hostname/CONTAINS vcsa"
    simple = _substitute_path("/api/v2/events/{constraints}", {"constraints": value})
    reserved = _substitute_path("/api/v2/events/{+constraints}", {"constraints": value})
    # Simple expansion mangles every separator.
    assert "%2F" in simple
    assert "/api/v2/events/a/b" not in simple  # belt-and-suspenders
    # Reserved expansion keeps the slash-delimited constraint chain literal.
    assert "/CONTAINS" in reserved
    assert "%2F" not in reserved


def test_reserved_expansion_still_encodes_genuinely_unsafe_chars() -> None:
    """A space stays ``%20`` under reserved expansion; only structural chars differ."""
    result = _substitute_path("/api/v2/events/{+constraints}", {"constraints": "a b"})
    assert result == "/api/v2/events/a%20b"


def test_reserved_operator_resolves_bare_param_name_no_keyerror() -> None:
    """``{+path}`` resolves the param keyed ``path`` (operator stripped from lookup).

    Regression guard for the latent ``KeyError``: before the operator was
    stripped, ``_PATH_VAR_RE`` captured ``+path`` (operator included) while
    the ingest pipeline names the param ``path``, so substitution raised
    ``KeyError`` → ``invalid_params`` before the request ever reached the wire.
    """
    result = _substitute_path("/v1/{+path}", {"path": "a/b/c"})
    assert result == "/v1/a/b/c"


def test_missing_param_still_raises_keyerror() -> None:
    """A genuinely-absent path var still raises ``KeyError`` (both forms)."""
    with pytest.raises(KeyError):
        _substitute_path("/v1/{+path}", {})
    with pytest.raises(KeyError):
        _substitute_path("/v1/{cluster}", {})
