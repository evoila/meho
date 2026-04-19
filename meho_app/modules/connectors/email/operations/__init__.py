# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Email Operations - Combined Definitions.

This module exports all email operation definitions.

Operations:
- send_email (WRITE): Send branded HTML email
- check_status (READ): Check delivery status

Total: 2 operations
"""

from .email_ops import EMAIL_OPERATIONS, WRITE_OPERATIONS

# Version for auto-sync on startup
# Increment this when operations are added or significantly changed
EMAIL_OPERATIONS_VERSION = "1.0.0"

__all__ = [
    "EMAIL_OPERATIONS",
    "EMAIL_OPERATIONS_VERSION",
    "WRITE_OPERATIONS",
]
