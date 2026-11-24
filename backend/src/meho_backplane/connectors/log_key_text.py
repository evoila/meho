# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hide text in log lines that can be part of a private key.

The redaction finds a private key by its ``BEGIN`` or ``END`` line. A log
read cannot always see them: other messages can stand between the lines of
a key, so its ``BEGIN`` line can be any number of lines back, and a log
that was rotated can start inside a key. No number of earlier lines proves
that a line is outside a key. So text with the form of a key body is hidden
in every log line, whatever the lines before it say.

A key body is base64 text. Ordinary words, numbers, lower case paths and
lower case hex values are not hidden: 16 random base64 characters have no
upper case letter in 1 of 4000 cases, 64 of them practically never.
"""

from __future__ import annotations

import re

__all__ = ["HIDDEN_KEY_TEXT", "hide_key_text"]

HIDDEN_KEY_TEXT = "[REDACTED:possible_private_key]"

#: A line that is only base64 text, as in a PEM block.
_BASE64_LINE_RE = re.compile(r"\s*([A-Za-z0-9+/]+={0,2})\s*\Z")
_LONG = 16
_SHORT = 8
_PATH_RE = re.compile(r"(?:/[a-z0-9]+){2,}/?\Z")
_WORD_RE = re.compile(r"(?:[A-Za-z]?[a-z0-9]+|[A-Z]+)\Z")

#: Base64 text inside a line.
_RUN_RE = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_SURE_RUN = 48
_UPPER_RE = re.compile(r"[A-Z]")
_LOWER_RE = re.compile(r"[a-z]")
_DIGIT_RE = re.compile(r"[0-9]")


def _is_body_line(text: str, behind_body_line: bool) -> bool:
    """Say whether a line that is only base64 text can be a line of a key body.

    The last line of a key body can be short. Directly behind another body
    line any length counts. Elsewhere a short word or number is shown.
    """
    if behind_body_line:
        return True
    if len(text) >= _LONG:
        return _PATH_RE.match(text) is None
    return len(text) >= _SHORT and _WORD_RE.match(text) is None


def _hide_run(match: re.Match[str]) -> str:
    run = match.group()
    if _UPPER_RE.search(run) is None:
        return run
    mixed = _LOWER_RE.search(run) is not None and _DIGIT_RE.search(run) is not None
    return HIDDEN_KEY_TEXT if mixed or len(run) >= _SURE_RUN else run


def hide_key_text(lines: list[str]) -> list[str]:
    """Return *lines* with the text hidden that can be part of a private key."""
    shown: list[str] = []
    behind_body_line = False
    for line in lines:
        alone = _BASE64_LINE_RE.match(line)
        behind_body_line = alone is not None and _is_body_line(alone.group(1), behind_body_line)
        shown.append(HIDDEN_KEY_TEXT if behind_body_line else _RUN_RE.sub(_hide_run, line))
    return shown
