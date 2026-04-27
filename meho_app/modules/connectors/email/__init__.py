# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Email Connector Module.

Provides email sending capability via a pluggable provider abstraction.
Supports SMTP, SendGrid, Mailgun, Amazon SES, and Generic HTTP providers.

Usage:
    from meho_app.modules.connectors.email import EmailConnector
    from meho_app.modules.connectors.email.operations import EMAIL_OPERATIONS
"""

from meho_app.modules.connectors.email.connector import EmailConnector

__all__ = ["EmailConnector"]
