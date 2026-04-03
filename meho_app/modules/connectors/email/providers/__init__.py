# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Email Providers.

Re-exports all provider implementations and base classes.
"""

from .base import EmailMessage, EmailProvider, SendResult
from .generic_http import GenericHTTPProvider
from .mailgun import MailgunProvider
from .sendgrid import SendGridProvider
from .ses import SESProvider
from .smtp import SMTPProvider

__all__ = [
    "EmailMessage",
    "EmailProvider",
    "GenericHTTPProvider",
    "MailgunProvider",
    "SESProvider",
    "SMTPProvider",
    "SendGridProvider",
    "SendResult",
]
