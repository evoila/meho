# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.audit_query.duration` (G8.1-T2).

Covers the grammar the parser advertises:

* Relative shorthand for every supported unit (``s`` / ``m`` / ``h`` /
  ``d`` / ``w``) round-trips to the expected ``now - timedelta``.
* The 4-digit upper bound (``9999<unit>``) accepts; 5-digit rejects.
* ISO-8601 absolute strings parse through :meth:`datetime.fromisoformat`,
  including the Python 3.11+ offset-aware forms.
* Naive ISO-8601 strings are interpreted as UTC.
* The empty string and every "off-grammar" shape reject with
  :class:`DurationParseError`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from meho_backplane.audit_query import DurationParseError, parse_duration

# Frozen reference clock for every relative-grammar assertion in this
# module. Pinning the clock keeps the assertions reproducible without
# monkey-patching :func:`datetime.now`, mirroring the contract
# :func:`parse_duration` advertises (``now`` is a mandatory keyword
# argument exactly so tests can inject one).
_REF = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30s", datetime(2026, 5, 14, 11, 59, 30, tzinfo=UTC)),
        ("5m", datetime(2026, 5, 14, 11, 55, 0, tzinfo=UTC)),
        ("24h", datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)),
        ("7d", datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)),
        ("2w", datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)),
    ],
)
def test_relative_grammar_for_every_unit(value: str, expected: datetime) -> None:
    """One assertion per supported unit suffix — the full grammar surface."""
    assert parse_duration(value, now=_REF) == expected


def test_n_unit_upper_bound_4_digits_accepts() -> None:
    """``9999<unit>`` is the largest accepted N — exercises the regex bound."""
    parse_duration("9999d", now=_REF)


def test_n_unit_5_digits_rejects() -> None:
    """``10000d`` falls past the 4-digit regex bound — :class:`DurationParseError`."""
    with pytest.raises(DurationParseError):
        parse_duration("10000d", now=_REF)


def test_iso8601_offset_aware_parses_through() -> None:
    """Offset-aware ISO-8601 surfaces with the operator-supplied tzinfo intact."""
    parsed = parse_duration("2026-04-01T00:00:00+00:00", now=_REF)
    assert parsed == datetime(2026, 4, 1, tzinfo=UTC)


def test_iso8601_naive_interpreted_as_utc() -> None:
    """A naive ISO-8601 date is interpreted as UTC — the documented choice."""
    parsed = parse_duration("2026-04-01", now=_REF)
    assert parsed.tzinfo is UTC
    assert parsed == datetime(2026, 4, 1, tzinfo=UTC)


def test_empty_string_rejects() -> None:
    """Empty input is a parser-level rejection, not a fall-through."""
    with pytest.raises(DurationParseError):
        parse_duration("", now=_REF)


@pytest.mark.parametrize(
    "garbage",
    [
        "abc",
        "24x",  # unsupported unit
        "h24",  # unit-first instead of value-first
        "24 h",  # whitespace
        "-5d",  # negative
        "1.5d",  # fractional
        "24hh",  # double unit
    ],
)
def test_off_grammar_inputs_reject(garbage: str) -> None:
    """Off-grammar inputs surface as :class:`DurationParseError` — never silent."""
    with pytest.raises(DurationParseError):
        parse_duration(garbage, now=_REF)
