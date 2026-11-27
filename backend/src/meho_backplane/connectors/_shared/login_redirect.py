# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The error of a connector login that got a redirect MEHO did not follow."""

from __future__ import annotations

from meho_backplane.connectors._shared.vcf_auth import SessionLoginError
from meho_backplane.connectors.base import ConnectorOwnMessage

__all__ = ["SessionLoginRedirectError"]


class SessionLoginRedirectError(SessionLoginError, ConnectorOwnMessage):
    """A named login got a redirect that MEHO did not follow.

    A login follows a redirect on the same origin, as a read does. It ends
    on any other redirect: to another origin, or without a usable
    ``Location``. The pooled client raises this error only with the switch
    ``redirect_on_change_result`` on. With the switch off, the login code
    reads the redirect answer and words its own error.

    The message is a fixed sentence of MEHO with only the HTTP status. It
    names no address: a login address can come from the credential (an
    external token URL). It holds no text from the vendor and no value of
    the call, so the dispatcher keeps it also for an operation that sends a
    secret. It says nothing about the operation: a login can also run again
    after the operation was sent once (a new login after a 401).
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(
            f"The login of this connector got HTTP {status_code}, a redirect. "
            "MEHO did not follow it, so the login did not finish."
        )
