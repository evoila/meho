# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Connector Helper Functions (TASK-102)

Utility functions for common GCP operations.
"""

from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


def extract_zone_from_url(zone_url: str) -> str:
    """
    Extract zone name from a GCP zone URL.

    Args:
        zone_url: Full zone URL like
            "https://www.googleapis.com/compute/v1/projects/my-project/zones/us-central1-a"

    Returns:
        Zone name like "us-central1-a"
    """
    if not zone_url:
        return ""
    return zone_url.split("/")[-1]


def extract_region_from_zone(zone: str) -> str:
    """
    Extract region from zone name.

    Args:
        zone: Zone name like "us-central1-a"

    Returns:
        Region name like "us-central1"
    """
    if not zone:
        return ""
    # Zone format: {region}-{zone_letter}
    # e.g., us-central1-a -> us-central1
    parts = zone.rsplit("-", 1)
    return parts[0] if len(parts) == 2 else zone


def extract_name_from_url(url: str) -> str:
    """
    Extract resource name from a GCP resource URL.

    Args:
        url: Full resource URL

    Returns:
        Resource name (last segment of URL)
    """
    if not url:
        return ""
    return url.split("/")[-1]


def format_bytes(size_bytes: int) -> str:
    """
    Format bytes to human-readable string.

    Args:
        size_bytes: Size in bytes

    Returns:
        Formatted string like "100 GB"
    """
    if size_bytes is None:
        return "Unknown"

    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} EB"


def parse_machine_type(machine_type_url: str) -> dict[str, str]:
    """
    Parse machine type URL to extract details.

    Args:
        machine_type_url: URL like
            "zones/us-central1-a/machineTypes/n1-standard-4"

    Returns:
        Dict with zone;and machine_type
    """
    if not machine_type_url:
        return {"zone": "", "machine_type": ""}

    parts = machine_type_url.split("/")
    result = {"zone": "", "machine_type": ""}

    for i, part in enumerate(parts):
        if part == "zones" and i + 1 < len(parts):
            result["zone"] = parts[i + 1]
        elif part == "machineTypes" and i + 1 < len(parts):
            result["machine_type"] = parts[i + 1]

    return result


def get_status_color(status: str) -> str:
    """
    Get a color indicator for a status string.

    Args:
        status: Status string like "RUNNING", "STOPPED", etc.

    Returns:
        Color name for UI display
    """
    status_colors = {
        "RUNNING": "green",
        "STAGING": "yellow",
        "PROVISIONING": "yellow",
        "STOPPING": "yellow",
        "SUSPENDING": "yellow",
        "SUSPENDED": "orange",
        "STOPPED": "red",
        "TERMINATED": "red",
        "REPAIRING": "yellow",
        "RECONCILING": "yellow",
        "DEGRADED": "orange",
        "ERROR": "red",
        "READY": "green",
        "CREATING": "yellow",
        "DELETING": "red",
    }
    return status_colors.get(status.upper(), "gray")


def list_all_zones(zones_client: Any, project_id: str) -> list[str]:
    """
    List all zones in a project.

    Args:
        zones_client: Compute Engine ZonesClient
        project_id: GCP project ID

    Returns:
        List of zone names
    """
    try:
        from google.cloud import compute_v1

        request = compute_v1.ListZonesRequest(project=project_id)
        zones = zones_client.list(request=request)
        return [zone.name for zone in zones]
    except Exception as e:
        logger.error(f"Failed to list zones: {e}")
        return []


def list_all_regions(regions_client: Any, project_id: str) -> list[str]:
    """
    List all regions in a project.

    Args:
        regions_client: Compute Engine RegionsClient
        project_id: GCP project ID

    Returns:
        List of region names
    """
    try:
        from google.cloud import compute_v1

        request = compute_v1.ListRegionsRequest(project=project_id)
        regions = regions_client.list(request=request)
        return [region.name for region in regions]
    except Exception as e:
        logger.error(f"Failed to list regions: {e}")
        return []


def parse_labels(labels: dict[str, str] | None) -> dict[str, str]:
    """
    Parse GCP resource labels.

    Args:
        labels: Labels dict from GCP resource

    Returns:
        Cleaned labels dict
    """
    if not labels:
        return {}
    return dict(labels)


def format_timestamp(timestamp: str | None) -> str:
    """
    Format a GCP timestamp for display.

    Args:
        timestamp: ISO format timestamp string

    Returns:
        Formatted timestamp or empty string
    """
    if not timestamp:
        return ""
    # GCP timestamps are already in ISO format
    return timestamp


def build_filter_string(filters: dict[str, Any]) -> str | None:
    """
    Build a GCP API filter string from a dict.

    Args:
        filters: Dict of field->value filters

    Returns:
        Filter string for GCP API or None
    """
    if not filters:
        return None

    filter_parts = []
    for key, value in filters.items():
        if value is not None:
            if isinstance(value, bool):
                filter_parts.append(f"{key}={str(value).lower()}")
            elif isinstance(value, str):
                filter_parts.append(f'{key}="{value}"')
            else:
                filter_parts.append(f"{key}={value}")

    return " AND ".join(filter_parts) if filter_parts else None


# =========================================================================
# CLOUD BUILD HELPERS (Phase 49)
# =========================================================================


def format_build_duration(start_time: Any, finish_time: Any) -> float | None:
    """
    Compute duration in seconds between two protobuf Timestamps.

    Handles both protobuf Timestamp objects (with .seconds attribute)
    and datetime objects.

    Args:
        start_time: Protobuf Timestamp or datetime for build start
        finish_time: Protobuf Timestamp or datetime for build finish

    Returns:
        Duration in seconds as float, or None if either timestamp is missing/zero
    """
    if not start_time or not finish_time:
        return None

    try:
        # Try protobuf Timestamp .seconds attribute first
        start_seconds = getattr(start_time, "seconds", None)
        finish_seconds = getattr(finish_time, "seconds", None)

        if start_seconds is not None and finish_seconds is not None:
            if start_seconds == 0 or finish_seconds == 0:
                return None
            duration = finish_seconds - start_seconds
            # Add nanosecond precision if available
            start_nanos = getattr(start_time, "nanos", 0) or 0
            finish_nanos = getattr(finish_time, "nanos", 0) or 0
            duration += (finish_nanos - start_nanos) / 1e9
            return round(duration, 2)

        # Fall back to datetime subtraction
        if hasattr(start_time, "timestamp") and hasattr(finish_time, "timestamp"):
            return round(finish_time.timestamp() - start_time.timestamp(), 2)

        return None
    except Exception as e:
        logger.warning(f"Failed to compute build duration: {e}")
        return None


def format_protobuf_timestamp(ts: Any) -> str | None:
    """
    Safely convert a protobuf Timestamp to an ISO 8601 string.

    Handles None values, zero timestamps, and protobuf Timestamp objects.
    Uses .isoformat() if the Timestamp has a non-zero seconds field.

    Args:
        ts: Protobuf Timestamp object, datetime, or None

    Returns:
        ISO 8601 string or None
    """
    if ts is None:
        return None

    try:
        # Protobuf Timestamp with zero seconds means unset
        seconds = getattr(ts, "seconds", None)
        if seconds is not None and seconds == 0:
            # Check nanos too -- truly zero means unset
            nanos = getattr(ts, "nanos", 0) or 0
            if nanos == 0:
                return None

        # Try protobuf's built-in isoformat/RFC3339 conversion
        if hasattr(ts, "isoformat"):
            return ts.isoformat()

        # Try converting to datetime first (protobuf Timestamp -> datetime)
        if hasattr(ts, "ToDatetime"):
            dt = ts.ToDatetime()
            return dt.isoformat()

        return str(ts)
    except Exception as e:
        logger.warning(f"Failed to format protobuf timestamp: {e}")
        return None


def parse_build_status(status: Any) -> str:
    """
    Map Cloud Build Status enum to a string using .name property.

    Args:
        status: Cloud Build Status protobuf enum value

    Returns:
        Status string (e.g., "SUCCESS", "FAILURE", "WORKING") or "UNKNOWN"
    """
    if status is None:
        return "UNKNOWN"

    if hasattr(status, "name"):
        try:
            return status.name
        except Exception:  # noqa: S110 -- intentional silent exception handling
            pass

    # Fallback: try integer mapping
    try:
        return str(status)
    except Exception:
        return "UNKNOWN"
