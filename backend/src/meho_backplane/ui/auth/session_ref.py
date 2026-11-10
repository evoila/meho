# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Short stand-in for a console session id, for logs and the Account page.

The console session cookie holds the ``web_session`` row id, and that id
alone opens the session. So MEHO never writes the id into a log line or
a page. It writes :func:`session_ref` instead: the first 12 hex
characters of an HMAC-SHA256 of the id, keyed with the server's
``UI_SESSION_ENCRYPTION_KEY``.

* The same session always gets the same ref, so log lines of one
  session still match each other and the row on the Account page.
* The ref cannot be turned back into the id without the server key, so
  it does not work as a cookie.

The Account page uses the ref as the per-row handle of its revoke form.
The revoke route finds the row by that ref among the caller's own
sessions only (:mod:`meho_backplane.ui.routes.account`).
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Final

from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.session_store import EncryptionKeyMissingError

__all__ = ["SESSION_REF_LENGTH", "session_ref"]

#: Hex characters kept from the HMAC: 48 bits, plenty to tell one
#: person's sessions apart.
SESSION_REF_LENGTH: Final[int] = 12

#: Domain label so this HMAC never equals another use of the same key
#: (the CSRF token and the run token use it too).
_DOMAIN: Final[bytes] = b"meho-web-session-ref\x00"


def session_ref(session_id: uuid.UUID) -> str:
    """Return the short, stable stand-in for a console session id.

    Raises :class:`EncryptionKeyMissingError` when
    ``UI_SESSION_ENCRYPTION_KEY`` is unset. No console session can exist
    without that key, so a caller that holds a session id has it.
    """
    key = get_settings().ui_session_encryption_key
    if not key:
        raise EncryptionKeyMissingError(
            "UI_SESSION_ENCRYPTION_KEY is empty; MEHO cannot make the session ref."
        )
    digest = hmac.new(
        key.encode("utf-8"), _DOMAIN + str(session_id).encode("ascii"), hashlib.sha256
    )
    return digest.hexdigest()[:SESSION_REF_LENGTH]
