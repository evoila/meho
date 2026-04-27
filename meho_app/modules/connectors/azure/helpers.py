# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Connector Helper Functions (Phase 92).

Utility functions for common Azure resource operations.
"""

from datetime import datetime
from typing import Any


def _extract_resource_group(resource_id: str) -> str:
    """Extract resource group name from an Azure ARM resource ID.

    ARM resource ID format:
    /subscriptions/{sub}/resourceGroups/{rg}/providers/...

    Args:
        resource_id: Full ARM resource ID.

    Returns:
        Resource group name, or empty string if not found.
    """
    if not resource_id:
        return ""
    parts = resource_id.split("/")
    for i, part in enumerate(parts):
        if part.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _build_resource_uri(
    subscription_id: str,
    resource_group: str,
    provider: str,
    resource_type: str,
    resource_name: str,
) -> str:
    """Build full ARM resource URI for Azure Monitor metrics.

    Args:
        subscription_id: Azure subscription ID.
        resource_group: Resource group name.
        provider: Resource provider (e.g. "Microsoft.Compute").
        resource_type: Resource type (e.g. "virtualMachines").
        resource_name: Name of the resource.

    Returns:
        Full ARM resource URI.
    """
    return (
        f"/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/{provider}/{resource_type}/{resource_name}"
    )


def _extract_os_type(vm: Any) -> str | None:
    """Extract OS type from a VM's storage profile.

    Args:
        vm: Azure VM SDK object.

    Returns:
        "Linux", "Windows", or None.
    """
    try:
        os_type = vm.storage_profile.os_disk.os_type
        if os_type is not None:
            # os_type may be an enum or a string
            return str(os_type.value) if hasattr(os_type, "value") else str(os_type)
    except (AttributeError, TypeError):
        pass
    return None


def _format_azure_timestamp(dt: datetime | None) -> str | None:
    """Convert a datetime to ISO 8601 string.

    Args:
        dt: Datetime object or None.

    Returns:
        ISO 8601 string or None.
    """
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except (AttributeError, TypeError):
        return None


def _safe_tags(tags: dict[str, str] | None) -> dict[str, str]:
    """Safely convert tags to a dict.

    Args:
        tags: Tags dict from Azure resource, or None.

    Returns:
        Dict of tags, or empty dict.
    """
    if tags is None:
        return {}
    return dict(tags)


def _safe_list(items: Any) -> list:
    """Safely convert an iterable to a list.

    Args:
        items: Iterable or None.

    Returns:
        List, or empty list if None.
    """
    if items is None:
        return []
    return list(items)
