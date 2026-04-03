# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Email Handler Mixins.

SendEmailHandlerMixin: Renders markdown to branded HTML, sends via provider,
logs delivery to EmailDeliveryLogModel.

CheckStatusHandlerMixin: Queries delivery log by email ID.
"""

import os
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser
from io import StringIO
from typing import Any

import markdown
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.modules.connectors.base import OperationResult
from meho_app.modules.connectors.email.models import EmailDeliveryLogModel
from meho_app.modules.connectors.email.providers.base import EmailMessage

# ---------------------------------------------------------------------------
# HTML -> Plain-text stripping (stdlib, no extra dependency)
# ---------------------------------------------------------------------------


class HTMLStripper(HTMLParser):
    """
    Simple HTML tag stripper for plain-text fallback.

    Preserves line breaks for block-level elements and skips style content.
    """

    def __init__(self) -> None:
        super().__init__()
        self.result = StringIO()
        self.skip_data = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("br", "p", "div", "tr", "li"):
            self.result.write("\n")
        if tag == "style":
            self.skip_data = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self.skip_data = False

    def handle_data(self, data: str) -> None:
        if not self.skip_data:
            self.result.write(data)

    def get_text(self) -> str:
        return self.result.getvalue().strip()


def html_to_plaintext(html: str) -> str:
    """Strip HTML to plain text for multipart/alternative fallback."""
    stripper = HTMLStripper()
    stripper.feed(html)
    return stripper.get_text()


# ---------------------------------------------------------------------------
# Jinja2 template loader (loaded once, cached)
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
# Safe (direct-use-of-jinja2): server-controlled email templates with trusted content
_jinja_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=False,  # We control the template content; body_html uses |safe  # noqa: S701 -- template content is trusted
)


# ---------------------------------------------------------------------------
# Handler Mixins
# ---------------------------------------------------------------------------


class SendEmailHandlerMixin:
    """
    Mixin for the send_email operation.

    Expects the host class to provide:
    - self.from_email: str
    - self.from_name: str
    - self.default_recipients: list[str]
    - self.provider: EmailProvider
    - self.provider_type: str
    - self.connector_name: str
    - self.connector_id: str
    - self.tenant_id: str
    """

    async def _handle_send_email(
        self,
        params: dict[str, Any],
        session: AsyncSession,
    ) -> OperationResult:
        """
        Send a branded HTML email.

        1. Convert body_markdown to HTML
        2. Render branded template
        3. Generate plain-text fallback
        4. Send via provider
        5. Log delivery to DB
        """
        subject = params.get("subject", "")
        body_markdown = params.get("body_markdown", "")
        session_url = params.get("session_url", "")

        if not subject or not body_markdown:
            return OperationResult(
                success=False,
                error="Both 'subject' and 'body_markdown' are required",
                error_code="INVALID_PARAMS",
            )

        # 1. Convert markdown to HTML
        body_html = markdown.markdown(body_markdown, extensions=["extra", "nl2br"])

        # 2. Render branded HTML template
        # Safe (direct-use-of-jinja2): server-controlled email template rendering
        template = _jinja_env.get_template("email_base.html")
        rendered_html = template.render(
            subject=subject,
            body_html=body_html,
            session_url=session_url,
            sent_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            connector_name=getattr(self, "connector_name", "Email"),
            provider_type=getattr(self, "provider_type", "smtp"),
        )

        # 3. Generate plain-text fallback
        plain_text = html_to_plaintext(rendered_html)

        # 4. Build and send email message
        message = EmailMessage(
            from_email=getattr(self, "from_email", ""),
            from_name=getattr(self, "from_name", "MEHO"),
            to_emails=getattr(self, "default_recipients", []),
            subject=subject,
            html_body=rendered_html,
            text_body=plain_text,
        )

        provider = getattr(self, "provider", None)
        if not provider:
            return OperationResult(
                success=False,
                error="Email provider not initialized",
                error_code="PROVIDER_ERROR",
            )

        result = await provider.send(message)

        # 5. Log delivery to DB
        log_entry = EmailDeliveryLogModel(
            id=uuid.uuid4(),
            connector_id=uuid.UUID(str(getattr(self, "connector_id", ""))),
            tenant_id=str(getattr(self, "tenant_id", "")),
            from_email=message.from_email,
            to_emails=message.to_emails,
            subject=subject,
            provider_type=getattr(self, "provider_type", "smtp"),
            provider_message_id=result.provider_message_id,
            status=result.status,
            error_message=result.error,
            created_at=datetime.now(UTC),
        )
        session.add(log_entry)
        await session.flush()

        if result.success:
            return OperationResult(
                success=True,
                data={
                    "email_id": str(log_entry.id),
                    "status": result.status,
                    "provider_message_id": result.provider_message_id,
                    "recipients": len(message.to_emails),
                    "subject": subject,
                },
            )
        else:
            return OperationResult(
                success=False,
                error=result.error or "Email sending failed",
                error_code="SEND_FAILED",
                data={
                    "email_id": str(log_entry.id),
                    "status": result.status,
                },
            )


class CheckStatusHandlerMixin:
    """
    Mixin for the check_status operation.

    Queries EmailDeliveryLogModel by ID.
    """

    async def _handle_check_status(
        self,
        params: dict[str, Any],
        session: AsyncSession,
    ) -> OperationResult:
        """Check the delivery status of a previously sent email."""
        email_id = params.get("email_id", "")
        if not email_id:
            return OperationResult(
                success=False,
                error="'email_id' is required",
                error_code="INVALID_PARAMS",
            )

        try:
            email_uuid = uuid.UUID(email_id)
        except ValueError:
            return OperationResult(
                success=False,
                error=f"Invalid email_id format: {email_id}",
                error_code="INVALID_PARAMS",
            )

        query = select(EmailDeliveryLogModel).where(EmailDeliveryLogModel.id == email_uuid)
        result = await session.execute(query)
        log_entry = result.scalar_one_or_none()

        if not log_entry:
            return OperationResult(
                success=False,
                error=f"Email delivery record not found: {email_id}",
                error_code="NOT_FOUND",
            )

        return OperationResult(
            success=True,
            data={
                "email_id": str(log_entry.id),
                "status": log_entry.status,
                "subject": log_entry.subject,
                "to_emails": log_entry.to_emails,
                "from_email": log_entry.from_email,
                "provider_type": log_entry.provider_type,
                "provider_message_id": log_entry.provider_message_id,
                "error_message": log_entry.error_message,
                "created_at": (log_entry.created_at.isoformat() if log_entry.created_at else None),
            },
        )
