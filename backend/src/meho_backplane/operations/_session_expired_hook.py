# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector can tell MEHO that an answer means "the login session ended".

Most products answer an ended session with 401, and the dispatcher already
signs in again for that status. Some products answer with another status
(for example 403) and a body of their own. A plain "every 403 means the
session ended" would be wrong: a real 403 means missing rights, and more
sign-in tries can lock an account.

So a connector may have an optional method::

    def is_session_expired(self, response: httpx.Response) -> bool: ...

The dispatcher asks it when a status is not one of the known session-ended
statuses. The connector looks at the status and the body. A connector
without the method, or a method that raises, answers "no", and the status
is handled as before.

When the answer is yes, the dispatcher drops the kept login, signs in again
and repeats the call once (``dispatcher._retry_after_expired_session_answer``).
It never loops. If the fresh sign-in is refused, or the repeated call gets
the same answer, the caller gets a clear error that says the product refused
the sign-in (wrong password or locked account).

This module holds the pieces of that path that need no dispatcher state.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.flight_recorder.typed import record_typed_call, typed_span_start

__all__ = [
    "REFUSED_SIGNIN_REMEDIATION",
    "mark_session_relogin",
    "product_refused_signin_error",
    "session_expired_by_hook",
    "with_refused_signin_remediation",
]

_log = structlog.get_logger(__name__)

#: What to do when the product refused the fresh sign-in. It names the
#: sign-in, not a missing permission.
REFUSED_SIGNIN_REMEDIATION = (
    "the product refused the sign-in: the password is wrong or the account is "
    "locked. MEHO signed in once for this call and did not try again, so no "
    "more tries can lock the account. Check the stored credential and the "
    "account on the product, then retry."
)


def session_expired_by_hook(connector_instance: Any, response: httpx.Response) -> bool:
    """Whether the connector says *response* means that its login session ended.

    ``False`` when the connector has no ``is_session_expired`` method, when
    the method answers anything but ``True``, and when it raises. A broken
    hook must never turn a real 403 into a sign-in loop.
    """
    hook = getattr(connector_instance, "is_session_expired", None)
    if not callable(hook):
        return False
    try:
        return hook(response) is True
    except Exception:
        _log.warning(
            "session_expired_hook_failed",
            connector=type(connector_instance).__name__,
            exc_info=True,
        )
        return False


def mark_session_relogin(status_code: int) -> None:
    """Show on the audit row and in the trace that MEHO signed in again.

    The audit row gets ``session_relogin`` and ``session_relogin_status`` in
    its payload (through the ``audit_*`` binds, which the dispatcher drops
    when the call ends). The trace gets one span. Neither carries a vendor
    body or a credential.
    """
    structlog.contextvars.bind_contextvars(
        audit_session_relogin=True,
        audit_session_relogin_status=status_code,
    )
    record_typed_call(
        typed_span_start("session_relogin"),
        extra={"session_relogin": True, "rejected_status": status_code},
    )


def product_refused_signin_error(
    retry_exc: httpx.HTTPStatusError, target: Any
) -> ConnectorAuthError:
    """The error for a repeated call that got the same "session ended" answer.

    The sign-in itself worked, yet the product ended the new session at once.
    Same fix as a refused sign-in: check the credential and the account.
    """
    status = retry_exc.response.status_code
    error = ConnectorAuthError(
        f"the product answered HTTP {status} again after a fresh sign-in",
        status_code=status,
        cause=f"session_dispatch_{status}_after_relogin",
        target_name=getattr(target, "name", None),
        remediation=REFUSED_SIGNIN_REMEDIATION,
        stage="after_relogin",
    )
    # The result builder reads the upstream body from the chained error.
    error.__cause__ = retry_exc
    return error


def with_refused_signin_remediation(error: ConnectorAuthError) -> ConnectorAuthError:
    """Give a refused fresh sign-in the clear text, unless the connector has its own."""
    if not error.remediation:
        error.remediation = REFUSED_SIGNIN_REMEDIATION
    return error
