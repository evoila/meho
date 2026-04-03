# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Generic HTTP Email Provider.

Allows any HTTP-based email API by letting the operator define the endpoint,
auth header, and a Jinja2 payload template. The template is rendered with
email message variables and POSTed as JSON.
"""

import json

import httpx
from jinja2 import Environment

from .base import EmailMessage, EmailProvider, SendResult


class GenericHTTPProvider(EmailProvider):
    """
    Generic HTTP provider with Jinja2 payload template.

    The operator provides:
    - endpoint_url: Where to POST the email payload
    - auth_header: Authorization header value (e.g., "Bearer xxx")
    - payload_template: Jinja2 template string that renders to JSON

    Template variables available:
    {{ from_email }}, {{ from_name }}, {{ to_emails }}, {{ subject }},
    {{ html_body }}, {{ text_body }}
    """

    def __init__(self, config: dict) -> None:
        self.endpoint_url = config["endpoint_url"]
        self.auth_header = config.get("auth_header", "")
        self.payload_template_str = config.get("payload_template", "")
        self._client: httpx.AsyncClient | None = None
        # Safe (direct-use-of-jinja2): admin-configured payload template for HTTP email provider
        self._jinja_env = Environment(autoescape=False)  # noqa: S701 -- template content is trusted

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialize httpx client."""
        if not self._client:
            headers = {"Content-Type": "application/json"}
            if self.auth_header:
                headers["Authorization"] = self.auth_header
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=30.0,
            )
        return self._client

    def _render_payload(self, message: EmailMessage) -> str:
        """Render the Jinja2 payload template with email message variables."""
        template = self._jinja_env.from_string(self.payload_template_str)
        return template.render(
            from_email=message.from_email,
            from_name=message.from_name,
            to_emails=message.to_emails,
            subject=message.subject,
            html_body=message.html_body,
            text_body=message.text_body,
        )

    async def send(self, message: EmailMessage) -> SendResult:
        """Send an email by POSTing rendered JSON to the configured endpoint."""
        client = self._get_client()
        try:
            rendered = self._render_payload(message)
            payload = json.loads(rendered)

            response = await client.post(self.endpoint_url, json=payload)
            if 200 <= response.status_code < 300:
                return SendResult(
                    success=True,
                    status="accepted",
                    provider_response=response.json() if response.text else None,
                )
            else:
                return SendResult(
                    success=False,
                    error=f"HTTP {response.status_code}: {response.text}",
                    status="failed",
                )
        except json.JSONDecodeError as e:
            return SendResult(
                success=False,
                error=f"Payload template rendered invalid JSON: {e}",
                status="failed",
            )
        except Exception as e:
            return SendResult(success=False, error=str(e), status="failed")

    async def test_connection(self) -> bool:
        """Test endpoint reachability with a minimal POST."""
        client = self._get_client()
        try:
            # Send a minimal test to verify endpoint and auth are valid
            test_payload = {"test": True}
            resp = await client.post(self.endpoint_url, json=test_payload)
            return 200 <= resp.status_code < 300
        except Exception:
            return False

    async def close(self) -> None:
        """Close httpx client."""
        if self._client:
            await self._client.aclose()
            self._client = None
