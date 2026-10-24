# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The one rule for showing a licence key: only its last 5 characters.

A licence key is a secret. Every read that returns licence keys shows each
key through :func:`mask_license_key`, so all of them show a key the same way.
Today these are ``vrops.license.info`` and ``sddc.license.list``.

Masking the key in the result is not enough on its own: the flight recorder
records the raw vendor answer before the handler masks it. So each of these
operations also carries the tag ``license-key``. Its ``key`` token is a secret
tag token
(:data:`~meho_backplane.redaction.flight_recorder.families.SECRET_FAMILY_TAGS`),
so the flight recorder stores no body of the operation's vendor calls.
"""

from __future__ import annotations

from typing import Final

__all__ = ["mask_license_key"]

#: How many characters of a key stay visible.
_VISIBLE_KEY_CHARS: Final = 5


def mask_license_key(key: object) -> str | None:
    """Return *key* with only its last 5 characters visible.

    Every character before them becomes ``*``, except a dash. So a key in the
    usual five-group shape keeps its shape: ``*****-*****-*****-*****-AB12C``.
    A key of 10 characters or fewer is hidden in full. A value that is not a
    non-empty string gives ``None``.
    """
    if not isinstance(key, str) or not key:
        return None
    if len(key) <= 2 * _VISIBLE_KEY_CHARS:
        return "*" * len(key)
    head, tail = key[:-_VISIBLE_KEY_CHARS], key[-_VISIBLE_KEY_CHARS:]
    return "".join(char if char == "-" else "*" for char in head) + tail
