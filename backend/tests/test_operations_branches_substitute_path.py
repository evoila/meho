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

from meho_backplane.operations._branches import _RFC6570_RESERVED_SAFE, _substitute_path


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


# --- S04: reserved-expansion path-escape hardening -------------------------


def test_reserved_safe_set_excludes_query_and_fragment_delimiters() -> None:
    """``?`` / ``#`` are never in the reserved safe set (#S04).

    Leaving them literal would let a caller-supplied ``{+var}`` value open a
    query string or fragment and address a resource the op's audit gate never
    authorised (which keys on ``descriptor.op_id``, not the resolved path).
    """
    assert "?" not in _RFC6570_RESERVED_SAFE
    assert "#" not in _RFC6570_RESERVED_SAFE


def test_reserved_expansion_encodes_query_and_fragment_delimiters() -> None:
    """A ``?`` / ``#`` in a ``{+var}`` value is percent-encoded, not passed through."""
    assert _substitute_path("/v1/{+p}", {"p": "a?b"}) == "/v1/a%3Fb"
    assert _substitute_path("/v1/{+p}", {"p": "a#b"}) == "/v1/a%23b"


@pytest.mark.parametrize(
    "value",
    [
        "..",  # bare
        "../etc/passwd",  # leading
        "a/../b",  # interior
        "a/..",  # trailing
        "../../etc/passwd",  # multi-level
        "%2e%2e/x",  # already percent-encoded
    ],
)
def test_dot_segment_value_is_rejected_before_substitution(value: str) -> None:
    """A ``..`` dot-segment value raises ``ValueError`` before it reaches the wire.

    Rejected in **both** expansion forms — a traversal segment is never a
    legitimate path value, and reserved expansion keeps ``/`` literal so a
    ``..`` there would climb out of the op's declared path (#S04). The
    dispatcher surfaces the raise as a caller-side ``invalid_params`` fault.
    """
    with pytest.raises(ValueError, match="traversal"):
        _substitute_path("/v1/{+p}", {"p": value})
    with pytest.raises(ValueError, match="traversal"):
        _substitute_path("/v1/{p}", {"p": value})


def test_dot_in_a_larger_segment_is_data_not_a_dot_segment() -> None:
    """``..`` embedded in a larger segment is data and passes through unchanged."""
    assert _substitute_path("/v1/{+p}", {"p": "a..b"}) == "/v1/a..b"
    assert _substitute_path("/v1/{+p}", {"p": "text/CONTAINS .."}) == "/v1/text/CONTAINS%20.."
