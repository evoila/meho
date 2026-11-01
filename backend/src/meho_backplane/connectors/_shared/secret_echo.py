# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find a secret that a vendor error answer repeats.

Some vendors quote the value they refused in an error answer, for example
a new password that does not meet the password policy. A connector that
sends a secret calls :func:`mentions_secret` on the answer's texts before
any of them leave the connector. When a secret is in them, the connector
drops all vendor text.
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable
from urllib.parse import quote

import httpx

__all__ = ["mentions_secret", "response_text"]


def response_text(response: httpx.Response) -> str:
    """The body of *response* as text, or ``""`` when it cannot be read."""
    try:
        return response.text
    except (UnicodeDecodeError, httpx.StreamError):
        return ""


def mentions_secret(texts: Iterable[str], secret: str) -> bool:
    """True when any text holds *secret* as is, JSON-escaped, URL-encoded or HTML-escaped."""
    forms = {secret, json.dumps(secret)[1:-1], quote(secret, safe=""), html.escape(secret)}
    return any(form in text for text in texts for form in forms if form)
