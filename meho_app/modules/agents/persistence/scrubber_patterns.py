# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Centralized pattern definitions for sensitive data detection.

This module contains all pattern definitions used by the scrubber to identify
and redact sensitive information. Separating patterns from scrubbing logic
makes it easier to:
1. Add new patterns in one place
2. Test pattern matching independently
3. Share patterns across different scrubbing contexts

Example:
    >>> from meho_app.modules.agents.persistence.scrubber_patterns import ScrubPatterns
    >>> ScrubPatterns.is_sensitive_key("password")
    True
    >>> ScrubPatterns.is_sensitive_key("username")
    False
"""

from __future__ import annotations

import re
from re import Pattern


class ScrubPatterns:
    """Centralized pattern definitions for sensitive data detection.

    This class encapsulates all sensitive data patterns and provides methods
    to check if a key or value should be redacted.
    """

    # Maximum payload size before truncation (50KB)
    MAX_PAYLOAD_SIZE: int = 50 * 1024

    # Maximum result sample rows
    MAX_RESULT_SAMPLE_ROWS: int = 10

    # Replacement marker for redacted values
    REDACTED: str = "[REDACTED]"

    # Keys that should have their values redacted
    SENSITIVE_KEYS: frozenset[str] = frozenset(
        {
            # Authentication
            "password",
            "passwd",
            "pwd",
            "secret",
            "token",
            "api_key",
            "apikey",
            "api-key",
            "access_token",
            "access-token",
            "refresh_token",
            "refresh-token",
            "bearer",
            "auth",
            "authorization",
            "x-api-key",
            "x-auth-token",
            "x-access-token",
            # Credentials
            "credential",
            "credentials",
            "private_key",
            "private-key",
            "privatekey",
            "session_id",
            "sessionid",
            "session-id",
            "csrf",
            "csrf_token",
            "xsrf",
            "xsrf_token",
            # Database
            "connection_string",
            "connectionstring",
            "database_url",
            "db_password",
            "db_pass",
            # Cloud
            "aws_secret",
            "aws_access_key",
            "azure_key",
            "gcp_key",
            # Generic
            "key",
            "cert",
            "certificate",
        }
    )

    # Headers that should always be redacted
    SENSITIVE_HEADERS: frozenset[str] = frozenset(
        {
            "authorization",
            "x-api-key",
            "x-auth-token",
            "x-access-token",
            "x-csrf-token",
            "cookie",
            "set-cookie",
            "proxy-authorization",
            "www-authenticate",
        }
    )

    # Patterns that indicate sensitive values
    SENSITIVE_PATTERNS: list[Pattern[str]] = [  # noqa: RUF012 -- mutable default is intentional class state
        # Bearer tokens
        re.compile(r"Bearer\s+[A-Za-z0-9\-_]+\.?[A-Za-z0-9\-_]*\.?[A-Za-z0-9\-_]*", re.I),
        # Basic auth
        re.compile(r"Basic\s+[A-Za-z0-9+/=]+", re.I),
        # API keys (common patterns)
        re.compile(r"[A-Za-z0-9]{32,}", re.I),  # Long alphanumeric strings
        # AWS access keys
        re.compile(r"AKIA[A-Z0-9]{16}"),
        # AWS secret keys
        re.compile(r"[A-Za-z0-9/+]{40}"),
        # GitHub tokens
        re.compile(r"ghp_[A-Za-z0-9]{36}"),
        re.compile(r"gho_[A-Za-z0-9]{36}"),
        re.compile(r"ghu_[A-Za-z0-9]{36}"),
        # Slack tokens
        re.compile(r"xox[baprs]-[A-Za-z0-9\-]+"),
        # Generic secrets
        re.compile(r'"password"\s*:\s*"[^"]*"', re.I),
        re.compile(r'"secret"\s*:\s*"[^"]*"', re.I),
        re.compile(r'"token"\s*:\s*"[^"]*"', re.I),
        re.compile(r'"api_key"\s*:\s*"[^"]*"', re.I),
    ]

    @classmethod
    def is_sensitive_key(cls, key: str) -> bool:
        """Check if a key name indicates sensitive data.

        Args:
            key: The key name to check.

        Returns:
            True if the key appears to hold sensitive data.
        """
        key_lower = key.lower().replace("-", "_")

        # Check exact matches
        if key_lower in cls.SENSITIVE_KEYS:
            return True

        # Check if key contains sensitive terms
        return any(sensitive in key_lower for sensitive in cls.SENSITIVE_KEYS)

    @classmethod
    def is_sensitive_header(cls, header: str) -> bool:
        """Check if a header name should be redacted.

        Args:
            header: The header name to check.

        Returns:
            True if the header should be redacted.
        """
        header_lower = header.lower()
        return header_lower in cls.SENSITIVE_HEADERS or cls.is_sensitive_key(header)

    @classmethod
    def matches_sensitive_pattern(cls, value: str) -> bool:
        """Check if a value matches any sensitive pattern.

        Args:
            value: The string value to check.

        Returns:
            True if the value matches any sensitive pattern.
        """
        return any(pattern.search(value) for pattern in cls.SENSITIVE_PATTERNS)

    @classmethod
    def scrub_patterns_from_value(cls, value: str) -> str:
        """Remove all sensitive patterns from a string value.

        Args:
            value: The string value to scrub.

        Returns:
            The value with sensitive patterns replaced with REDACTED marker.
        """
        result = value
        for pattern in cls.SENSITIVE_PATTERNS:
            result = pattern.sub(cls.REDACTED, result)
        return result
