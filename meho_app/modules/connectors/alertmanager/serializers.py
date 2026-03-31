# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Alertmanager Response Serializers.

Transform raw Alertmanager v2 API JSON responses into clean, structured data
for the agent. Handles alert listing with grouping, alert detail progressive
disclosure, silence listing, cluster status, and receivers.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def _parse_duration_string(duration_str: str) -> timedelta:
    """
    Parse a human-friendly duration string into a timedelta.

    Supports: h (hours), m (minutes), d (days), s (seconds).
    Examples: "2h", "30m", "1d", "4h30m", "1d2h", "90s".

    Args:
        duration_str: Duration string to parse.

    Returns:
        timedelta representing the duration.

    Raises:
        ValueError: If the duration string cannot be parsed.
    """
    if not duration_str:
        raise ValueError("Empty duration string")

    pattern = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")
    match = pattern.fullmatch(duration_str.strip())

    if not match or not any(match.groups()):
        raise ValueError(f"Cannot parse duration string: '{duration_str}'")

    days = int(match.group(1) or 0)
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    seconds = int(match.group(4) or 0)

    result = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    if result.total_seconds() <= 0:
        raise ValueError(f"Duration must be positive: '{duration_str}'")

    return result


def _format_duration(start_iso: str, end_iso: str | None = None) -> str:
    """
    Format ISO8601 timestamps into a human-readable duration string.

    If end_iso is None, computes duration from start_iso to now (UTC).

    Args:
        start_iso: ISO8601 start timestamp.
        end_iso: Optional ISO8601 end timestamp. If None, uses current UTC time.

    Returns:
        Human-readable duration string like "2h 15m", "3d 1h", "45m".
    """
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        if end_iso:
            end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        else:
            end = datetime.now(UTC)

        delta = end - start
        total_seconds = int(delta.total_seconds())

        if total_seconds < 0:
            return "0s"

        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60

        parts: list[str] = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")

        return " ".join(parts) if parts else "< 1m"
    except (ValueError, TypeError):
        return "unknown"


def _format_time_remaining(ends_at_iso: str) -> str:
    """
    Format time remaining until a silence expires.

    Args:
        ends_at_iso: ISO8601 end timestamp.

    Returns:
        Human-readable remaining time like "1h 30m", or "expired" if in the past.
    """
    try:
        ends_at = datetime.fromisoformat(ends_at_iso.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        delta = ends_at - now
        total_seconds = int(delta.total_seconds())

        if total_seconds <= 0:
            return "expired"

        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60

        parts: list[str] = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")

        return " ".join(parts) if parts else "< 1m"
    except (ValueError, TypeError):
        return "unknown"


# =============================================================================
# ALERT SERIALIZERS
# =============================================================================


def serialize_alerts(alerts: list[dict[str, Any]], group_by_alertname: bool = True) -> dict:
    """
    Transform Alertmanager v2 alerts into grouped, agent-consumable output.

    Groups alerts by alertname. Per group: alertname, instance count.
    Per alert instance: target, state, duration, severity, summary.
    Summary header: total firing, silenced, inhibited counts.

    Args:
        alerts: List of alert dicts from Alertmanager v2 /api/v2/alerts.
        group_by_alertname: Whether to group alerts by alertname (default True).

    Returns:
        {
            "summary": {"total": N, "firing": X, "silenced": Y, "inhibited": Z},
            "groups": [{"alertname": str, "count": int, "alerts": [...]}]
        }
    """
    total = len(alerts)
    firing = 0
    silenced = 0
    inhibited = 0

    # Classify states
    for alert in alerts:
        status = alert.get("status", {})
        state = status.get("state", "active")
        if state == "suppressed":
            # Suppressed can mean silenced or inhibited
            silenced_by = status.get("silencedBy", [])
            inhibited_by = status.get("inhibitedBy", [])
            if silenced_by:
                silenced += 1
            elif inhibited_by:
                inhibited += 1
            else:
                silenced += 1  # Default suppressed to silenced
        elif state == "active":
            firing += 1
        elif state == "unprocessed":
            firing += 1  # Treat unprocessed as firing

    summary = {
        "total": total,
        "firing": firing,
        "silenced": silenced,
        "inhibited": inhibited,
    }

    if not group_by_alertname:
        flat_alerts = [_serialize_alert_instance(a) for a in alerts]
        return {"summary": summary, "alerts": flat_alerts}

    # Group by alertname
    groups_map: dict[str, list[dict[str, Any]]] = {}
    for alert in alerts:
        labels = alert.get("labels", {})
        alertname = labels.get("alertname", "unknown")
        if alertname not in groups_map:
            groups_map[alertname] = []
        groups_map[alertname].append(alert)

    groups = []
    for alertname, group_alerts in sorted(groups_map.items()):
        instances = [_serialize_alert_instance(a) for a in group_alerts]
        groups.append(
            {
                "alertname": alertname,
                "count": len(instances),
                "alerts": instances,
            }
        )

    return {"summary": summary, "groups": groups}


def _serialize_alert_instance(alert: dict[str, Any]) -> dict:
    """Serialize a single alert instance to compact format."""
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    status = alert.get("status", {})

    # Determine target: prefer instance, then pod, then service, then job
    target = (
        labels.get("instance")
        or labels.get("pod")
        or labels.get("service")
        or labels.get("job")
        or "unknown"
    )

    # Determine state
    state = status.get("state", "active")
    if state == "suppressed":
        silenced_by = status.get("silencedBy", [])
        inhibited_by = status.get("inhibitedBy", [])
        if silenced_by:
            state = "silenced"
        elif inhibited_by:
            state = "inhibited"

    # Duration since startsAt
    starts_at = alert.get("startsAt", "")
    duration = _format_duration(starts_at) if starts_at else "unknown"

    severity = labels.get("severity", "")
    summary = annotations.get("summary", annotations.get("description", ""))

    result: dict[str, Any] = {
        "target": target,
        "state": state,
        "duration": duration,
        "severity": severity,
        "summary": summary,
        "fingerprint": alert.get("fingerprint", ""),
    }

    return result


def serialize_alert_detail(alert: dict[str, Any]) -> dict:
    """
    Full single-alert detail for progressive disclosure deep-dive.

    Returns all labels, all annotations (highlighting runbook_url, dashboard_url),
    generatorURL, silenced_by IDs, inhibited_by IDs, startsAt, fingerprint.

    Args:
        alert: Single alert dict from Alertmanager v2 /api/v2/alerts.

    Returns:
        Full alert detail dict.
    """
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    status = alert.get("status", {})

    # State classification
    state = status.get("state", "active")
    silenced_by = status.get("silencedBy", [])
    inhibited_by = status.get("inhibitedBy", [])

    if state == "suppressed":
        if silenced_by:
            state = "silenced"
        elif inhibited_by:
            state = "inhibited"

    # Duration
    starts_at = alert.get("startsAt", "")
    ends_at = alert.get("endsAt", "")
    duration = _format_duration(starts_at) if starts_at else "unknown"

    result: dict[str, Any] = {
        "fingerprint": alert.get("fingerprint", ""),
        "state": state,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "duration": duration,
        "generator_url": alert.get("generatorURL", ""),
        "labels": labels,
        "annotations": annotations,
    }

    # Highlight important annotations
    if annotations.get("runbook_url"):
        result["runbook_url"] = annotations["runbook_url"]
    if annotations.get("dashboard_url"):
        result["dashboard_url"] = annotations["dashboard_url"]

    # Silence/inhibition references
    if silenced_by:
        result["silenced_by"] = silenced_by
    if inhibited_by:
        result["inhibited_by"] = inhibited_by

    return result


# =============================================================================
# SILENCE SERIALIZERS
# =============================================================================


def serialize_silences(silences: list[dict[str, Any]]) -> dict:
    """
    Transform Alertmanager v2 silences into agent-consumable output.

    Summary header with active/pending/expired counts. Per silence:
    ID (truncated), matchers as compact string, state, created_by,
    time remaining, comment (truncated to 80 chars).

    Args:
        silences: List of silence dicts from Alertmanager v2 /api/v2/silences.

    Returns:
        {
            "summary": {"total": N, "active": X, "pending": Y, "expired": Z},
            "silences": [{id, matchers, state, created_by, time_remaining, comment}]
        }
    """
    active = 0
    pending = 0
    expired = 0

    for silence in silences:
        state = silence.get("status", {}).get("state", "expired")
        if state == "active":
            active += 1
        elif state == "pending":
            pending += 1
        else:
            expired += 1

    summary = {
        "total": len(silences),
        "active": active,
        "pending": pending,
        "expired": expired,
    }

    serialized = []
    for silence in silences:
        silence_id = silence.get("id", "")
        # Truncate UUID to first 8 chars for display
        id_display = silence_id[:8] if len(silence_id) > 8 else silence_id

        # Build compact matchers string
        matchers = silence.get("matchers", [])
        matcher_parts = []
        for m in matchers:
            name = m.get("name", "")
            value = m.get("value", "")
            is_regex = m.get("isRegex", False)
            is_equal = m.get("isEqual", True)
            op = ("=~" if is_equal else "!~") if is_regex else "=" if is_equal else "!="
            matcher_parts.append(f"{name}{op}{value}")
        matchers_str = ", ".join(matcher_parts)

        state = silence.get("status", {}).get("state", "expired")
        created_by = silence.get("createdBy", "")
        comment = silence.get("comment", "")
        # Truncate comment to 80 chars
        if len(comment) > 80:
            comment = comment[:77] + "..."

        # Time remaining
        ends_at = silence.get("endsAt", "")
        time_remaining = _format_time_remaining(ends_at) if ends_at else "unknown"

        serialized.append(
            {
                "id": id_display,
                "full_id": silence_id,
                "matchers": matchers_str,
                "state": state,
                "created_by": created_by,
                "time_remaining": time_remaining,
                "comment": comment,
            }
        )

    return {"summary": summary, "silences": serialized}


# =============================================================================
# STATUS SERIALIZERS
# =============================================================================


def serialize_cluster_status(status: dict[str, Any]) -> dict:
    """
    Transform Alertmanager v2 status response into cluster health output.

    Args:
        status: Status dict from Alertmanager v2 /api/v2/status.

    Returns:
        {
            "cluster": {"name": str, "status": str, "peer_count": int},
            "peers": [{"name": str, "address": str, "state": str}]
        }
    """
    cluster = status.get("cluster", {})
    cluster_name = cluster.get("name", "unknown")
    cluster_status = cluster.get("status", "unknown")
    peers_raw = cluster.get("peers", [])

    peers = []
    for peer in peers_raw:
        peers.append(
            {
                "name": peer.get("name", "unknown"),
                "address": peer.get("address", "unknown"),
            }
        )

    result = {
        "cluster": {
            "name": cluster_name,
            "status": cluster_status,
            "peer_count": len(peers),
        },
        "peers": peers,
    }

    # Include version info if available
    version_info = status.get("versionInfo", {})
    if version_info:
        result["version"] = version_info.get("version", "unknown")

    return result


def serialize_receivers(receivers: list[dict[str, Any]]) -> dict:
    """
    Transform Alertmanager v2 receivers response into receiver listing.

    Args:
        receivers: List of receiver dicts from Alertmanager v2 /api/v2/receivers.

    Returns:
        {"receivers": [{"name": str}]}
    """
    serialized = []
    for receiver in receivers:
        serialized.append(
            {
                "name": receiver.get("name", "unknown"),
            }
        )

    return {"total": len(serialized), "receivers": serialized}
