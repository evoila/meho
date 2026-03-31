# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Utility functions for the Connectors module.
"""

from urllib.parse import urlparse


def extract_target_host(base_url: str) -> str:
    """
    Extract hostname from a base_url for topology matching.

    Used when registering connectors as topology entities to enable
    automatic correlation with discovered infrastructure (e.g., K8s Ingresses).

    Args:
        base_url: The connector's base URL (e.g., "https://api.myapp.com/v1")

    Returns:
        The hostname (e.g., "api.myapp.com") or the original URL if parsing fails

    Examples:
        >>> extract_target_host("https://api.myapp.com/v1")
        'api.myapp.com'
        >>> extract_target_host("http://192.168.1.10:8080/api")
        '192.168.1.10'
        >>> extract_target_host("vcenter.example.com")
        'vcenter.example.com'
    """
    # Handle URLs without scheme (common for typed connectors like VMware)
    if not base_url.startswith(("http://", "https://")):
        # Try parsing as-is first
        parsed = urlparse(base_url)
        if parsed.hostname:
            return parsed.hostname
        # If no scheme, it might just be a hostname
        # Try adding a scheme and parsing again
        parsed = urlparse(f"https://{base_url}")
        if parsed.hostname:
            return parsed.hostname
        # Fall back to returning as-is
        return base_url

    parsed = urlparse(base_url)
    return parsed.hostname or base_url
