# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find a forbidden token in a text when the test knows only its hash.

Some guard tests must check that a text does not contain a private
name. Writing that name in a public test file would publish it. So a
guard keeps the length and the SHA-256 hex digest of each lowercase
name, and this helper compares every window of that length in the text.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable


def find_hashed_token(text: str, tokens: Iterable[tuple[int, str]]) -> str | None:
    """Return the first part of ``text`` that matches a ``(length, sha256)`` pair.

    Returns ``None`` when no window matches. The caller lowercases
    ``text`` when the check must ignore case.
    """
    for length, digest in tokens:
        for start in range(len(text) - length + 1):
            window = text[start : start + length]
            if hashlib.sha256(window.encode("utf-8")).hexdigest() == digest:
                return window
    return None
