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

The dispatcher asks it only for a 4xx status that is not one of the known
session-ended statuses. After a 5xx or a timeout the call may have run, so
it is never asked and never repeated. The connector looks at the status and
the body. A connector without the method, or a method that raises, answers
"no", and the status is handled as before.

When the answer is yes, the dispatcher drops the kept login. It repeats the
call once only when that cannot send a change twice
(:func:`may_resend_after_session_end`): the operation's risk level is
``safe``, or it is an ingested operation that sent exactly one request
(that request was refused, so nothing ran). A call that runs after a
person's approval is repeated only when it is ``safe``: one approval is
one run of a change. In every other case the caller gets
``connector_auth_failed`` that says the session ended and the change may
or may not have been made.

The repeat never loops. If the fresh sign-in is refused, the caller gets an
error that says the product refused the sign-in (wrong password or locked
account). If the repeated call gets the same answer, the error says that
the product took the new sign-in and still ended the session.

This module holds the pieces of that path that need no dispatcher state.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.flight_recorder.typed import record_typed_call, typed_span_start
from meho_backplane.operations._branches import _profile_pagination
from meho_backplane.operations.composite import approved_run_var
from meho_backplane.operations.uncertain_outcome import LOGIN_ENDED_ADVICE, changes_target

__all__ = [
    "NOT_RESENT_REMEDIATION",
    "REFUSED_SIGNIN_REMEDIATION",
    "SAME_ANSWER_REMEDIATION",
    "drop_login_before_approved_change",
    "mark_session_relogin",
    "may_resend_after_session_end",
    "same_answer_after_signin_error",
    "session_ended_not_resent_error",
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

#: What to do when the product took the fresh sign-in and still gave the
#: same "session ended" answer.
SAME_ANSWER_REMEDIATION = (
    "MEHO signed in again and the product accepted the new sign-in, but it "
    'gave the same "session ended" answer again. MEHO did not try a third '
    "time. The account may be locked or not allowed to start a session on "
    "the product. Check the account and the stored credential on the "
    "product, then retry."
)

#: What to do when the session ended during an operation that MEHO does not
#: send again, because it may already have changed the product.
NOT_RESENT_REMEDIATION = (
    "the product said that the login session ended. MEHO dropped the kept "
    "session, so the next call signs in again. MEHO did not send this "
    "operation again, because it may already have changed the product. "
    f"{LOGIN_ENDED_ADVICE}"
)

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def session_expired_by_hook(connector_instance: Any, response: httpx.Response) -> bool:
    """Whether the connector says *response* means that its login session ended.

    ``False`` for a status that is not 4xx, when the connector has no
    ``is_session_expired`` method, when the method answers anything but
    ``True`` (a warning is logged when the answer is not a bool), and when it
    raises (a warning is logged too). A broken
    hook must never turn a real 403 into a sign-in loop.
    """
    if not 400 <= response.status_code < 500:
        # After a 5xx the call may have run: never a reason to sign in again.
        return False
    hook = getattr(connector_instance, "is_session_expired", None)
    if not callable(hook):
        return False
    try:
        answer = hook(response)
    except Exception:
        _log.warning(
            "session_expired_hook_failed",
            connector=type(connector_instance).__name__,
            exc_info=True,
        )
        return False
    if not isinstance(answer, bool):
        _log.warning(
            "session_expired_hook_bad_answer",
            connector=type(connector_instance).__name__,
            answer_type=type(answer).__name__,
        )
    return answer is True


def may_resend_after_session_end(
    descriptor: Any, connector_instance: Any, rejected: httpx.Request
) -> bool:
    """Whether MEHO may run the operation again after "the session ended".

    Yes only when a repeat cannot send a change twice:

    * the operation's risk level is ``safe`` (it changes nothing), or
    * it is an ingested operation that sent exactly one request. That one
      request was refused, so nothing ran. A write is always one request;
      a read sends more than one only when the connector follows pages.

    A ``safe`` operation is repeated also in a run after a person's
    approval (for example a read step of an approved multi-step operation).
    Any other call after an approval is never repeated: the approval was
    spent on the first run. A typed handler or a multi-step operation may
    have sent a write before the refused request, so it is not repeated
    unless it is ``safe``.
    """
    if descriptor.safety_level == "safe":
        return True
    if approved_run_var.get():
        return False
    if descriptor.source_kind != "ingested":
        return False
    return rejected.method not in _READ_METHODS or _profile_pagination(connector_instance) is None


async def drop_login_before_approved_change(
    descriptor: Any, connector_instance: Any, target: Any
) -> None:
    """Drop the kept login of *target* before MEHO sends an approved change.

    A change that waited for a person can meet a login that ended while it
    waited. So an approved run of an operation that changes something starts
    with a fresh login: the connector's ``invalidate_session`` hook drops
    the kept one, and the call logs in again. A read, and a change that did
    not wait for a person, keep the kept login. A connector without the hook
    has nothing to drop. The hook is duck-typed (``getattr``) like the other
    session hooks, and is called with the target only (a connector's
    ``rejected`` argument is optional), so a hook that takes only the target
    works too.

    A hook that raises is logged and the call goes on: the change is sent
    once, and a refused login is then stored as an unknown outcome.
    """
    if not changes_target(descriptor.safety_level):
        return
    hook = getattr(connector_instance, "invalidate_session", None)
    if not callable(hook):
        return
    try:
        await hook(target)
    except Exception:
        _log.warning(
            "approved_change_login_drop_failed",
            connector=type(connector_instance).__name__,
            exc_info=True,
        )


def mark_session_relogin(status_code: int, *, resent: bool) -> None:
    """Show on the audit row and in the trace what MEHO did after "session ended".

    The audit row gets ``session_relogin`` (MEHO dropped the kept login, so
    the next call signs in again; always true here, also when the operation
    was not sent again), ``session_resent`` (MEHO sent the operation again),
    ``result_unknown`` (true when MEHO did not send it again: the change may
    or may not have been made) and ``session_relogin_status`` (the status
    that said the session ended), in
    its payload (through the ``audit_*`` binds, which the dispatcher drops
    when the call ends). The trace gets one ``session_relogin`` span. Neither
    carries a vendor body or a credential.
    """
    structlog.contextvars.bind_contextvars(
        audit_session_relogin=True,
        audit_session_resent=resent,
        audit_result_unknown=not resent,
        audit_session_relogin_status=status_code,
    )
    record_typed_call(
        typed_span_start("session_relogin"),
        extra={"session_relogin": True, "resent": resent, "rejected_status": status_code},
    )


def same_answer_after_signin_error(
    retry_exc: httpx.HTTPStatusError, target: Any
) -> ConnectorAuthError:
    """The error for a repeated call that got the same "session ended" answer.

    The sign-in itself worked, yet the product ended the new session at once.
    """
    status = retry_exc.response.status_code
    error = ConnectorAuthError(
        f"the product answered HTTP {status} again after a fresh sign-in",
        status_code=status,
        cause=f"session_dispatch_{status}_after_relogin",
        target_name=getattr(target, "name", None),
        remediation=SAME_ANSWER_REMEDIATION,
        stage="after_relogin",
    )
    # The result builder reads the upstream body from the chained error.
    error.__cause__ = retry_exc
    return error


def session_ended_not_resent_error(
    rejected_exc: httpx.HTTPStatusError, target: Any
) -> ConnectorAuthError:
    """The error for "the session ended" on an operation MEHO does not repeat."""
    status = rejected_exc.response.status_code
    error = ConnectorAuthError(
        f"the product said the session ended (HTTP {status}); the operation was not sent again",
        status_code=status,
        cause=f"session_ended_{status}_not_resent",
        target_name=getattr(target, "name", None),
        remediation=NOT_RESENT_REMEDIATION,
        stage="call",
    )
    error.__cause__ = rejected_exc
    return error


def with_refused_signin_remediation(error: ConnectorAuthError) -> ConnectorAuthError:
    """Give a refused fresh sign-in the clear text, unless the connector has its own."""
    if not error.remediation:
        error.remediation = REFUSED_SIGNIN_REMEDIATION
    return error
