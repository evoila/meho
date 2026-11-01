# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find a secret that a vendor repeats in its error text.

A connector that sends a secret (a password, a private key) may get an
error answer that repeats it. Before the vendor text goes into an answer,
:func:`mentions_secret` checks it for the secret as is and in the forms a
vendor may use when it repeats a value: JSON-escaped, URL-encoded and
HTML-escaped.
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable
from urllib.parse import quote

__all__ = ["mentions_secret"]


def mentions_secret(texts: Iterable[str], secret: str) -> bool:
    """True when any text holds *secret* as is, JSON-escaped, URL-encoded or HTML-escaped."""
    forms = {secret, json.dumps(secret)[1:-1], quote(secret, safe=""), html.escape(secret)}
    return any(form in text for text in texts for form in forms if form)
