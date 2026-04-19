# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Shared TimeRange Model for Observability Connectors.

UTC-normalized time range with auto-resolved step for Prometheus/Loki/Tempo queries.
Parses relative time expressions ('1h', '30m', '7d') and computes optimal query step.

Step resolution table (from user decisions):
  15m -> 15s step
  1h  -> 30s step
  6h  -> 5m  step
  24h -> 15m step
  7d  -> 1h  step
"""

import re
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

# Step resolution table: total_seconds threshold -> step string
# Sorted by threshold ascending for linear scan
STEP_TABLE: list[tuple[int, str]] = [
    (15 * 60, "15s"),  # <= 15m -> 15s step
    (60 * 60, "30s"),  # <= 1h  -> 30s step
    (6 * 60 * 60, "5m"),  # <= 6h  -> 5m  step
    (24 * 60 * 60, "15m"),  # <= 24h -> 15m step
    (7 * 24 * 60 * 60, "1h"),  # <= 7d  -> 1h  step
]

_DURATION_RE = re.compile(r"^(\d+)(m|h|d)$")
_MULTIPLIERS = {"m": 60, "h": 3600, "d": 86400}


class TimeRange(BaseModel):
    """UTC-normalized time range with auto-resolved step."""

    start: datetime
    end: datetime
    step: str  # Prometheus step string (e.g., "30s", "5m", "1h")

    @classmethod
    def from_relative(cls, expr: str) -> "TimeRange":
        """
        Parse relative time expression into TimeRange with auto step.

        Args:
            expr: Relative expression like '1h', '30m', '7d', '15m'

        Returns:
            TimeRange with UTC-aware start/end and resolved step

        Raises:
            ValueError: If expression format is invalid
        """
        seconds = _parse_duration(expr)
        now = datetime.now(UTC)
        start = now - timedelta(seconds=seconds)
        step = _resolve_step(seconds)
        return cls(start=start, end=now, step=step)

    def to_prometheus_params(self) -> dict:
        """Convert to Prometheus query_range parameters."""
        return {
            "start": self.start.timestamp(),
            "end": self.end.timestamp(),
            "step": self.step,
        }


def _parse_duration(expr: str) -> int:
    """Parse duration string to total seconds."""
    match = _DURATION_RE.match(expr.strip())
    if not match:
        raise ValueError(
            f"Invalid time range expression: '{expr}'. Expected format: '1h', '30m', '7d'"
        )
    value = int(match.group(1))
    unit = match.group(2)
    return value * _MULTIPLIERS[unit]


def _resolve_step(total_seconds: int) -> str:
    """Auto-resolve step from total duration using step table."""
    for threshold, step in STEP_TABLE:
        if total_seconds <= threshold:
            return step
    return "1h"  # Fallback for > 7d
