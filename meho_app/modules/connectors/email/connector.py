# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Email Connector.

Extends BaseConnector with a pluggable EmailProvider abstraction.
Dispatches to the correct provider (SMTP, SendGrid, Mailgun, SES,
Generic HTTP) based on connector config.

2 operations across 1 category:
- Email: send_email (WRITE), check_status (READ)

Example:
    connector = EmailConnector(
        connector_id="abc123",
        config={
            "provider_type": "smtp",
            "from_email": "meho@company.com",
            "from_name": "MEHO Alerts",
            "default_recipients": ["team@company.com"],
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
        },
        credentials={
            "smtp_username": "meho@company.com",
            "smtp_password": "app-password",
        },
    )
"""

import time
from collections.abc import Callable
from datetime import UTC
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import (
    BaseConnector,
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)
from meho_app.modules.connectors.email.handlers import (
    CheckStatusHandlerMixin,
    SendEmailHandlerMixin,
)
from meho_app.modules.connectors.email.operations import EMAIL_OPERATIONS
from meho_app.modules.connectors.email.providers.base import (
    EmailMessage,
    EmailProvider,
)
from meho_app.modules.connectors.email.providers.generic_http import GenericHTTPProvider
from meho_app.modules.connectors.email.providers.mailgun import MailgunProvider
from meho_app.modules.connectors.email.providers.sendgrid import SendGridProvider
from meho_app.modules.connectors.email.providers.ses import SESProvider
from meho_app.modules.connectors.email.providers.smtp import SMTPProvider

logger = get_logger(__name__)


class EmailConnector(
    BaseConnector,
    SendEmailHandlerMixin,
    CheckStatusHandlerMixin,
):
    """
    Email connector with provider abstraction.

    Supports SMTP, SendGrid, Mailgun, Amazon SES, and Generic HTTP
    providers. The provider is selected at registration time via config.

    Agent calls send_email (WRITE) to send branded HTML emails and
    check_status (READ) to query the delivery log.
    """

    def __init__(
        self,
        connector_id: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ):
        super().__init__(connector_id, config, credentials)

        # Email-specific config
        self.from_email: str = config.get("from_email", "")
        self.from_name: str = config.get("from_name", "MEHO")
        self.default_recipients: list[str] = config.get("default_recipients", [])
        self.provider_type: str = config.get("provider_type", "smtp")
        self.connector_name: str = config.get("connector_name", "Email")
        self.tenant_id: str = config.get("tenant_id", "")

        # Create provider from config + credentials
        self.provider: EmailProvider = self._create_provider(config, credentials)

        # Build operation dispatch table
        self._operation_handlers: dict[str, Callable] = {
            "send_email": self._dispatch_send_email,
            "check_status": self._dispatch_check_status,
        }

    # =========================================================================
    # PROVIDER FACTORY
    # =========================================================================

    def _create_provider(
        self, config: dict[str, Any], credentials: dict[str, Any]
    ) -> EmailProvider:
        """Dispatch to the correct provider based on config."""
        provider_type = config.get("provider_type", "smtp")

        if provider_type == "smtp":
            return SMTPProvider({**config, **credentials})
        elif provider_type == "sendgrid":
            return SendGridProvider(credentials)
        elif provider_type == "mailgun":
            return MailgunProvider(credentials)
        elif provider_type == "ses":
            return SESProvider(credentials)
        elif provider_type == "generic_http":
            return GenericHTTPProvider({**config, **credentials})
        else:
            raise ValueError(f"Unknown email provider type: {provider_type}")

    # =========================================================================
    # CONNECTION & LIFECYCLE
    # =========================================================================

    async def connect(self) -> bool:
        """Email connector has no persistent connection."""
        self._is_connected = True
        return True

    async def disconnect(self) -> None:
        """Close the email provider."""
        await self.provider.close()
        self._is_connected = False

    async def test_connection(self) -> bool:
        """
        Test email provider connectivity.

        If test_connection succeeds AND default_recipients are configured,
        sends a branded test email to confirm end-to-end delivery.
        """
        try:
            ok = await self.provider.test_connection()
            if not ok:
                logger.warning(f"Email provider test_connection failed for {self.connector_id}")
                return False

            # Send a real test email if recipients are configured
            if self.default_recipients:
                from datetime import datetime

                import markdown as md_lib

                from meho_app.modules.connectors.email.handlers.email_handlers import (
                    _jinja_env,
                    html_to_plaintext,
                )

                test_md = (
                    "## Email Connector Test\n\n"
                    "Your MEHO email connector is configured and working correctly.\n\n"
                    f"- **Provider:** {self.provider_type}\n"
                    f"- **From:** {self.from_name} <{self.from_email}>\n"
                    f"- **Recipients:** {', '.join(self.default_recipients)}\n"
                )

                body_html = md_lib.markdown(test_md, extensions=["extra", "nl2br"])
                # nosemgrep: direct-use-of-jinja2 -- server-controlled email template, not user input
                template = _jinja_env.get_template("email_base.html")
                rendered_html = template.render(
                    subject="MEHO Email Connector Test",
                    body_html=body_html,
                    session_url="",
                    sent_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
                    connector_name=self.connector_name,
                    provider_type=self.provider_type,
                )
                plain_text = html_to_plaintext(rendered_html)

                test_msg = EmailMessage(
                    from_email=self.from_email,
                    from_name=self.from_name,
                    to_emails=self.default_recipients,
                    subject="MEHO Email Connector Test",
                    html_body=rendered_html,
                    text_body=plain_text,
                )

                result = await self.provider.send(test_msg)
                if not result.success:
                    logger.warning(f"Email test send failed: {result.error}")
                    return False

                logger.info(f"Email test sent to {', '.join(self.default_recipients)}")

            return True
        except Exception as e:
            logger.warning(f"Email connection test failed: {e}")
            return False

    # =========================================================================
    # EXECUTION
    # =========================================================================

    async def execute(
        self,
        operation_id: str,
        parameters: dict[str, Any],
    ) -> OperationResult:
        """Execute an email operation."""
        start_time = time.time()

        handler = self._operation_handlers.get(operation_id)
        if not handler:
            return OperationResult(
                success=False,
                error=f"Unknown operation: {operation_id}",
                error_code="NOT_FOUND",
                operation_id=operation_id,
            )

        try:
            result = await handler(parameters)
            duration_ms = (time.time() - start_time) * 1000
            result.operation_id = operation_id
            result.duration_ms = duration_ms
            logger.info(f"{operation_id}: completed in {duration_ms:.1f}ms")
            return result
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(f"{operation_id} failed: {e}", exc_info=True)
            return OperationResult(
                success=False,
                error=str(e),
                error_code="INTERNAL_ERROR",
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

    async def _dispatch_send_email(self, parameters: dict[str, Any]) -> OperationResult:
        """Dispatch send_email with a self-managed DB session."""
        from meho_app.database import get_session_maker

        session_maker = get_session_maker()
        async with session_maker() as session:
            result = await self._handle_send_email(parameters, session)
            await session.commit()
            return result

    async def _dispatch_check_status(self, parameters: dict[str, Any]) -> OperationResult:
        """Dispatch check_status with a self-managed DB session."""
        from meho_app.database import get_session_maker

        session_maker = get_session_maker()
        async with session_maker() as session:
            return await self._handle_check_status(parameters, session)

    # =========================================================================
    # OPERATIONS & TYPES
    # =========================================================================

    def get_operations(self) -> list[OperationDefinition]:
        """Get email operations for registration."""
        return list(EMAIL_OPERATIONS)

    def get_types(self) -> list[TypeDefinition]:
        """Get email types for registration.

        Returns empty list -- emails are not topology entities.
        """
        return []
