# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The error of a connector login that got a redirect MEHO did not follow."""

from __future__ import annotations

from meho_backplane.connectors._shared.vcf_auth import SessionLoginError
from meho_backplane.connectors.base import ConnectorOwnMessage

__all__ = ["SessionLoginRedirectError"]


class SessionLoginRedirectError(SessionLoginError, ConnectorOwnMessage):
    """The login got a redirect that MEHO did not follow, so the operation was not sent.

    A login follows a redirect on the same origin, as a read does. It ends
    here on any other redirect: to another origin, or without a usable
    ``Location``. The error is the same with the
    switch ``redirect_on_change_result`` off and on.

    The message is a fixed sentence of MEHO with only the HTTP status. It
    names no address: a login address can come from the credential (an
    external token URL). It holds no text from the vendor and no value of
    the call, so the dispatcher keeps it also for an operation that sends a
    secret.
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(
            f"The login of this connector got HTTP {status_code}, a redirect. "
            "MEHO did not follow it and did not send the operation."
        )
