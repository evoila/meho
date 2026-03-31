# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Credential masking utility for superadmin tenant context.

When a superadmin operates in a tenant context (via X-Acting-As-Tenant header),
sensitive credential fields are masked to prevent exposure of tenant secrets.

This is a security best practice for multi-tenant systems where superadmins
should have visibility into configurations but not actual credential values.
"""

from typing import Any

from meho_app.core.auth_context import UserContext

# Fields that contain sensitive credential data
SENSITIVE_FIELDS: set[str] = {
    # Authentication credentials
    "password",
    "api_key",
    "secret",
    "token",
    "bearer_token",
    "private_key",
    # Typed connector credentials
    "api_token_secret",
    "service_account_json",
    "kubeconfig",
    "ca_certificate",
    # Generic sensitive patterns
    "credentials",
    "auth_token",
    "access_token",
    "refresh_token",
}

# Fields that may contain nested sensitive data
NESTED_SENSITIVE_CONTAINERS: set[str] = {
    "auth_config",
    "login_config",
    "protocol_config",
}


def _mask_dict_recursive(
    data: dict[str, Any],
    masked_fields: set[str],
) -> dict[str, Any]:
    """
    Recursively mask sensitive fields in a dictionary.

    Args:
        data: Dictionary to process
        masked_fields: Set to collect names of masked fields

    Returns:
        New dictionary with sensitive fields masked
    """
    result = {}

    for key, value in data.items():
        key_lower = key.lower()

        # Check if this is a sensitive field
        is_sensitive = key_lower in SENSITIVE_FIELDS or any(
            sensitive in key_lower for sensitive in ["password", "secret", "token"]
        )
        # Special case: 'key' should only trigger if it's clearly a credential key
        if not is_sensitive and "key" in key_lower:
            # Only mask if it looks like a credential key (api_key, secret_key, etc.)
            # but not generic things like "token_path" which contains config paths
            is_sensitive = any(
                prefix in key_lower for prefix in ["api_", "secret_", "private_", "auth_"]
            ) or key_lower.endswith("_key")

        if is_sensitive:
            if value is not None and value != "" and value != {}:
                result[key] = None
                masked_fields.add(key)
            else:
                # Preserve empty/None values as-is
                result[key] = value
        elif isinstance(value, dict):
            # Recursively process nested dictionaries
            nested_masked: set[str] = set()
            result[key] = _mask_dict_recursive(value, nested_masked)

            # If any nested fields were masked, mark the container
            if nested_masked:
                masked_fields.add(key)
        elif isinstance(value, list):
            # Process lists (may contain dicts)
            result[key] = [
                _mask_dict_recursive(item, masked_fields) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value

    return result


def mask_credentials(data: dict[str, Any], user: UserContext) -> dict[str, Any]:
    """
    Mask sensitive credential fields if user is acting as superadmin in tenant context.

    When a superadmin uses the X-Acting-As-Tenant header to view tenant data,
    this function masks all sensitive credential fields to prevent exposure.

    Args:
        data: Dictionary containing connector or other data with potential credentials
        user: Current user context

    Returns:
        New dictionary with:
        - Sensitive fields set to None
        - {field}_masked = True flags added for masked fields

    Example:
        >>> data = {"password": "secret123", "name": "My Connector"}
        >>> user = UserContext(acting_as_superadmin=True, ...)
        >>> result = mask_credentials(data, user)
        >>> result
        {"password": None, "password_masked": True, "name": "My Connector"}
    """
    # Only mask if user is acting as superadmin in tenant context
    if not getattr(user, "acting_as_superadmin", False):
        return data

    # Track which fields were masked
    masked_fields: set[str] = set()

    # Recursively mask the data
    result = _mask_dict_recursive(data, masked_fields)

    # Add mask indicators for top-level and nested containers
    for field in masked_fields:
        mask_key = f"{field}_masked"
        if mask_key not in result:
            result[mask_key] = True

    # Always add indicators for known containers if they exist
    for container in NESTED_SENSITIVE_CONTAINERS:
        if data.get(container):
            mask_key = f"{container}_masked"
            if mask_key not in result:
                result[mask_key] = container in masked_fields

    return result


def is_field_sensitive(field_name: str) -> bool:
    """
    Check if a field name indicates sensitive data.

    Args:
        field_name: Name of the field to check

    Returns:
        True if the field is considered sensitive
    """
    field_lower = field_name.lower()

    if field_lower in SENSITIVE_FIELDS:
        return True

    # Check for common sensitive patterns
    sensitive_patterns = ["password", "secret", "token", "key", "credential"]
    return any(pattern in field_lower for pattern in sensitive_patterns)
