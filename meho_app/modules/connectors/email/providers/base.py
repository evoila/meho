# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Email Provider Abstraction.

Defines the EmailProvider ABC and shared dataclasses for all email providers.
Each provider (SMTP, SendGrid, Mailgun, SES, Generic HTTP) implements this
interface to provide a uniform email sending capability.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EmailMessage:
    """Standardized email message for all providers."""

    from_email: str
    from_name: str
    to_emails: list[str]
    subject: str
    html_body: str
    text_body: str  # plain-text fallback (multipart/alternative)


@dataclass
class SendResult:
    """Standardized result from sending an email."""

    success: bool
    provider_message_id: str | None = None
    status: str = "sent"  # sent, accepted, failed
    error: str | None = None
    provider_response: dict[str, Any] | None = field(default=None)


class EmailProvider(ABC):
    """
    Abstract base for email sending providers.

    All providers implement send(), test_connection(), and close().
    The EmailConnector dispatches to the correct provider based on config.
    """

    @abstractmethod
    async def send(self, message: EmailMessage) -> SendResult:
        """Send an email via this provider."""
        ...

    @abstractmethod
    async def test_connection(self) -> bool:
        """Test that the provider is configured correctly."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Clean up provider resources (e.g., close httpx client)."""
        ...
