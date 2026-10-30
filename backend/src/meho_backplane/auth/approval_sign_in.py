# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Proof that a person is present when they approve on the command line or REST.

An approve is a person's decision (v0.1-spec §7). In the web interface the
click is the proof. On the command line and REST the token must show a
fresh sign-in: the shared check
:func:`~meho_backplane.auth.fresh_sign_in.has_fresh_sign_in` must pass. It
is the same check, with the same ``FRESH_SIGN_IN_MAX_AGE_SECONDS`` window
(default 5 minutes), that turning approval off for an operation uses
(#4711). A token whose ``acr`` is one of
:attr:`~meho_backplane.settings.Settings.approval_one_time_code_acr_values`
is recorded as a one-time code step; it needs the same fresh sign-in. A
token without ``auth_time`` never approves (#3979,
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
from dataclasses import dataclass
from typing import Any, Final, Literal

import structlog
from fastapi import HTTPException

from meho_backplane.auth.fresh_sign_in import fresh_sign_in_challenge, has_fresh_sign_in
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


def fresh_sign_in_required(*, reason: str) -> HTTPException:
    """Build the ``401`` that asks for a fresh sign-in (RFC 9470 §3).

    The shared :func:`~meho_backplane.auth.fresh_sign_in.fresh_sign_in_challenge`
    builds it. *reason* is the machine-readable prefix of ``detail``.
    """
    return fresh_sign_in_challenge(f"{reason}: {FRESH_SIGN_IN_REMEDIATION}")


def check_fresh_approval_sign_in(
    operator: Operator,
    settings: Settings,
    *,
    now: float | None = None,
) -> ApprovalAuth:
    """Return the proof for a token approve, or raise the ``401`` challenge.

    The shared :func:`~meho_backplane.auth.fresh_sign_in.has_fresh_sign_in`
    decides: a person whose sign-in is at most
    ``FRESH_SIGN_IN_MAX_AGE_SECONDS`` old. A token with no ``auth_time`` is
    refused (fail closed).
    """
    if not has_fresh_sign_in(operator, now=now):
        structlog.get_logger(__name__).warning(
            "approval_sign_in_not_fresh",
            operator_sub=operator.sub,
            has_auth_time=operator.auth_time is not None,
        )
        raise fresh_sign_in_required(reason="fresh_sign_in_required")
    one_time_code = operator.acr is not None and (
        operator.acr in settings.approval_one_time_code_acr_values
    )
    return ApprovalAuth(
        channel="rest",
        method="one_time_code" if one_time_code else "fresh_sign_in",
        auth_time=operator.auth_time,
        sid=operator.sid,
    )
