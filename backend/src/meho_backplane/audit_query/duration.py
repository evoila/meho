# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Duration parser for the audit-query REST surface (G8.1-T2).

Routers under :mod:`meho_backplane.api.v1.audit` accept ``since`` /
``until`` as either operator-friendly relative shorthand (``"24h"`` /
``"7d"`` / ``"30m"``) or absolute ISO-8601 strings. The T1 substrate
(:class:`~meho_backplane.audit_query.schemas.AuditQueryFilters`) takes
:class:`datetime` only — duration parsing belongs at the router layer
per the substrate docstring contract.

Grammar
=======

* ``<N><unit>`` where ``unit`` ∈ ``{s, m, h, d, w}``. ``N`` is an
  unsigned integer ≤ 9999. Result is ``now - timedelta``.
* Otherwise the value is parsed with :meth:`datetime.fromisoformat`
  (Python 3.11+ accepts the ``Z`` suffix and offset-aware forms).
  Naive datetimes are interpreted as UTC so the downstream
  ``occurred_at`` comparison lands in a single timezone — matching
  the precedent
  :func:`~meho_backplane.retrieval.usage.parse_since` set at line 256.
* Anything else raises :class:`DurationParseError`.

The companion parser
:func:`~meho_backplane.retrieval.usage.parse_since` accepts a subset
(``d`` / ``h`` only) because retrieval-usage telemetry deals in
operator-day granularity. Audit forensics often wants sub-minute
resolution (``"30s"``) and multi-week windows (``"2w"``), so the audit
parser ships a wider grammar. Unifying the two is a v0.2.next surface.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Final

__all__ = [
    "DurationParseError",
    "parse_duration",
]


class DurationParseError(ValueError):
    """Raised by :func:`parse_duration` when the input doesn't parse.

    Subclasses :class:`ValueError` so router-side ``except`` blocks
    catch the standard exception; the dedicated subclass lets test
    code pin "this specific parser rejected the input" without
    matching on every :class:`ValueError` the call stack might raise.
    """


_RELATIVE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<n>\d{1,4})(?P<unit>[smhdw])",
)

_UNIT_TO_TIMEDELTA_KW: Final[dict[str, str]] = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def parse_duration(value: str, *, now: datetime) -> datetime:
    """Resolve a duration string to an absolute UTC datetime.

    *now* is injected explicitly so tests can pin a frozen clock
    without monkey-patching :func:`datetime.now`. Production callers
    pass ``datetime.now(UTC)``.

    Examples
    --------

    >>> from datetime import UTC, datetime
    >>> ref = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    >>> parse_duration("24h", now=ref).isoformat()
    '2026-05-13T12:00:00+00:00'
    >>> parse_duration("7d", now=ref).isoformat()
    '2026-05-07T12:00:00+00:00'
    >>> parse_duration("30m", now=ref).isoformat()
    '2026-05-14T11:30:00+00:00'
    >>> parse_duration("2w", now=ref).isoformat()
    '2026-04-30T12:00:00+00:00'
    >>> parse_duration("2026-04-01", now=ref).isoformat()
    '2026-04-01T00:00:00+00:00'
    """
    if not value:
        raise DurationParseError("duration must be non-empty")

    rel = _RELATIVE_PATTERN.fullmatch(value)
    if rel is not None:
        n = int(rel.group("n"))
        unit = rel.group("unit")
        kw = _UNIT_TO_TIMEDELTA_KW[unit]
        return now - timedelta(**{kw: n})

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DurationParseError(
            f"unrecognised duration {value!r}: expected '<N>{{s|m|h|d|w}}' or an ISO-8601 datetime",
        ) from exc

    if parsed.tzinfo is None:
        # Naive datetimes from operators are interpreted as UTC. Any
        # other choice (server local, operator local) would be a
        # silent-correctness footgun: the ``occurred_at`` column is
        # ``timestamptz`` on PG and naive-UTC on SQLite; comparing a
        # naive non-UTC value against either would shift the window
        # by the local offset without the operator noticing.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
