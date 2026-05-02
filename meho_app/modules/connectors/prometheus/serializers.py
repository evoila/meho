# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus Response Serializers.

Transform raw Prometheus API responses into clean, structured data
for the agent. Handles targets, alerts, rules, and metrics metadata.
"""

import re
from typing import Any

_IP_PATTERN = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def serialize_targets(raw_targets: list) -> dict:  # NOSONAR (cognitive complexity)
    """
    Transform Prometheus /api/v1/targets active targets into clean list.

    Extracts job, instance, health, scrape URL, and Kubernetes labels
    (namespace, pod, node) when discovered via K8s service discovery.

    Also extracts IP address from instance field for topology entity
    resolution (enables SAME_AS edges to K8s/VMware entities).
    """
    targets = []
    for target in raw_targets:
        labels = target.get("labels", {})
        discovered = target.get("discoveredLabels", {})

        instance = labels.get("instance", "unknown")

        item: dict[str, Any] = {
            "job": labels.get("job", "unknown"),
            "instance": instance,
            "health": target.get("health", "unknown"),
            "scrape_url": target.get("scrapeUrl", ""),
            "labels_url": target.get("scrapeUrl", ""),
            "last_scrape": target.get("lastScrape", ""),
            "last_scrape_duration": target.get("lastScrapeDuration", 0),
            "last_error": target.get("lastError", ""),
        }

        # Extract IP from instance (e.g., "10.0.0.5:9090" -> "10.0.0.5")
        # Used by topology entity resolution for SAME_AS edges
        ip_candidate = instance.split(":")[0] if ":" in instance else instance
        if _IP_PATTERN.match(ip_candidate):
            item["ip_address"] = ip_candidate

        # Extract Kubernetes labels if present
        k8s_labels: dict[str, str] = {}
        for key in (
            "__meta_kubernetes_namespace",
            "__meta_kubernetes_pod_name",
            "__meta_kubernetes_node_name",
        ):
            short_key = key.replace("__meta_kubernetes_", "").replace("_name", "")
            value = discovered.get(key)
            if value:
                k8s_labels[short_key] = value

        # Also check standard labels for K8s metadata
        for key in ("namespace", "pod", "node"):
            if key not in k8s_labels and key in labels:
                k8s_labels[key] = labels[key]

        # Promote K8s labels to top-level for flat extraction schema access
        if k8s_labels:
            item["kubernetes"] = k8s_labels
            for key, value in k8s_labels.items():
                item[key] = value

        targets.append(item)

    # Sort by health (down first, then up, then unknown)
    health_order = {"down": 0, "unknown": 1, "up": 2}
    targets.sort(key=lambda t: health_order.get(t["health"], 1))

    return {
        "targets": targets,
        "total_count": len(targets),
        "healthy": sum(1 for t in targets if t["health"] == "up"),
        "unhealthy": sum(1 for t in targets if t["health"] == "down"),
    }


def serialize_alerts(raw_alerts: list) -> dict:
    """
    Transform Prometheus /api/v1/alerts response into clean list.
    """
    alerts = []
    for alert in raw_alerts:
        alerts.append(
            {
                "name": alert.get("labels", {}).get("alertname", "unknown"),
                "state": alert.get("state", "unknown"),
                "labels": alert.get("labels", {}),
                "annotations": alert.get("annotations", {}),
                "active_at": alert.get("activeAt", ""),
                "value": alert.get("value", ""),
            }
        )

    # Sort by state: firing first, then pending, then inactive
    state_order = {"firing": 0, "pending": 1, "inactive": 2}
    alerts.sort(key=lambda a: state_order.get(a["state"], 2))

    return {
        "alerts": alerts,
        "total_count": len(alerts),
        "firing": sum(1 for a in alerts if a["state"] == "firing"),
        "pending": sum(1 for a in alerts if a["state"] == "pending"),
    }


def serialize_rules(raw_rules: dict, rule_type: str | None = None) -> dict:
    """
    Transform Prometheus /api/v1/rules response into clean list.

    Args:
        raw_rules: Raw rules response data
        rule_type: Optional filter: 'alerting' or 'recording'
    """
    rules = []
    groups = raw_rules.get("groups", [])

    for group in groups:
        group_name = group.get("name", "unknown")
        group_file = group.get("file", "")

        for rule in group.get("rules", []):
            rtype = rule.get("type", "unknown")

            # Filter by type if specified
            if rule_type and rtype != rule_type:
                continue

            item: dict[str, Any] = {
                "name": rule.get("name", "unknown"),
                "type": rtype,
                "query": rule.get("query", ""),
                "group": group_name,
                "file": group_file,
                "health": rule.get("health", "unknown"),
                "state": rule.get("state", ""),
            }

            # Alerting rules have additional fields
            if rtype == "alerting":
                item["duration"] = rule.get("duration", 0)
                item["labels"] = rule.get("labels", {})
                item["annotations"] = rule.get("annotations", {})
                item["active_alerts"] = len(rule.get("alerts", []))

            rules.append(item)

    return {
        "rules": rules,
        "total_count": len(rules),
        "alerting": sum(1 for r in rules if r["type"] == "alerting"),
        "recording": sum(1 for r in rules if r["type"] == "recording"),
    }


def serialize_metrics_metadata(
    raw_metadata: dict,
    search: str | None = None,
) -> dict:
    """
    Transform Prometheus /api/v1/metadata response into grouped metrics.

    Groups metrics by type (counter, gauge, histogram, summary).
    Limits to 100 per type to prevent context overflow.

    Args:
        raw_metadata: Raw metadata dict (metric_name -> list of metadata entries)
        search: Optional substring filter for metric names
    """
    MAX_PER_TYPE = 100

    # Group by type
    grouped: dict[str, list] = {
        "counter": [],
        "gauge": [],
        "histogram": [],
        "summary": [],
        "unknown": [],
    }

    for metric_name, entries in raw_metadata.items():
        # Apply search filter
        if search and search.lower() not in metric_name.lower():
            continue

        if not entries:
            continue

        # Use first entry for type and help text
        entry = entries[0]
        metric_type = entry.get("type", "unknown")
        help_text = entry.get("help", "")

        # Normalize type
        if metric_type not in grouped:
            metric_type = "unknown"

        grouped[metric_type].append(
            {
                "name": metric_name,
                "type": metric_type,
                "help": help_text,
                "unit": entry.get("unit", ""),
            }
        )

    # Sort each group alphabetically and limit
    for type_name in grouped:
        grouped[type_name].sort(key=lambda m: m["name"])

    # Count totals before limiting
    total_by_type = {t: len(metrics) for t, metrics in grouped.items()}
    total_count = sum(total_by_type.values())

    # Apply limit
    for type_name in grouped:
        grouped[type_name] = grouped[type_name][:MAX_PER_TYPE]

    # Remove empty groups
    grouped = {k: v for k, v in grouped.items() if v}

    return {
        "metrics_by_type": grouped,
        "total_count": total_count,
        "total_by_type": total_by_type,
        "search": search,
        "limited_to": MAX_PER_TYPE,
    }
