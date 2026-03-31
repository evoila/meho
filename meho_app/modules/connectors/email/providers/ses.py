# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Amazon SES Email Provider (via SMTP).

Uses the SES SMTP endpoint rather than the SES API to avoid boto3/SigV4
complexity. Derives SMTP credentials from the IAM access key and secret
key using the AWS-documented algorithm.

Reference: https://docs.aws.amazon.com/ses/latest/dg/smtp-credentials.html
"""

import base64
import hashlib
import hmac

from .base import EmailMessage, EmailProvider, SendResult
from .smtp import SMTPProvider


def derive_smtp_password(secret_key: str, region: str) -> str:
    """
    Derive SES SMTP password from AWS secret access key.

    Uses the AWS-documented HMAC chain algorithm:
    1. Start with "AWS4" + secret_key
    2. HMAC-SHA256 chain: date -> region -> service -> terminal -> message
    3. Prepend version byte (0x04) and base64 encode
    """
    DATE = "11111111"
    SERVICE = "ses"
    MESSAGE = "SendRawEmail"
    TERMINAL = "aws4_request"
    VERSION = 0x04

    signature = hmac.new(
        ("AWS4" + secret_key).encode("utf-8"),
        DATE.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    signature = hmac.new(signature, region.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(signature, SERVICE.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(signature, TERMINAL.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(signature, MESSAGE.encode("utf-8"), hashlib.sha256).digest()

    signature_and_version = bytes([VERSION]) + signature
    return base64.b64encode(signature_and_version).decode("utf-8")


class SESProvider(EmailProvider):
    """
    Amazon SES provider wrapping SMTPProvider with derived SMTP credentials.

    SES SMTP endpoints: email-smtp.{region}.amazonaws.com:587
    """

    def __init__(self, config: dict):
        self.access_key = config["ses_access_key"]
        self.secret_key = config["ses_secret_key"]
        self.region = config.get("ses_region", "us-east-1")

        # Derive SMTP password from IAM credentials
        smtp_password = derive_smtp_password(self.secret_key, self.region)

        # Create inner SMTP provider with SES SMTP endpoint
        smtp_config = {
            "smtp_host": f"email-smtp.{self.region}.amazonaws.com",
            "smtp_port": 587,
            "smtp_starttls": True,
            "smtp_username": self.access_key,
            "smtp_password": smtp_password,
        }
        self._smtp = SMTPProvider(smtp_config)

    async def send(self, message: EmailMessage) -> SendResult:
        """Send via SES SMTP endpoint."""
        result = await self._smtp.send(message)
        # Override status to "accepted" for SES (provider accepts, delivers async)
        if result.success:
            result.status = "accepted"
        return result

    async def test_connection(self) -> bool:
        """Test SES SMTP connectivity."""
        return await self._smtp.test_connection()

    async def close(self) -> None:
        """Delegate to inner SMTP provider."""
        await self._smtp.close()
