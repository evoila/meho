# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.runbooks.substitution` (G12.3-T2, #1301).

The companion runtime helper to T1's publish-time
:func:`meho_backplane.runbooks.schemas.validate_substitutions`:
T1 rejects disallowed patterns, T2 resolves the allowlisted ones.

Coverage matrix:

* ``${run.target}`` resolves in a top-level string.
* ``${run.params.X}`` resolves in a top-level string with a non-string
  value (the contract is ``str(value)``).
* Recursion into nested dicts -- the resolver walks through the
  container shape, not just the top-level surface.
* Missing param surfaces as :class:`KeyError` with the bare param name.
* Non-string scalars (``int`` / ``bool`` / ``None``) pass through
  unchanged -- the engine relies on this so it can pump a whole
  ``params`` dict through ``resolve_substitutions`` without filtering.

The regex constants are exercised indirectly by every test that
substitutes; they are also referenced by name in the module-level
``__all__`` assertion so a future rename surfaces the breakage at
import time.
"""

from __future__ import annotations

import pytest

from meho_backplane.runbooks.substitution import (
    RUN_PARAMS_PATTERN,
    RUN_TARGET_PATTERN,
    resolve_substitutions,
)


def test_resolve_target_in_string() -> None:
    out = resolve_substitutions(
        "do X on ${run.target}",
        target="vc-01",
        params={},
    )
    assert out == "do X on vc-01"


def test_resolve_param_in_string() -> None:
    # Non-string param value -- the resolver coerces via ``str()`` so
    # the engine can pass arbitrary JSON-shaped params through.
    out = resolve_substitutions(
        "set ${run.params.threshold}",
        target="ignored",
        params={"threshold": 95},
    )
    assert out == "set 95"


def test_resolve_nested_in_dict() -> None:
    # The substitution walks into nested containers; keys stay literal.
    payload = {
        "host": "${run.target}",
        "args": {
            "threshold": "${run.params.threshold}",
            "literal": "no-substitution-here",
        },
        "list_of_strings": ["${run.target}", "${run.params.region}"],
    }
    out = resolve_substitutions(
        payload,
        target="vc-01",
        params={"threshold": 95, "region": "eu"},
    )
    assert out == {
        "host": "vc-01",
        "args": {
            "threshold": "95",
            "literal": "no-substitution-here",
        },
        "list_of_strings": ["vc-01", "eu"],
    }


def test_resolve_missing_param_raises_keyerror() -> None:
    # A bare missing param: KeyError carrying the bare param name as
    # the single arg so T3 can surface a typed wire error with the
    # name intact.
    with pytest.raises(KeyError) as excinfo:
        resolve_substitutions(
            "set ${run.params.missing}",
            target="vc-01",
            params={},
        )
    assert excinfo.value.args == ("missing",)


def test_resolve_non_string_passthrough() -> None:
    # Scalars other than str carry no substitution surface; the
    # resolver returns them as-is so the engine can call
    # ``resolve_substitutions`` over a params dict without filtering.
    assert resolve_substitutions(42, target="vc-01", params={}) == 42
    assert resolve_substitutions(True, target="vc-01", params={}) is True
    assert resolve_substitutions(None, target="vc-01", params={}) is None
    assert resolve_substitutions(3.14, target="vc-01", params={}) == 3.14


def test_run_target_pattern_constant_matches_only_run_target() -> None:
    # Locks the regex constant against accidental relaxation -- the
    # engine and the publish-time helper share this single source of
    # truth.
    assert RUN_TARGET_PATTERN.findall("a ${run.target} b") == ["${run.target}"]
    assert RUN_TARGET_PATTERN.findall("nothing here") == []
    # The pattern must not also match ``${run.params.X}``.
    assert RUN_TARGET_PATTERN.findall("${run.params.X}") == []


def test_run_params_pattern_constant_captures_name() -> None:
    # The single capture group is the bare parameter name -- the
    # resolver depends on this exact group shape.
    names = RUN_PARAMS_PATTERN.findall("${run.params.foo} and ${run.params.bar}")
    assert names == ["foo", "bar"]
    # Capital letters in the param name are not part of the grammar.
    assert RUN_PARAMS_PATTERN.findall("${run.params.WithCaps}") == []
    # Nested paths are not part of the grammar.
    assert RUN_PARAMS_PATTERN.findall("${run.params.X.Y}") == []


def test_resolve_multiple_occurrences_in_one_string() -> None:
    # Both patterns can co-occur in a single string; both are resolved.
    out = resolve_substitutions(
        "on ${run.target}: ${run.params.action} from ${run.params.source}",
        target="vc-01",
        params={"action": "restart", "source": "operator-cli"},
    )
    assert out == "on vc-01: restart from operator-cli"


def test_resolve_unrecognised_substitution_passes_through() -> None:
    # A ``${foo}`` pattern that does not match either of the two
    # allowlisted forms passes through verbatim -- the publish-time
    # gate is the layer that rejects it; T2's resolver is intentionally
    # narrow (resolve the good patterns, leave anything else alone).
    out = resolve_substitutions(
        "literal ${unknown.thing} stays put",
        target="vc-01",
        params={},
    )
    assert out == "literal ${unknown.thing} stays put"


def test_resolve_preserves_input_immutability() -> None:
    # The resolver returns a new container; the caller's dict is
    # unchanged -- callers feed pydantic-frozen models, but downstream
    # services may pass shared dicts too.
    original_params = {"region": "eu"}
    payload = {"args": {"region": "${run.params.region}"}}
    out = resolve_substitutions(payload, target="vc-01", params=original_params)
    assert out == {"args": {"region": "eu"}}
    # The input payload is untouched.
    assert payload == {"args": {"region": "${run.params.region}"}}
    # The params dict is untouched.
    assert original_params == {"region": "eu"}
