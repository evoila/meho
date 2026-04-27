# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Danger Level Assignment

TASK-76: Automatic danger level classification for API endpoints.

Assigns danger levels based on HTTP method:
- GET, HEAD, OPTIONS → safe (auto-approve)
- POST, PUT, PATCH → dangerous (requires approval)
- DELETE → critical (requires approval + confirmation)
"""

from typing import Literal

# Type alias for danger levels
DangerLevel = Literal["safe", "caution", "dangerous", "critical"]


def assign_danger_level(
    method: str, path: str = "", override: DangerLevel | None = None
) -> tuple[DangerLevel, bool]:
    """
    Automatically assign danger level and approval requirement.

    Args:
        method: HTTP method (GET, POST, etc.)
        path: Endpoint path (for context-based rules)
        override: Manual override (if set by admin)

    Returns:
        Tuple of (danger_level, requires_approval)

    Examples:
        >>> assign_danger_level("GET", "/api/vm")
        ("safe", False)
        >>> assign_danger_level("DELETE", "/api/vm/123")
        ("critical", True)
        >>> assign_danger_level("POST", "/api/auth/login")
        ("dangerous", True)  # Can be overridden to "safe"
    """
    # Use override if provided
    if override:
        requires_approval = override in ("dangerous", "critical")
        return (override, requires_approval)

    method = method.upper()

    # Safe methods (read-only, no side effects)
    if method in ("GET", "HEAD", "OPTIONS"):
        return ("safe", False)

    # Critical methods (destructive, irreversible)
    if method == "DELETE":
        return ("critical", True)

    # Dangerous methods (write operations, may have side effects)
    if method in ("POST", "PUT", "PATCH"):
        return ("dangerous", True)

    # Unknown method (be cautious)
    return ("dangerous", True)


def should_require_approval(danger_level: DangerLevel) -> bool:
    """
    Check if endpoint danger level requires user approval.

    Args:
        danger_level: Endpoint danger level

    Returns:
        True if requires user approval, False if auto-approved
    """
    return danger_level in ("dangerous", "critical")


def should_auto_approve(danger_level: DangerLevel) -> bool:
    """
    Check if endpoint can be auto-approved (no user interaction needed).

    Args:
        danger_level: Endpoint danger level

    Returns:
        True if safe to auto-approve, False if needs user approval
    """
    return danger_level in ("safe", "caution")


def get_impact_message(method: str, path: str = "") -> str:
    """
    Get a human-readable impact message for an operation.

    Args:
        method: HTTP method
        path: Endpoint path (for context)

    Returns:
        Warning message about the operation's impact
    """
    method = method.upper()

    if method == "DELETE":
        return "⚠️ This action will permanently delete data and cannot be undone."
    elif method == "POST":
        return "This action will create new resources in the target system."
    elif method in ("PUT", "PATCH"):
        return "This action will modify existing resources in the target system."
    else:
        return "This action will query data from the target system."


def get_danger_emoji(danger_level: DangerLevel) -> str:
    """
    Get emoji indicator for danger level.

    Used in UI and messages.
    """
    mapping = {
        "safe": "🟢",
        "caution": "🟡",
        "dangerous": "🟠",
        "critical": "🔴",
    }
    return mapping.get(danger_level, "⚪")


def get_danger_color(danger_level: DangerLevel) -> str:
    """
    Get CSS color class for danger level.

    Used in frontend styling.
    """
    mapping = {
        "safe": "green",
        "caution": "yellow",
        "dangerous": "orange",
        "critical": "red",
    }
    return mapping.get(danger_level, "gray")


# Common safe patterns that can override default dangerous classification
SAFE_POST_PATTERNS = [
    "/auth/login",
    "/auth/token",
    "/auth/refresh",
    "/session",
    "?action=list",  # vCenter pattern for listing via POST
]


def is_safe_post_pattern(path: str) -> bool:
    """
    Check if a POST endpoint matches a known safe pattern.

    Some POST endpoints are safe (login, token refresh, etc.)
    and should be marked as such.

    Args:
        path: Endpoint path

    Returns:
        True if matches a safe POST pattern
    """
    path_lower = path.lower()
    return any(pattern in path_lower for pattern in SAFE_POST_PATTERNS)
