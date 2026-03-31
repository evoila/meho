# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Mailgun Email Provider.

Uses httpx to call the Mailgun API with HTTP Basic Auth (api:key).
Sends via form data to /v3/{domain}/messages.
"""

import httpx

from .base import EmailMessage, EmailProvider, SendResult


class MailgunProvider(EmailProvider):
    """Mailgun API provider using httpx."""

    def __init__(self, config: dict):
        self.api_key = config["api_key"]
        self.domain = config["mailgun_domain"]
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialize httpx client with Basic Auth."""
        if not self._client:
            self._client = httpx.AsyncClient(
                base_url="https://api.mailgun.net",
                auth=("api", self.api_key),
                timeout=30.0,
            )
        return self._client

    async def send(self, message: EmailMessage) -> SendResult:
        """Send an email via Mailgun API using form data."""
        client = await self._get_client()
        form_data = {
            "from": f"{message.from_name} <{message.from_email}>",
            "to": message.to_emails,
            "subject": message.subject,
            "text": message.text_body,
            "html": message.html_body,
        }
        try:
            response = await client.post(
                f"/v3/{self.domain}/messages",
                data=form_data,
            )
            if response.status_code == 200:
                resp_json = response.json()
                msg_id = resp_json.get("id")
                return SendResult(
                    success=True,
                    provider_message_id=msg_id,
                    status="accepted",
                    provider_response=resp_json,
                )
            else:
                return SendResult(
                    success=False,
                    error=f"Mailgun {response.status_code}: {response.text}",
                    status="failed",
                )
        except Exception as e:
            return SendResult(success=False, error=str(e), status="failed")

    async def test_connection(self) -> bool:
        """Verify API key and domain are valid."""
        client = await self._get_client()
        try:
            resp = await client.get(f"/v3/domains/{self.domain}")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """Close httpx client."""
        if self._client:
            await self._client.aclose()
            self._client = None
