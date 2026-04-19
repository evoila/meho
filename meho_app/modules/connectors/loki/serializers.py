# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Loki Response Serializers.

Transform raw Loki API responses into clean, structured data
for the agent. Handles log streams, log context, volume, and patterns.
"""

from datetime import UTC, datetime
from typing import Any


def _ns_to_iso(ns_str: str) -> str:
    """Convert Loki nanosecond timestamp string to ISO8601."""
    try:
        ns = int(ns_str)
        seconds = ns / 1_000_000_000
        dt = datetime.fromtimestamp(seconds, tz=UTC)
        return dt.isoformat()
    except (ValueError, TypeError, OSError):
        return ns_str


def _extract_severity(stream_labels: dict[str, Any]) -> str:
    """Extract severity/level from stream labels, trying common label names."""
    for label_name in ("level", "severity", "detected_level"):
        value = stream_labels.get(label_name)
        if value:
            return str(value).lower()
    return "unknown"


def _extract_source(stream_labels: dict[str, Any]) -> str:
    """Extract human-readable source identifier from stream labels."""
    namespace = stream_labels.get("namespace", "")
    pod = stream_labels.get("pod", "")
    if namespace and pod:
        return f"{namespace}/{pod}"

    job = stream_labels.get("job", "")
    instance = stream_labels.get("instance", "")
    if job and instance:
        return f"{job}/{instance}"

    # Fallback: use service_name or first meaningful label
    service: Any = stream_labels.get("service_name", "")
    if service:
        return str(service)

    container: Any = stream_labels.get("container", "")
    if container:
        return str(container)

    return "unknown"


def serialize_log_streams(data: dict, limit: int = 100) -> dict:  # NOSONAR (cognitive complexity)
    """
    Transform Loki query_range response into structured log output.

    Extracts streams from data["data"]["result"]. Each stream has
    stream labels and values (list of [timestamp_ns, line]).
    Merges all streams, sorts by timestamp descending (newest first),
    and limits to the specified number of lines.

    Returns:
        {
            "summary": {total_lines, streams_count, time_range, severity_breakdown, truncated},
            "logs": [{timestamp, severity, source, message}, ...]
        }
    """
    result = data.get("data", {}).get("result", [])

    # Collect all log lines across streams
    all_lines: list[dict[str, Any]] = []
    severity_counts: dict[str, int] = {}
    min_ts: str | None = None
    max_ts: str | None = None

    for stream in result:
        stream_labels = stream.get("stream", {})
        severity = _extract_severity(stream_labels)
        source = _extract_source(stream_labels)
        values = stream.get("values", [])

        for ts_ns, line in values:
            iso_ts = _ns_to_iso(ts_ns)
            all_lines.append(
                {
                    "timestamp": iso_ts,
                    "severity": severity,
                    "source": source,
                    "message": line,
                    "_sort_key": int(ts_ns) if ts_ns.isdigit() else 0,
                }
            )

            # Track severity counts
            severity_counts[severity] = severity_counts.get(severity, 0) + 1

            # Track time range
            if min_ts is None or ts_ns < min_ts:
                min_ts = ts_ns
            if max_ts is None or ts_ns > max_ts:
                max_ts = ts_ns

    total_lines = len(all_lines)

    # Sort by timestamp descending (newest first)
    all_lines.sort(key=lambda x: x["_sort_key"], reverse=True)

    # Apply limit
    truncated = total_lines > limit
    limited_lines = all_lines[:limit]

    # Remove sort key from output
    logs = [{k: v for k, v in line.items() if k != "_sort_key"} for line in limited_lines]

    summary = {
        "total_lines": total_lines,
        "streams_count": len(result),
        "time_range": {
            "start": _ns_to_iso(min_ts) if min_ts else None,
            "end": _ns_to_iso(max_ts) if max_ts else None,
        },
        "severity_breakdown": severity_counts,
        "truncated": truncated,
    }
    if truncated:
        summary["showing"] = limit

    return {"summary": summary, "logs": logs}


def serialize_log_context(
    data: dict,
    center_timestamp: str,
    before_lines: int = 20,
    after_lines: int = 20,
) -> dict:
    """
    Transform Loki query_range response into before/after context sections.

    Splits log lines relative to the center timestamp.

    Returns:
        {
            "before": [{timestamp, severity, source, message}, ...],
            "center_timestamp": str,
            "after": [{timestamp, severity, source, message}, ...]
        }
    """
    result = data.get("data", {}).get("result", [])

    # Parse center timestamp to nanoseconds for comparison
    center_ns = _parse_timestamp_to_ns(center_timestamp)

    # Collect all log lines
    all_lines: list[dict[str, Any]] = []
    for stream in result:
        stream_labels = stream.get("stream", {})
        severity = _extract_severity(stream_labels)
        source = _extract_source(stream_labels)
        values = stream.get("values", [])

        for ts_ns, line in values:
            all_lines.append(
                {
                    "timestamp": _ns_to_iso(ts_ns),
                    "severity": severity,
                    "source": source,
                    "message": line,
                    "_ns": int(ts_ns) if ts_ns.isdigit() else 0,
                }
            )

    # Sort by timestamp ascending (chronological order)
    all_lines.sort(key=lambda x: x["_ns"])

    # Split into before and after
    before = []
    after = []
    for line in all_lines:
        if line["_ns"] < center_ns:
            before.append(line)
        else:
            after.append(line)

    # Take the last N before-lines and first N after-lines
    before = before[-before_lines:] if before_lines else []
    after = after[:after_lines] if after_lines else []

    # Remove internal sort keys
    def _clean(lines: list) -> list:
        return [{k: v for k, v in line.items() if k != "_ns"} for line in lines]

    return {
        "before": _clean(before),
        "center_timestamp": center_timestamp,
        "after": _clean(after),
    }


def serialize_log_volume(data: dict) -> dict:
    """
    Transform Loki metric query response (count_over_time) into volume stats.

    Returns:
        {
            "total_logs": int,
            "buckets": [{"time": iso_ts, "count": int}, ...],
            "peak_time": iso_ts,
            "peak_count": int
        }
    """
    result = data.get("data", {}).get("result", [])

    # Aggregate values across all series
    bucket_map: dict[str, float] = {}
    for series in result:
        values = series.get("values", [])
        for ts, val in values:
            ts_str = str(ts)
            count = float(val)
            bucket_map[ts_str] = bucket_map.get(ts_str, 0) + count

    # Build sorted bucket list
    buckets = []
    total = 0.0
    peak_time = None
    peak_count = 0.0

    for ts_str in sorted(bucket_map.keys()):
        count = bucket_map[ts_str]
        total += count

        # Convert epoch seconds to ISO
        try:
            ts_float = float(ts_str)
            iso_ts = datetime.fromtimestamp(ts_float, tz=UTC).isoformat()
        except (ValueError, TypeError, OSError):
            iso_ts = ts_str

        buckets.append({"time": iso_ts, "count": int(count)})

        if count > peak_count:
            peak_count = count
            peak_time = iso_ts

    return {
        "total_logs": int(total),
        "buckets": buckets,
        "peak_time": peak_time,
        "peak_count": int(peak_count),
    }


def serialize_log_patterns(data: dict) -> dict:
    """
    Transform Loki patterns API response into structured pattern list.

    Each pattern has a pattern string, count, and optional sample line.
    Sorted by count descending (most frequent first).

    Returns:
        {
            "patterns": [{"pattern": str, "count": int, "sample": str}, ...],
            "total_patterns": int
        }
    """
    # The Loki patterns API returns a list of pattern objects
    # Response format may vary by Loki version
    raw_patterns = data if isinstance(data, list) else data.get("data", [])

    if isinstance(raw_patterns, dict):
        # Some versions nest under "data"
        raw_patterns = raw_patterns.get("data", [])

    patterns = []
    for entry in raw_patterns:
        pattern_text = entry.get("pattern", "")
        count = entry.get("count", 0)
        # Sample line may be in "sample" or "samples" list
        sample = entry.get("sample", "")
        if not sample:
            samples = entry.get("samples", [])
            if samples:
                sample = samples[0] if isinstance(samples[0], str) else str(samples[0])

        patterns.append(
            {
                "pattern": pattern_text,
                "count": int(count),
                "sample": sample,
            }
        )

    # Sort by count descending
    patterns.sort(key=lambda p: p["count"], reverse=True)

    return {
        "patterns": patterns,
        "total_patterns": len(patterns),
    }


def _parse_timestamp_to_ns(timestamp: str) -> int:
    """
    Parse a timestamp string to nanoseconds.

    Supports ISO8601 format and Unix nanosecond strings.
    """
    # If it looks like a nanosecond timestamp already
    if timestamp.isdigit() and len(timestamp) > 15:
        return int(timestamp)

    # Try ISO8601 parsing
    try:
        # Handle Z suffix and timezone
        ts = timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1_000_000_000)
    except (ValueError, TypeError):
        pass

    # Try as epoch seconds
    try:
        return int(float(timestamp) * 1_000_000_000)
    except (ValueError, TypeError):
        return 0
