# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the approver of ``linux.script.run`` reads about the script.

For ``linux.script.run`` the script body is the change that waits for
approval, so the park-time preview shows it. The fields are:

* ``script`` -- the script text. Values that look like secrets (a
  ``DB_PASSWORD=...`` pair, a bearer token, a JWT, a private key block, the
  password in a URL, a quoted ``"password": "..."`` pair) are replaced by a
  marker, with the same packaged default redaction policy that MEHO uses for
  error text. The text is cut at :data:`SCRIPT_SHOWN_MAX_BYTES`.
* ``script_bytes`` -- the size of the full script in UTF-8 bytes.
* ``script_sha256`` -- the SHA-256 of the full script as it will run, so the
  approver can compare it with a reviewed copy. Left out when values were
  hidden: with the rest of the text in view, the hash would let a reader test
  guesses of the hidden value.
* ``script_values_hidden`` / ``script_truncated`` -- ``True`` when the shown
  text differs from the script that runs.
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

from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.resolver import get_default_policy
from meho_backplane.redaction.secret_fields import scrub_secret_fields_in_text

__all__ = [
    "SCRIPT_CHECK_MATCHES",
    "SCRIPT_CHECK_MISMATCH",
    "SCRIPT_SHOWN_MAX_BYTES",
    "script_matches_expected",
    "script_preview_fields",
    "script_sha256",
]

#: The most script text, in UTF-8 bytes, the approval preview holds.
SCRIPT_SHOWN_MAX_BYTES: Final[int] = 65_536

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
    scrubbed = scrub_secret_fields_in_text(script)
    result = redact(scrubbed, get_default_policy())
    shown = result.redacted
    if not isinstance(shown, str):  # pragma: no cover -- str in, str out
        raise TypeError("redaction returned a non-string for a string")
    return shown, scrubbed != script or bool(result.manifest)


def _cut(text: str, max_bytes: int) -> tuple[str, bool]:
    """*text* cut to at most *max_bytes* UTF-8 bytes, and whether it was cut."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False
    return data[:max_bytes].decode("utf-8", "ignore"), True


def script_preview_fields(params: dict[str, Any]) -> dict[str, Any]:
    """The script fields of the ``linux.script.run`` approval preview.

    *params* must hold ``script`` as a string. See the module docstring for
    the fields.
    """
    script: str = params["script"]
    fields: dict[str, Any] = {"script_bytes": len(script.encode("utf-8"))}
    try:
        shown, hidden = _hide_secret_values(script)
    except Exception:
        # Fail closed: without the redaction pass, show no text and no hash.
        fields["script_unavailable"] = _UNAVAILABLE_REASON
    else:
        shown, truncated = _cut(shown, SCRIPT_SHOWN_MAX_BYTES)
        fields["script"] = shown
        if hidden:
            fields["script_values_hidden"] = True
        else:
            fields["script_sha256"] = script_sha256(script)
        if truncated:
            fields["script_truncated"] = True
    script_ref = params.get("script_ref")
    if isinstance(script_ref, str) and script_ref:
        fields["script_ref"] = script_ref
    matches = script_matches_expected(script, params.get("expected_sha256"))
    if matches is not None:
        fields["script_check"] = SCRIPT_CHECK_MATCHES if matches else SCRIPT_CHECK_MISMATCH
    return fields
