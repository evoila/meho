# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SMTP Email Provider.

Uses aiosmtplib for async SMTP email delivery. Supports both direct TLS
(port 465) and STARTTLS (port 587) with auto-detection based on port.
"""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

from .base import EmailMessage, EmailProvider, SendResult


class SMTPProvider(EmailProvider):
    """SMTP provider using aiosmtplib."""

    def __init__(self, config: dict):
        self.hostname = config["smtp_host"]
        self.port = config.get("smtp_port", 587)
        self.username = config.get("smtp_username")
        self.password = config.get("smtp_password")

        # Auto-detect TLS mode based on port (Pitfall 1 from research)
        # Port 465 -> direct TLS (use_tls=True)
        # Port 587 -> STARTTLS (start_tls=True)
        # Port 25  -> no encryption (warn in docs)
        if self.port == 465:
            self.use_tls = True
            self.start_tls = False
        elif self.port == 587:
            self.use_tls = False
            self.start_tls = True
        else:
            # Allow explicit override from config
            self.use_tls = config.get("smtp_tls", False)
            self.start_tls = config.get("smtp_starttls", False)

    async def send(self, message: EmailMessage) -> SendResult:
        """Send an email via SMTP using aiosmtplib."""
        try:
            mime_msg = MIMEMultipart("alternative")
            mime_msg["From"] = f"{message.from_name} <{message.from_email}>"
            mime_msg["To"] = ", ".join(message.to_emails)
            mime_msg["Subject"] = message.subject

            # Plain text first (fallback), HTML second (preferred)
            mime_msg.attach(MIMEText(message.text_body, "plain", "utf-8"))
            mime_msg.attach(MIMEText(message.html_body, "html", "utf-8"))

            await aiosmtplib.send(
                mime_msg,
                hostname=self.hostname,
                port=self.port,
                username=self.username,
                password=self.password,
                use_tls=self.use_tls,
                start_tls=self.start_tls if not self.use_tls else False,
            )
            return SendResult(success=True, status="sent")
        except Exception as e:
            return SendResult(success=False, error=str(e), status="failed")

    async def test_connection(self) -> bool:
        """Test SMTP connectivity by connecting and issuing EHLO."""
        try:
            smtp = aiosmtplib.SMTP(
                hostname=self.hostname,
                port=self.port,
                use_tls=self.use_tls,
                start_tls=self.start_tls if not self.use_tls else False,
            )
            await smtp.connect()
            if self.username and self.password:
                await smtp.login(self.username, self.password)
            await smtp.quit()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        """No-op -- SMTP connections are per-send, no persistent state."""
        pass
