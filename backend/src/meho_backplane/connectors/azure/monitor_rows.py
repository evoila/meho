# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reading Azure Monitor metric answers and turning them into numbers.

Azure Monitor answers a metric read with one time series per metric. Each point
has a time stamp and the values Azure could make for that hour. A point without
a value means Azure had no sample for that hour (for example because the VM was
deallocated). This module never turns such a gap into zero: a missing hour stays
``None`` and is left out of every average, percentile and maximum.

The rules in one place:

* The window is a whole number of hours. Hour ``i`` starts at
  ``start + i`` hours. Points outside the window, or not on the hour, are
  ignored.
* A number that is not a finite number (text, ``NaN``, a boolean) is a gap.
* The 95th percentile is the nearest-rank percentile of the hourly values.
* Memory used is worked out per hour from the available memory and the memory
  size of the VM's size, and kept between 0 and 100.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

__all__ = [
    "CPU_METRIC",
    "MEMORY_METRIC",
    "MIN_COVERAGE_PERCENT",
    "HourlyMetrics",
    "StatBlock",
    "cpu_block",
    "failed_text",
    "hour_time",
    "memory_block",
    "parse_metrics",
    "percentile_95",
    "round_value",
    "time_text",
    "used_percent",
    "vm_coverage_reason",
]

#: The Azure metric name of CPU use, in percent.
CPU_METRIC: Final[str] = "Percentage CPU"

#: The Azure metric name of free memory, in bytes.
MEMORY_METRIC: Final[str] = "Available Memory Bytes"

#: A VM needs at least this share of its hourly samples to count as measured.
MIN_COVERAGE_PERCENT: Final[float] = 80.0

_GIB: Final[int] = 2**30

_NAMES: Final[dict[str, str]] = {CPU_METRIC.lower(): "cpu", MEMORY_METRIC.lower(): "memory"}


@dataclass
class HourlyMetrics:
    """The hourly values of one VM, by hour index. A missing hour has no entry."""

    cpu: dict[int, dict[str, float]] = field(default_factory=dict)
    memory: dict[int, dict[str, float]] = field(default_factory=dict)
    #: Metrics Azure reported an error for (``errorCode`` other than ``Success``).
    failed: list[str] = field(default_factory=list)


def time_text(value: datetime) -> str:
    """A UTC time as Azure writes it, for example ``2026-10-01T08:00:00Z``."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value: object) -> float | None:
    """A finite number, or ``None``. A boolean is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _hour_index(stamp: object, start: datetime, hours: int) -> int | None:
    """The hour a time stamp belongs to, or ``None`` when it is outside the window."""
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = (moment - start).total_seconds()
    if seconds < 0 or seconds % 3600 != 0:
        return None
    index = int(seconds // 3600)
    return index if index < hours else None


def _points(metric: dict[str, Any], start: datetime, hours: int) -> dict[int, dict[str, float]]:
    """The values of one metric by hour. The first point of an hour wins."""
    series = metric.get("timeseries")
    if not isinstance(series, list) or not series or not isinstance(series[0], dict):
        return {}
    data = series[0].get("data")
    found: dict[int, dict[str, float]] = {}
    for point in data if isinstance(data, list) else []:
        if not isinstance(point, dict):
            continue
        index = _hour_index(point.get("timeStamp"), start, hours)
        if index is None or index in found:
            continue
        values = {
            key: number
            for key in ("average", "minimum", "maximum")
            if (number := _number(point.get(key))) is not None
        }
        if values:
            found[index] = values
    return found


def parse_metrics(metrics: object, start: datetime, hours: int) -> HourlyMetrics:
    """Read the ``value`` list of one resource's metrics answer.

    Metrics other than CPU and available memory are ignored. A metric Azure
    reports an error for adds its name to ``failed`` and has no points.
    """
    result = HourlyMetrics()
    for metric in metrics if isinstance(metrics, list) else []:
        if not isinstance(metric, dict):
            continue
        name = metric.get("name")
        raw = name.get("value") if isinstance(name, dict) else None
        kind = _NAMES.get(raw.lower()) if isinstance(raw, str) else None
        if kind is None:
            continue
        code = metric.get("errorCode")
        if isinstance(code, str) and code.lower() != "success":
            result.failed.append(kind)
            continue
        getattr(result, kind).update(_points(metric, start, hours))
    return result


def percentile_95(values: list[float]) -> float | None:
    """The nearest-rank 95th percentile, or ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(0.95 * len(ordered)), 1) - 1]


def round_value(value: float | None) -> float | None:
    """Two decimals, and ``None`` stays ``None``."""
    return None if value is None else round(value, 2)


def failed_text(parsed: HourlyMetrics) -> str | None:
    """A sentence when Azure reported an error for a metric, else ``None``."""
    if not parsed.failed:
        return None
    names = " and ".join("CPU" if kind == "cpu" else "memory" for kind in parsed.failed)
    return f"Azure reported an error for the {names} metric, so those hours are unknown."


def _coverage(found: int, expected: int) -> float:
    return round(100.0 * found / expected, 1) if expected > 0 else 0.0


@dataclass(frozen=True)
class StatBlock:
    """Average, 95th percentile and maximum of one measure, and how well it is covered."""

    average: float | None
    p95: float | None
    maximum: float | None
    samples_found: int
    coverage_percent: float
    samples_expected: int

    @property
    def sufficient(self) -> bool:
        """Enough samples, and a number to size from.

        Compares the counts, not the rounded ``coverage_percent``: 403 of 504 is
        79.96%, which rounds to 80.0 but is below the cutoff.
        """
        if self.average is None or self.samples_expected <= 0:
            return False
        return 100 * self.samples_found >= MIN_COVERAGE_PERCENT * self.samples_expected


def _block(typical: list[float], peaks: list[float], expected: int) -> StatBlock:
    average = sum(typical) / len(typical) if typical else None
    return StatBlock(
        average=round_value(average),
        p95=round_value(percentile_95(typical)),
        maximum=round_value(max(peaks) if peaks else None),
        samples_found=len(typical),
        coverage_percent=_coverage(len(typical), expected),
        samples_expected=expected,
    )


def cpu_block(cpu: dict[int, dict[str, float]], expected: int) -> StatBlock:
    """CPU use in percent: the hourly averages, and the highest hourly maximum."""
    typical = [v["average"] for v in cpu.values() if "average" in v]
    peaks = [v["maximum"] for v in cpu.values() if "maximum" in v]
    return _block(typical, peaks, expected)


def used_percent(available_bytes: float, memory_gb: float) -> float:
    """Memory used in percent, from the free bytes and the size of the memory."""
    total = memory_gb * _GIB
    return min(100.0, max(0.0, 100.0 * (1.0 - available_bytes / total)))


def memory_block(
    memory: dict[int, dict[str, float]], memory_gb: float | None, expected: int
) -> StatBlock:
    """Memory used in percent. Needs the memory size; without it every value is unknown.

    The average and the percentile come from the hourly average of free memory.
    The maximum comes from the lowest free memory of an hour, the peak of use.
    Without the memory size the samples are still counted, but the block is
    never sufficient: there is no number to size from.
    """
    if memory_gb is None or memory_gb <= 0:
        found = sum(1 for v in memory.values() if "average" in v)
        return StatBlock(None, None, None, found, _coverage(found, expected), expected)
    typical = [used_percent(v["average"], memory_gb) for v in memory.values() if "average" in v]
    peaks = [used_percent(v["minimum"], memory_gb) for v in memory.values() if "minimum" in v]
    return _block(typical, peaks, expected)


def hour_time(start: datetime, index: int) -> str:
    """The start of hour *index* of the window, as text."""
    return time_text(start + timedelta(hours=index))


def vm_coverage_reason(
    block: StatBlock, expected: int, power_state: str | None, *, missing_data: bool
) -> str | None:
    """Why a VM has too little data, or ``None`` when it has enough."""
    if block.sufficient:
        return None
    if missing_data:
        return "Azure returned no metric data for this VM."
    text = (
        f"Only {block.samples_found} of {expected} hourly samples were found "
        f"({block.coverage_percent:g}%); at least {MIN_COVERAGE_PERCENT:g}% is needed."
    )
    if power_state in ("deallocated", "stopped"):
        text += " The VM is off now, and hours when it was off have no samples."
    return text
