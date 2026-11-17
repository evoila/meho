# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the approver of ``linux.script.run`` reads about the script.

For ``linux.script.run`` the script body is the change that waits for
approval, so the park-time preview shows it. The fields are:

* ``script`` -- the script text. Values that look like secrets are replaced
  by a marker in two passes. First the shell-aware pass of
  :func:`~meho_backplane.redaction.command_line.hide_shell_script`, the
  same one the ``k8s.job.create`` preview uses for a command line: the value
  of a ``NAME=VALUE`` whose name holds a secret word (``PGPASSWORD=...``,
  ``export AWS_SECRET_ACCESS_KEY=...``), the value after a secret flag
  (``--password x``, ``sshpass -p x``), the password in ``curl -u
  user:password`` and in ``mysql -pVALUE``, the password in a URL, tokens
  with a known vendor prefix. Then the packaged default redaction policy
  that MEHO uses for error text (a bearer token, a JWT, a private key
  block, a quoted ``"password": "..."`` pair). The text is cut at
  :data:`SCRIPT_SHOWN_MAX_BYTES` bytes per page.
* ``script_pages`` -- the number of pages the shown text has. The preview
  holds page 1. :func:`script_page_fields` gives any page; the approver reads
  them in the console, in the CLI or over REST.
* ``script_bytes`` -- the size of the full script in UTF-8 bytes.
* ``script_sha256`` -- the SHA-256 of the full script as it will run, so the
  approver can compare it with a reviewed copy. Left out when values were
  hidden: with the rest of the text in view, the hash would let a reader test
  guesses of the hidden value.
* ``script_values_hidden`` / ``script_truncated`` -- ``True`` when the shown
  text differs from the script that runs. ``script_truncated`` means the text
  has more than one page.
* ``script_unavailable`` -- set, and no text shown, when the redaction pass
  fails.
* ``script_ref`` / ``script_check`` -- the reviewed source the caller named,
  and ``"matches"`` or ``"does not match"`` when the caller sent the expected
  SHA-256 of the script (``expected_sha256``).

The arguments and the ``env`` values are not part of this module: the preview
builder in :mod:`~meho_backplane.connectors.linux.ops_write` shows only their
size and names.
"""

from __future__ import annotations

import hashlib
from typing import Any, Final

from meho_backplane.redaction.command_line import hide_shell_script
from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.resolver import get_default_policy
from meho_backplane.redaction.secret_fields import scrub_secret_fields_in_text

__all__ = [
    "SCRIPT_CHECK_MATCHES",
    "SCRIPT_CHECK_MISMATCH",
    "SCRIPT_MAX_BYTES",
    "SCRIPT_SHOWN_MAX_BYTES",
    "ScriptPageOutOfRangeError",
    "script_matches_expected",
    "script_page_fields",
    "script_preview_fields",
    "script_sha256",
    "script_too_large_message",
]

#: The most script text, in UTF-8 bytes, one page of the approval preview holds.
SCRIPT_SHOWN_MAX_BYTES: Final[int] = 65_536

#: The longest script, in UTF-8 bytes, that ``linux.script.run`` accepts: 16 pages.
SCRIPT_MAX_BYTES: Final[int] = 1_048_576

SCRIPT_CHECK_MATCHES: Final[str] = "matches"
SCRIPT_CHECK_MISMATCH: Final[str] = "does not match"

_UNAVAILABLE_REASON: Final[str] = (
    "MEHO could not check the script for values that look like secrets, so it is not shown"
)


def script_sha256(script: str) -> str:
    """The SHA-256 hex digest of *script* in UTF-8."""
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def script_matches_expected(script: str, expected: Any) -> bool | None:
    """Whether *script* has the SHA-256 *expected*; ``None`` when none was sent.

    The comparison ignores upper and lower case and spaces around the digest.
    A value that is not a string never matches.
    """
    if expected is None:
        return None
    if not isinstance(expected, str):
        return False
    return expected.strip().lower() == script_sha256(script)


def _hide_secret_values(script: str) -> tuple[str, bool]:
    """*script* with the values that look like secrets replaced, and whether any was."""
    scrubbed = scrub_secret_fields_in_text(hide_shell_script(script))
    result = redact(scrubbed, get_default_policy())
    shown = result.redacted
    if not isinstance(shown, str):  # pragma: no cover -- str in, str out
        raise TypeError("redaction returned a non-string for a string")
    return shown, shown != script or bool(result.manifest)


def script_too_large_message(size: int) -> str:
    """The plain refusal text for a script of *size* bytes (more than the limit)."""
    return (
        f"The script is {size:,} bytes. MEHO accepts at most {SCRIPT_MAX_BYTES:,} bytes "
        "(1 MiB) for linux.script.run. Keep the script shorter, for example copy a "
        "large file to the host first."
    )


class ScriptPageOutOfRangeError(ValueError):
    """The page asked for is not one of the pages of the script."""

    def __init__(self, page: int, pages: int) -> None:
        super().__init__(f"The script has {pages} page(s). Page {page} does not exist.")
        self.page = page
        self.pages = pages


def _pages(text: str, page_bytes: int) -> list[str]:
    """*text* cut into pages of at most *page_bytes* UTF-8 bytes.

    A border never splits a UTF-8 character, and the pages joined are *text*.
    An empty text is one empty page.
    """
    data = text.encode("utf-8")
    if len(data) <= page_bytes:
        return [text]
    pages: list[str] = []
    start = 0
    while start < len(data):
        end = min(start + page_bytes, len(data))
        # 0b10xxxxxx is the middle of a character: step back to its start.
        while end < len(data) and (data[end] & 0xC0) == 0x80:
            end -= 1
        pages.append(data[start:end].decode("utf-8"))
        start = end
    return pages


def script_page_fields(script: str, page: int = 1) -> dict[str, Any]:
    """The script fields for page *page* (from 1) of *script*.

    Values that look like secrets are hidden over the whole script first, then
    the hidden text is cut into pages, so a secret that crosses a page border
    is hidden. ``script_sha256`` is the hash of the whole script, the same on
    every page, and left out when values were hidden. Raises
    :class:`ScriptPageOutOfRangeError` when *page* is not a page of the text.
    """
    fields: dict[str, Any] = {"script_bytes": len(script.encode("utf-8"))}
    try:
        shown, hidden = _hide_secret_values(script)
    except Exception:
        # Fail closed: without the redaction pass, show no text and no hash.
        if page != 1:
            raise ScriptPageOutOfRangeError(page, 1) from None
        fields["script_unavailable"] = _UNAVAILABLE_REASON
        return fields
    pages = _pages(shown, SCRIPT_SHOWN_MAX_BYTES)
    if not 1 <= page <= len(pages):
        raise ScriptPageOutOfRangeError(page, len(pages))
    fields["script"] = pages[page - 1]
    fields["script_pages"] = len(pages)
    if hidden:
        fields["script_values_hidden"] = True
    else:
        fields["script_sha256"] = script_sha256(script)
    return fields


def script_preview_fields(params: dict[str, Any]) -> dict[str, Any]:
    """The script fields of the ``linux.script.run`` approval preview.

    *params* must hold ``script`` as a string. See the module docstring for
    the fields.
    """
    script: str = params["script"]
    fields = script_page_fields(script, 1)
    if fields.get("script_pages", 1) > 1:
        fields["script_truncated"] = True
    script_ref = params.get("script_ref")
    if isinstance(script_ref, str) and script_ref:
        fields["script_ref"] = script_ref
    matches = script_matches_expected(script, params.get("expected_sha256"))
    if matches is not None:
        fields["script_check"] = SCRIPT_CHECK_MATCHES if matches else SCRIPT_CHECK_MISMATCH
    return fields
