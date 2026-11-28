# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A made-up key for the tests of a reason that looks like a secret.

The key is built at run time, so no literal value sits in the repo. It has
no credential name around it, so only the shape check can refuse it.
"""

from __future__ import annotations

import base64
import os
from typing import Any


def made_up_key() -> str:
    """A base64 text of 32 random bytes with upper case, lower case and digits."""
    while True:
        text = base64.b64encode(os.urandom(32)).decode("ascii")
        mixed = text.upper() != text and text.lower() != text
        if mixed and any(c.isdigit() for c in text):
            return text


def assert_no_trace(key: str, *places: Any) -> None:
    """No part of *key* (the whole key, its start or its end) is in any of *places*."""
    for place in places:
        text = str(place)
        for part in (key, key[:12], key[-12:]):
            assert part not in text, "a part of the key was found"
