# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Time-Series Data Reduction for Observability Connectors.

Computes summary statistics (min/max/avg/current/p95/trend) from Prometheus
range query results and returns top-N sorted by current value. Prevents
context overflow by reducing thousands of data points to a handful of numbers.

Also supports instant query results (single value per series).
"""

from typing import Any

import numpy as np


def summarize_time_series(  # NOSONAR (cognitive complexity)
    result: list[dict],
    entity_field: str,
    metric_name: str,
    top_n: int = 10,
) -> dict:
    """
    Convert Prometheus range query result to summary statistics.

    Input format (Prometheus range query):
        [{"metric": {"pod": "nginx-abc"}, "values": [[t1, "0.5"], [t2, "0.7"], ...]}]

    Output format:
        {
            "items": [{"pod": "nginx-abc", "cpu_cores": {"min": 0.3, "max": 0.9, ...}}],
            "total_count": 50,
            "showing": 10,
            "metric": "cpu_cores"
        }

    Handles:
    - NaN values (filtered out, Pitfall 2)
    - Empty results
    - Single data point (p95 = that value, trend = "stable")
    - All NaN values (series skipped)

    Args:
        result: Prometheus range query result list
        entity_field: Label key to use as entity identifier (e.g., "pod", "node")
        metric_name: Name for the metric in output (e.g., "cpu_cores", "memory_bytes")
        top_n: Maximum number of items to return, sorted by current value desc

    Returns:
        Summary dict with items, total_count, showing, and metric name
    """
    items: list[dict] = []

    for series in result:
        metric_labels = series.get("metric", {})
        label = metric_labels.get(entity_field, "unknown")

        # Parse float values, filter out NaN (Pitfall 2)
        raw_values = series.get("values", [])
        values = []
        for v in raw_values:
            str_val = str(v[1])
            if str_val.lower() == "nan":
                continue
            try:
                values.append(float(str_val))
            except (ValueError, TypeError):
                continue

        if not values:
            continue

        # Compute summary stats
        current = values[-1]
        p95 = float(np.percentile(values, 95)) if len(values) > 1 else current

        item: dict[str, Any] = {
            entity_field: label,
            metric_name: {
                "min": round(min(values), 4),
                "max": round(max(values), 4),
                "avg": round(sum(values) / len(values), 4),
                "current": round(current, 4),
                "p95": round(p95, 4),
                "trend": _compute_trend(values),
                "samples": len(values),
            },
        }

        # Include extra labels (excluding entity_field and __name__)
        for k, v in metric_labels.items():
            if k != entity_field and k != "__name__":
                item[k] = v

        items.append(item)

    # Sort by current value descending, take top_n
    items.sort(key=lambda x: x[metric_name]["current"], reverse=True)
    total = len(items)

    return {
        "items": items[:top_n],
        "total_count": total,
        "showing": min(top_n, total),
        "metric": metric_name,
    }


def summarize_instant(
    result: list[dict],
    entity_field: str,
    metric_name: str,
    top_n: int = 10,
) -> dict:
    """
    Convert Prometheus instant query result to summary statistics.

    Input format (Prometheus instant query):
        [{"metric": {"pod": "nginx-abc"}, "value": [timestamp, "0.5"]}]

    Same output format as summarize_time_series but stats are just the single
    value for min/max/avg/current, no p95 or trend.

    Args:
        result: Prometheus instant query result list
        entity_field: Label key to use as entity identifier
        metric_name: Name for the metric in output
        top_n: Maximum number of items to return

    Returns:
        Summary dict with items, total_count, showing, and metric name
    """
    items: list[dict] = []

    for series in result:
        metric_labels = series.get("metric", {})
        label = metric_labels.get(entity_field, "unknown")

        # Parse single value
        raw_value = series.get("value", [])
        if not raw_value or len(raw_value) < 2:
            continue

        str_val = str(raw_value[1])
        if str_val.lower() == "nan":
            continue

        try:
            value = float(str_val)
        except (ValueError, TypeError):
            continue

        rounded = round(value, 4)

        item: dict[str, Any] = {
            entity_field: label,
            metric_name: {
                "min": rounded,
                "max": rounded,
                "avg": rounded,
                "current": rounded,
                "samples": 1,
            },
        }

        # Include extra labels
        for k, v in metric_labels.items():
            if k != entity_field and k != "__name__":
                item[k] = v

        items.append(item)

    # Sort by current value descending, take top_n
    items.sort(key=lambda x: x[metric_name]["current"], reverse=True)
    total = len(items)

    return {
        "items": items[:top_n],
        "total_count": total,
        "showing": min(top_n, total),
        "metric": metric_name,
    }


def _compute_trend(values: list[float]) -> str:
    """
    Determine trend direction from time-series values.

    Compares first-half average to second-half average.
    >10% change = increasing/decreasing, else stable.
    """
    if len(values) < 2:
        return "stable"

    mid = len(values) // 2
    first_half = values[:mid]
    second_half = values[mid:]

    first_avg = sum(first_half) / len(first_half)
    second_avg = sum(second_half) / len(second_half)

    # Avoid division by zero
    denominator = max(abs(first_avg), 0.0001)
    pct_change = (second_avg - first_avg) / denominator

    if pct_change > 0.1:
        return "increasing"
    elif pct_change < -0.1:
        return "decreasing"
    return "stable"
