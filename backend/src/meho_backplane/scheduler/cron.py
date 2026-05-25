# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cron-expression parsing and ``next_fire_at`` arithmetic (G11.3-T2 #823).

Thin wrapper over ``croniter`` (the only new dep this task introduces;
pure-Python, single-purpose, ~1.5 kLoC) so the rest of the scheduler
package depends on a narrow seam rather than the library directly --
the same posture
:mod:`meho_backplane.agent.run` follows for ``pydantic_ai``.

What this module does:

* :func:`is_valid_cron_expr` -- application-level validation for a cron
  expression *before* it lands in the DB.
* :func:`next_fire_after` -- compute the next scheduled instant given
  an expression, a base time, and a timezone. Used by the repository
  on insert (compute the first ``next_fire_at``) and by the loop after
  each fire (advance to the next match).

Why we pin the base + tz pair instead of letting croniter default
==================================================================

``croniter`` will happily accept a naive base datetime and compute
"next" against the host clock's local time, which means two replicas
in different host TZs (or a deployment that migrates between hosts)
disagree on the next-fire instant for the same expression. Passing an
aware base (constructed from the row's persisted timezone) is what
makes the computation deterministic and replica-safe.

croniter 6.x notes
==================

The installed library is 6.2.2 (verified at worktree-sync time, see
the PR body's framework_research). The 6.x API used here:

* :class:`croniter.croniter` constructor takes ``(expr, base_dt)``.
* :meth:`croniter.croniter.is_valid` is a classmethod that returns
  ``bool``.
* :meth:`croniter.croniter.get_next` returns a Python object; calling
  with ``ret_type=datetime`` returns a tz-aware datetime when ``base``
  was tz-aware.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

__all__ = [
    "InvalidCronExpressionError",
    "InvalidTimezoneError",
    "is_valid_cron_expr",
    "next_fire_after",
    "resolve_timezone",
]


class InvalidCronExpressionError(ValueError):
    """Raised when a cron expression fails ``croniter``'s validation.

    A typed exception (not a bare ``ValueError``) so the create-trigger
    service can map it to a 400 response with an actionable message,
    while internal callers (the loop on a row with a corrupted expr)
    can distinguish it from generic value errors.
    """

    def __init__(self, expr: str) -> None:
        self.expr = expr
        super().__init__(f"invalid cron expression: {expr!r}")


class InvalidTimezoneError(ValueError):
    """Raised when an IANA timezone string is not resolvable.

    Same shape as :class:`InvalidCronExpressionError`. ``zoneinfo``
    raises :class:`ZoneInfoNotFoundError` for an unknown TZ; we
    re-raise as this typed exception so callers do not depend on
    stdlib internals.
    """

    def __init__(self, tz_name: str) -> None:
        self.tz_name = tz_name
        super().__init__(f"unknown timezone: {tz_name!r}")


def is_valid_cron_expr(expr: str) -> bool:
    """Return ``True`` when *expr* is a valid 5-field cron expression.

    Thin pass-through to ``croniter.croniter.is_valid``. Exposed as a
    module-level function so the rest of the codebase imports one name
    and the croniter dependency stays confined to this module.
    """
    return bool(croniter.is_valid(expr))


def resolve_timezone(tz_name: str) -> tzinfo:
    """Resolve an IANA timezone name to a :class:`tzinfo`.

    Empty string and ``"UTC"`` both resolve to :data:`datetime.UTC`
    (no ``zoneinfo`` lookup) so deployments without a tzdata package
    still work for the common case. Any other name goes through
    :class:`zoneinfo.ZoneInfo`; an unresolvable name raises
    :class:`InvalidTimezoneError`.
    """
    if not tz_name or tz_name == "UTC":
        return UTC
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise InvalidTimezoneError(tz_name) from exc


def next_fire_after(
    expr: str,
    base: datetime,
    tz_name: str = "UTC",
) -> datetime:
    """Compute the next cron match strictly after *base*.

    Both inputs and the returned value are tz-aware:

    * If *base* is naive, it is treated as wall-clock time *in the
      trigger's tz* and reattached -- the most defensible behaviour for
      a corrupted row (the alternative is to crash on every fire, which
      stops the loop).
    * The returned datetime is in UTC, normalised so the
      ``next_fire_at`` column always stores a UTC instant regardless of
      the source timezone. Operators inspecting the row see consistent
      timestamps; the persisted ``timezone`` column records what the
      expression's semantics are interpreted in.

    Raises:
        InvalidCronExpressionError: *expr* fails croniter's validation.
        InvalidTimezoneError: *tz_name* is not a valid IANA zone.
    """
    if not croniter.is_valid(expr):
        raise InvalidCronExpressionError(expr)
    tz = resolve_timezone(tz_name)
    # Normalise *base* into the trigger's timezone. Naive base -> attach
    # the trigger's tz; aware base -> convert to the trigger's tz so the
    # cron-fields semantic (hour 9 = 9am in *this* zone) holds.
    anchored = base.replace(tzinfo=tz) if base.tzinfo is None else base.astimezone(tz)
    itr = croniter(expr, anchored)
    next_local: datetime = itr.get_next(datetime)
    # Persist as UTC for cross-tz determinism.
    return next_local.astimezone(UTC)
