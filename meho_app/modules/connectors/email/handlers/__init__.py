# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Email Connector Handler Mixins.

Each mixin provides operation handlers for email operations.
"""

from .email_handlers import (
    CheckStatusHandlerMixin,
    SendEmailHandlerMixin,
    html_to_plaintext,
)

__all__ = [
    "CheckStatusHandlerMixin",
    "SendEmailHandlerMixin",
    "html_to_plaintext",
]
