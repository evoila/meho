# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Proof that a person is present when they approve on the command line or REST.

An approve is a person's decision (v0.1-spec §7). In the web interface the
click is the proof. On the command line and REST the token must show a
fresh sign-in: its ``auth_time`` claim is at most
:attr:`~meho_backplane.settings.Settings.approval_fresh_sign_in_max_age_seconds`
old (default 5 minutes). A token whose ``acr`` is one of
:attr:`~meho_backplane.settings.Settings.approval_one_time_code_acr_values`
is recorded as a one-time code step; it needs the same recent
``auth_time``. A token without ``auth_time`` never approves (#3979,
``docs/decisions/approval-groups-and-self-approval.md``).

A refreshed token keeps the time of the first sign-in (RFC 9470 §6.1), so a
long-lived ``meho login`` token cannot pass. The refusal is ``401`` with the
RFC 9470 §3 challenge, so the command line knows to open a new browser
sign-in.

This module only reads the token. The rule "one sign-in allows one
approval" needs the audit log, so it lives in
:func:`~meho_backplane.operations.approval_queue.approve_request`.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Final, Literal

import structlog
from fastapi import HTTPException, status

from meho_backplane.auth.operator import Operator
from meho_backplane.settings import Settings

__all__ = [
    "FRESH_SIGN_IN_REMEDIATION",
    "ApprovalAuth",
    "check_fresh_approval_sign_in",
    "fresh_sign_in_required",
    "web_click_approval_auth",
]

ApprovalChannel = Literal["rest", "web"]
ApprovalMethod = Literal["fresh_sign_in", "one_time_code", "web_click"]

#: The text a refused approve carries after its machine-readable prefix.
FRESH_SIGN_IN_REMEDIATION: Final[str] = (
    "approve in the web interface, or sign in again: an approve on the "
    "command line or REST needs a fresh sign-in or a one-time code. "
    "`meho approvals approve <id>` opens the browser sign-in for you. "
    "A plain API token never approves."
)


@dataclass(frozen=True)
class ApprovalAuth:
    """How a person proved they were present for one approve.

    ``channel`` is where the approve came from, ``method`` how the person
    proved presence, ``auth_time`` the sign-in time from the token (or
    ``None``). ``sid`` is the Keycloak session id; it is never written to
    the audit row as is, only as part of :meth:`sign_in_key`.
    """

    channel: ApprovalChannel
    method: ApprovalMethod
    auth_time: int | None
    sid: str | None = None

    @property
    def single_use(self) -> bool:
        """Whether this sign-in may approve only once (every token approve)."""
        return self.method != "web_click"

    def sign_in_key(self, sub: str) -> str | None:
        """A short hash that names one sign-in of *sub*, or ``None`` for a click.

        Built from ``sub``, ``sid`` and ``auth_time``. The audit row keeps
        the hash, not the session id, and a second approve with the same
        hash is refused.
        """
        if not self.single_use:
            return None
        raw = f"{sub}\n{self.sid or ''}\n{self.auth_time}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def audit_payload(self, sub: str) -> dict[str, Any]:
        """The ``approval_auth`` block of the ``approval.decision`` audit row."""
        payload: dict[str, Any] = {
            "channel": self.channel,
            "method": self.method,
            "auth_time": self.auth_time,
        }
        key = self.sign_in_key(sub)
        if key is not None:
            payload["sign_in_key"] = key
        return payload


def web_click_approval_auth(operator: Operator) -> ApprovalAuth:
    """The proof for an approve given with a click in the web interface."""
    return ApprovalAuth(channel="web", method="web_click", auth_time=operator.auth_time)


def fresh_sign_in_required(settings: Settings, *, reason: str) -> HTTPException:
    """Build the ``401`` that asks for a fresh sign-in (RFC 9470 §3).

    ``max_age`` tells the client how old the new sign-in may be, in
    seconds. *reason* is the machine-readable prefix of ``detail``.
    """
    max_age = settings.approval_fresh_sign_in_max_age_seconds
    challenge = (
        'Bearer error="insufficient_user_authentication", '
        'error_description="A fresh sign-in is required to approve", '
        f'max_age="{max_age}"'
    )
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"{reason}: {FRESH_SIGN_IN_REMEDIATION}",
        headers={"WWW-Authenticate": challenge},
    )


def check_fresh_approval_sign_in(
    operator: Operator,
    settings: Settings,
    *,
    now: float | None = None,
) -> ApprovalAuth:
    """Return the proof for a token approve, or raise the ``401`` challenge.

    The sign-in must be at most ``approval_fresh_sign_in_max_age_seconds``
    old. A sign-in time further in the future than the JWT clock leeway is
    refused too. A token with no ``auth_time`` is refused (fail closed).
    """
    current = time.time() if now is None else now
    auth_time = operator.auth_time
    log = structlog.get_logger(__name__)
    if auth_time is None:
        log.warning("approval_sign_in_missing", operator_sub=operator.sub)
        raise fresh_sign_in_required(settings, reason="fresh_sign_in_required")
    age = current - auth_time
    max_age = settings.approval_fresh_sign_in_max_age_seconds
    if age > max_age or age < -settings.keycloak_jwt_leeway_seconds:
        log.warning(
            "approval_sign_in_too_old",
            operator_sub=operator.sub,
            age_seconds=int(age),
            max_age_seconds=max_age,
        )
        raise fresh_sign_in_required(settings, reason="fresh_sign_in_required")
    one_time_code = operator.acr is not None and (
        operator.acr in settings.approval_one_time_code_acr_values
    )
    return ApprovalAuth(
        channel="rest",
        method="one_time_code" if one_time_code else "fresh_sign_in",
        auth_time=auth_time,
        sid=operator.sid,
    )
