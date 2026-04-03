# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes Connector Helpers

Utility functions for the Kubernetes connector.
"""

from typing import Any


def get_pod_status_reason(pod: Any) -> str:  # NOSONAR (cognitive complexity)
    """
    Get the reason for the current pod status.

    Similar to how kubectl shows pod status reason.
    """
    status = pod.status
    if not status:
        return "Unknown"

    # Check container statuses for waiting/terminated reasons
    if status.container_statuses:
        for cs in status.container_statuses:
            if cs.state:
                if cs.state.waiting and cs.state.waiting.reason:
                    return str(cs.state.waiting.reason)
                if cs.state.terminated and cs.state.terminated.reason:
                    return str(cs.state.terminated.reason)

    # Check init container statuses
    if status.init_container_statuses:
        for cs in status.init_container_statuses:
            if cs.state:
                if cs.state.waiting and cs.state.waiting.reason:
                    return f"Init:{cs.state.waiting.reason}"
                if cs.state.terminated and cs.state.terminated.reason:
                    return f"Init:{cs.state.terminated.reason}"

    # Check conditions for reason
    if status.conditions:
        for cond in status.conditions:
            if cond.type == "PodScheduled" and cond.status == "False":
                return cond.reason or "Scheduling"

    return status.phase or "Unknown"


def get_container_restart_count(pod: Any) -> int:
    """Get total restart count across all containers in a pod."""
    total = 0
    if pod.status and pod.status.container_statuses:
        for cs in pod.status.container_statuses:
            total += cs.restart_count or 0
    return total


def is_pod_ready(pod: Any) -> bool:
    """Check if a pod is ready (all containers running and ready)."""
    if not pod.status or not pod.status.conditions:
        return False

    for cond in pod.status.conditions:
        if cond.type == "Ready":
            return bool(cond.status == "True")
    return False


def get_node_status(node: Any) -> str:
    """Get the overall status of a node (Ready, NotReady, etc.)."""
    if not node.status or not node.status.conditions:
        return "Unknown"

    for cond in node.status.conditions:
        if cond.type == "Ready":
            if cond.status == "True":
                return "Ready"
            elif cond.status == "False":
                return "NotReady"
            else:
                return "Unknown"
    return "Unknown"


def format_resource_quantity(quantity: str | None) -> dict[str, Any]:
    """
    Parse a Kubernetes resource quantity string.

    Examples:
        "100m" -> {"value": 100, "unit": "m", "description": "100 millicores"}
        "1Gi" -> {"value": 1, "unit": "Gi", "description": "1 gibibyte"}
    """
    if not quantity:
        return {"value": 0, "unit": "", "raw": quantity}

    # CPU quantities
    if quantity.endswith("m"):
        return {
            "value": int(quantity[:-1]),
            "unit": "millicores",
            "raw": quantity,
        }
    if quantity.endswith("n"):
        return {
            "value": int(quantity[:-1]),
            "unit": "nanocores",
            "raw": quantity,
        }

    # Memory quantities
    units = {
        "Ki": "kibibytes",
        "Mi": "mebibytes",
        "Gi": "gibibytes",
        "Ti": "tebibytes",
        "Pi": "pebibytes",
        "Ei": "exbibytes",
        "K": "kilobytes",
        "M": "megabytes",
        "G": "gigabytes",
        "T": "terabytes",
        "P": "petabytes",
        "E": "exabytes",
    }

    for suffix, unit_name in units.items():
        if quantity.endswith(suffix):
            return {
                "value": int(quantity[: -len(suffix)]),
                "unit": unit_name,
                "raw": quantity,
            }

    # Plain number (bytes or cores)
    try:
        return {
            "value": int(quantity),
            "unit": "bytes" if int(quantity) > 1000 else "cores",
            "raw": quantity,
        }
    except ValueError:
        return {"value": 0, "unit": "unknown", "raw": quantity}


def build_label_selector(labels: dict[str, str]) -> str:
    """Build a label selector string from a dict of labels."""
    if not labels:
        return ""
    return ",".join(f"{k}={v}" for k, v in labels.items())


def build_field_selector(fields: dict[str, str]) -> str:
    """Build a field selector string from a dict of fields."""
    if not fields:
        return ""
    return ",".join(f"{k}={v}" for k, v in fields.items())
