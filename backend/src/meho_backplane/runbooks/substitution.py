# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runtime substitution of ``${run.target}`` / ``${run.params.X}`` tokens (G12.3-T2, #1301).

Companion to :func:`meho_backplane.runbooks.schemas.validate_substitutions`
-- T1 (#1295) rejects disallowed substitution patterns at **publish time**;
this module **resolves** the allowed patterns at **advance time**, called
from the engine (G12.3-T2) on the way to building a
:class:`~meho_backplane.runbooks.runs_schemas.StepBody` for the operator's
current position.

Two patterns are allowlisted by Initiative #1198, both single-flat-level:

* ``${run.target}`` -- the run's subject (the host, cluster, cert
  thumbprint) supplied at ``runbook_start`` time.
* ``${run.params.X}`` -- one of the named run parameters, where ``X``
  matches ``[a-z_][a-z0-9_]*``. Nested paths (``${run.params.X.Y}``) are
  not allowed and are rejected at publish time -- this module assumes the
  body it walks has already cleared that gate.

The function is **pure**: no DB session, no contextvars, no clocks. The
service layer (G12.3-T3) is the boundary that supplies *target* and
*params* from the persisted :class:`RunbookRun` row; this module is
deliberately storage-blind.

The regex constants are exported so the engine, the service, and future
tests can share one source of truth. T1's :data:`_SUBSTITUTION_PATTERN`
in :mod:`meho_backplane.runbooks.schemas` covers the publish-time
allowlist surface (where rejecting *any* disallowed ``${...}`` is the
point) and intentionally stays separate -- a future refactor (deferred
to G12.3-T3 per #1301 scope) can have ``validate_substitutions`` import
the same two narrow patterns from here, but T2 stays focused.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "RUN_PARAMS_PATTERN",
    "RUN_TARGET_PATTERN",
    "resolve_substitutions",
]


#: Matches the literal ``${run.target}`` substitution token.
#:
#: ``re.sub`` consumers replace every occurrence with the run's target
#: string. There is no capture group -- the entire match is the token.
RUN_TARGET_PATTERN: Final[re.Pattern[str]] = re.compile(r"\$\{run\.target\}")

#: Matches ``${run.params.X}`` where ``X`` is ``[a-z_][a-z0-9_]*``.
#:
#: The single capture group is the bare parameter name (``X``) -- the
#: substitution callback indexes ``params[X]`` to find the replacement.
#: A nested ``${run.params.X.Y}`` does not match (the inner ``.`` would
#: have to be part of the param-name grammar, which it is not) -- such
#: patterns are publish-time-rejected by T1's :func:`validate_substitutions`.
RUN_PARAMS_PATTERN: Final[re.Pattern[str]] = re.compile(r"\$\{run\.params\.([a-z_][a-z0-9_]*)\}")


def resolve_substitutions(
    value: object,
    *,
    target: str,
    params: dict[str, object],
) -> object:
    """Walk *value* and resolve ``${run.target}`` / ``${run.params.X}``.

    Recursive over ``str`` / ``dict`` / ``list``; other scalar types
    (``int``, ``float``, ``bool``, ``None``) pass through verbatim.

    For string values both regex patterns are applied:

    * Each ``${run.target}`` occurrence is replaced with ``str(target)``.
    * Each ``${run.params.X}`` occurrence is replaced with ``str(params[X])``.

    For dict values the function recurses on each value; **keys stay
    literal** -- a substitution smuggled into a key was already rejected
    at publish time by :func:`meho_backplane.runbooks.schemas.validate_substitutions`,
    so attempting to resolve one here would mask a contract violation.

    For list values the function recurses on each element. The returned
    container is a new ``list`` / ``dict`` (not a mutated copy) so the
    caller's input is unchanged -- consistent with the
    :class:`pydantic.BaseModel`'s frozen posture on the engine's inputs.

    A non-resolvable ``${run.params.X}`` (key not present in *params*)
    surfaces as :class:`KeyError` with the missing param name. T3's
    service is expected to translate that into a typed error before it
    reaches the engine -- so reaching this branch indicates a publish /
    start invariant was bypassed (defense in depth).

    The function does **not** validate the input against the allowlist
    -- that is the publish-time gate's job. Any ``${something_else}``
    pattern in *value* passes through verbatim (matched by neither
    pattern), which is exactly the behavior the engine wants: the run-
    time helper resolves the good patterns and leaves anything else
    alone, because the publish gate already guaranteed nothing else can
    be present in a well-formed template.
    """
    if isinstance(value, str):
        return _resolve_string(value, target=target, params=params)
    if isinstance(value, dict):
        return {
            key: resolve_substitutions(item, target=target, params=params)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [resolve_substitutions(item, target=target, params=params) for item in value]
    return value


def _resolve_string(value: str, *, target: str, params: dict[str, object]) -> str:
    """Apply both substitution patterns to a single string.

    Target replacement runs first so a ``${run.target}`` that resolves
    to a literal string containing ``${run.params.X}`` is **not**
    re-expanded (the resolved value is data, not template). Param
    replacement runs second over the now-stripped-of-target string.
    """
    resolved = RUN_TARGET_PATTERN.sub(lambda _: target, value)
    return RUN_PARAMS_PATTERN.sub(lambda m: _lookup_param(m.group(1), params), resolved)


def _lookup_param(name: str, params: dict[str, object]) -> str:
    """Return ``str(params[name])`` or raise :class:`KeyError`.

    Wrapped in its own helper so the :func:`re.Pattern.sub` callback in
    :func:`_resolve_string` stays a one-liner and the error surface is
    explicit -- ``KeyError(name)`` carries the bare param name as the
    arg, so the engine's :class:`KeyError` handler (T3) can surface a
    typed error with the missing name intact.
    """
    if name not in params:
        raise KeyError(name)
    return str(params[name])
