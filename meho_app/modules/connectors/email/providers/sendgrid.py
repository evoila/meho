# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SendGrid Email Provider.

Uses httpx to call the SendGrid v3 API directly for email delivery.
Bearer token authentication with JSON payloads.
"""

import httpx

from .base import EmailMessage, EmailProvider, SendResult


class SendGridProvider(EmailProvider):
    """SendGrid API v3 provider using httpx."""

    def __init__(self, config: dict) -> None:
        self.api_key = config["api_key"]
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialize httpx client."""
        if not self._client:
            self._client = httpx.AsyncClient(
                base_url="https://api.sendgrid.com",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    async def send(self, message: EmailMessage) -> SendResult:
        """Send an email via SendGrid v3 API."""
        client = self._get_client()
        payload = {
            "personalizations": [{"to": [{"email": e} for e in message.to_emails]}],
            "from": {"email": message.from_email, "name": message.from_name},
            "subject": message.subject,
            "content": [
                {"type": "text/plain", "value": message.text_body},
                {"type": "text/html", "value": message.html_body},
            ],
        }
        try:
            response = await client.post("/v3/mail/send", json=payload)
            if response.status_code in (200, 202):
                msg_id = response.headers.get("X-Message-Id")
                return SendResult(
                    success=True,
                    provider_message_id=msg_id,
                    status="accepted",
                )
            else:
                return SendResult(
                    success=False,
                    error=f"SendGrid {response.status_code}: {response.text}",
                    status="failed",
                )
        except Exception as e:
            return SendResult(success=False, error=str(e), status="failed")

    async def test_connection(self) -> bool:
        """Verify API key is valid by fetching user profile."""
        client = self._get_client()
        try:
            resp = await client.get("/v3/user/profile")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """Close httpx client."""
        if self._client:
            await self._client.aclose()
            self._client = None
