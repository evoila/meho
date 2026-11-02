# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Proof that a person is present when they approve.

An approve is a person's decision (v0.1-spec §7). On every surface the
person's sign-in must be recent and must have asked for their
credentials. On the command line and REST the token must come from the
person's own approval sign-in (#5515):

* the token's ``azp`` claim is the approval client
  (:attr:`~meho_backplane.settings.Settings.keycloak_cli_approve_client_id`,
  default ``meho-cli-approve``), and the principal is a person. A stored
  ``meho login`` token, an MCP token or any other token gets the same
  ``401`` as an old sign-in, whatever its ``auth_time``;
* the sign-in is young enough for the request: the token's ``auth_time``
  is at most ``FRESH_SIGN_IN_MAX_AGE_SECONDS`` old (the approval window,
  default 15 minutes), or for a request whose operation is ``destructive``
  at most ``PER_ACTION_SIGN_IN_MAX_AGE_SECONDS`` old (default five
  minutes). Each risk level has its own limit; the window does not cap
  the destructive limit, and the destructive limit does not cap the
  window. A token without ``auth_time`` never approves;
* the sign-in asked for the person's credentials: the token's ``acr`` is
  set and is not ``"0"``. With no level-of-authentication mapping,
  Keycloak gives ``"1"`` to a sign-in that asked for the password and
  ``"0"`` to one that did not. The approval client's own sign-in flow
  always asks, so its tokens carry ``"1"`` (or a higher level).

In the web interface the console session's own token is the proof
(:func:`check_fresh_web_approval_sign_in`). The browser never holds that
token: the console got it from the console login (the ``meho-web``
client), so there is no client check. The age and ``acr`` checks are the
same as on REST. When the sign-in is too old, the console sends the
person to a new login with ``prompt=login`` and ``max_age=0``, which asks
for the credentials also in a remembered browser.

Inside the window one sign-in may approve several requests. A request
whose operation is ``destructive`` is stricter: its sign-in must not have
approved anything else. The age check and that rule need the request (its
risk level) and the audit log, so they live in
:func:`~meho_backplane.operations.approval_queue.approve_request`.
:func:`check_fresh_approval_sign_in` and
:func:`check_fresh_web_approval_sign_in` run before the request is read;
they refuse a sign-in that is too old for both limits.

A refreshed token keeps the time of the first sign-in (RFC 9470 §6.1), so
renewing a token never makes the window longer. The refusal is ``401``
with the RFC 9470 §3 challenge, so the command line knows to open a new
browser sign-in. A token whose ``acr`` is one of
:attr:`~meho_backplane.settings.Settings.approval_one_time_code_acr_values`
is recorded as a one-time code step; it needs the same checks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Final, Literal

import structlog
from fastapi import HTTPException

from meho_backplane.auth.fresh_sign_in import fresh_sign_in_challenge, has_fresh_sign_in
from meho_backplane.auth.operator import Operator, is_human_principal
from meho_backplane.settings import Settings, get_settings

__all__ = [
    "FRESH_SIGN_IN_REMEDIATION",
    "WEB_FRESH_SIGN_IN_REMEDIATION",
    "ApprovalAuth",
    "approval_sign_in_max_age",
    "asked_for_credentials",
    "check_fresh_approval_sign_in",
    "check_fresh_web_approval_sign_in",
    "fresh_sign_in_required",
    "has_fresh_web_approval_sign_in",
    "is_approval_sign_in",
]

ApprovalChannel = Literal["rest", "web"]
ApprovalMethod = Literal["fresh_sign_in", "one_time_code"]

#: The ``acr`` Keycloak gives a sign-in that did not ask for credentials.
_NO_CREDENTIAL_ACR: Final[str] = "0"

#: The text a refused approve carries after its machine-readable prefix.
FRESH_SIGN_IN_REMEDIATION: Final[str] = (
    "sign in again: an approve needs a fresh approval sign-in. "
    "`meho approvals approve <id>` opens the browser sign-in for you. "
    "A stored `meho login` token, an MCP token or an API token never approves."
)

#: The text the web interface shows when the console sign-in is too old.
WEB_FRESH_SIGN_IN_REMEDIATION: Final[str] = (
    "Sign in again to approve. An approval needs a recent sign-in that asked "
    "for your password, and a destructive operation needs a sign-in of its own."
)


@dataclass(frozen=True)
class ApprovalAuth:
    """How a person proved they were present for one approve.

    ``channel`` is where the approve came from, ``method`` how the person
    proved presence, ``auth_time`` the sign-in time from the token, ``acr``
    how the person signed in (the token's ``acr`` claim, or ``None``).
    ``sid`` is the Keycloak session id; it is never written to the audit
    row as is, only as part of :meth:`sign_in_key`.
    """

    channel: ApprovalChannel
    method: ApprovalMethod
    auth_time: int
    sid: str | None = None
    acr: str | None = None

    def sign_in_key(self, sub: str) -> str:
        """A short hash that names one sign-in of *sub*.

        Built from ``sub``, ``sid`` and ``auth_time``. The audit row keeps
        the hash, not the session id. The ``destructive`` rule uses it to
        find what else this sign-in approved.
        """
        raw = f"{sub}\n{self.sid or ''}\n{self.auth_time}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def audit_payload(self, sub: str, *, single_use: bool = False) -> dict[str, Any]:
        """The ``approval_auth`` block of the ``approval.decision`` audit row.

        *single_use* marks an approve that used the sign-in alone (a
        ``destructive`` request): the sign-in approves nothing more.
        """
        payload: dict[str, Any] = {
            "channel": self.channel,
            "method": self.method,
            "auth_time": self.auth_time,
            "acr": self.acr,
            "sign_in_key": self.sign_in_key(sub),
        }
        if single_use:
            payload["single_use"] = True
        return payload


def fresh_sign_in_required(*, reason: str, max_age_seconds: int | None = None) -> HTTPException:
    """Build the ``401`` that asks for a fresh sign-in (RFC 9470 §3).

    The shared :func:`~meho_backplane.auth.fresh_sign_in.fresh_sign_in_challenge`
    builds it. *reason* is the machine-readable prefix of ``detail``.
    *max_age_seconds* is the ``max_age`` of the challenge; it defaults to
    the approval window (``FRESH_SIGN_IN_MAX_AGE_SECONDS``).
    """
    if max_age_seconds is None:
        max_age_seconds = get_settings().fresh_sign_in_max_age_seconds
    return fresh_sign_in_challenge(
        f"{reason}: {FRESH_SIGN_IN_REMEDIATION}", max_age_seconds=max_age_seconds
    )


def is_approval_sign_in(operator: Operator, settings: Settings) -> bool:
    """Whether the token comes from a person's approval sign-in (#5515).

    True only for a person whose token's ``azp`` claim is the approval
    client. An empty client setting means no token is one.
    """
    client_id = settings.keycloak_cli_approve_client_id
    return bool(client_id) and is_human_principal(operator) and operator.azp == client_id


def asked_for_credentials(operator: Operator) -> bool:
    """Whether the token's ``acr`` shows a sign-in that asked for credentials.

    False for a token without ``acr`` and for ``acr`` ``"0"``.
    """
    return operator.acr is not None and operator.acr != _NO_CREDENTIAL_ACR


def approval_sign_in_max_age(settings: Settings, *, destructive: bool) -> int:
    """How old a sign-in may be, in seconds, to approve a request of this risk level.

    The approval window (``FRESH_SIGN_IN_MAX_AGE_SECONDS``) for every
    request but one whose operation is ``destructive``; that one has its
    own limit, ``PER_ACTION_SIGN_IN_MAX_AGE_SECONDS``.
    """
    if destructive:
        return settings.per_action_sign_in_max_age_seconds
    return settings.fresh_sign_in_max_age_seconds


def _longest_sign_in_max_age(settings: Settings) -> int:
    """The longer of the two limits of :func:`approval_sign_in_max_age`."""
    return max(
        approval_sign_in_max_age(settings, destructive=False),
        approval_sign_in_max_age(settings, destructive=True),
    )


def _approval_auth(
    operator: Operator, settings: Settings, *, channel: ApprovalChannel, auth_time: int
) -> ApprovalAuth:
    """The proof for a sign-in that passed the checks."""
    one_time_code = operator.acr is not None and (
        operator.acr in settings.approval_one_time_code_acr_values
    )
    return ApprovalAuth(
        channel=channel,
        method="one_time_code" if one_time_code else "fresh_sign_in",
        auth_time=auth_time,
        sid=operator.sid,
        acr=operator.acr,
    )


def check_fresh_approval_sign_in(
    operator: Operator,
    settings: Settings,
    *,
    now: float | None = None,
) -> ApprovalAuth:
    """Return the proof for a token approve, or raise the ``401`` challenge.

    The token must come from the person's approval sign-in
    (:func:`is_approval_sign_in`), that sign-in must have asked for
    credentials (:func:`asked_for_credentials`), and it must be young
    enough for some request: ``auth_time`` at most the longer of the two
    limits of :func:`approval_sign_in_max_age` old
    (:func:`~meho_backplane.auth.fresh_sign_in.has_fresh_sign_in`). This
    runs before the request is read; the limit of the request's own risk
    level is applied in
    :func:`~meho_backplane.operations.approval_queue.approve_request`. A
    token with no ``auth_time`` or no ``acr`` is refused (fail closed).
    Every refusal is the same ``401``, so a client cannot tell which part
    failed.
    """
    approval_client = is_approval_sign_in(operator, settings)
    credentials = asked_for_credentials(operator)
    auth_time = operator.auth_time
    if (
        auth_time is None
        or not approval_client
        or not credentials
        or not has_fresh_sign_in(
            operator, now=now, max_age_seconds=_longest_sign_in_max_age(settings)
        )
    ):
        structlog.get_logger(__name__).warning(
            "approval_sign_in_not_fresh",
            operator_sub=operator.sub,
            has_auth_time=operator.auth_time is not None,
            approval_client=approval_client,
            asked_for_credentials=credentials,
        )
        raise fresh_sign_in_required(
            reason="fresh_sign_in_required",
            max_age_seconds=settings.fresh_sign_in_max_age_seconds,
        )
    return _approval_auth(operator, settings, channel="rest", auth_time=auth_time)


def has_fresh_web_approval_sign_in(
    operator: Operator, settings: Settings, *, now: float | None = None
) -> bool:
    """Whether the console session's sign-in may still approve some request.

    True when the person's sign-in asked for credentials
    (:func:`asked_for_credentials`) and is at most the longer of the two
    limits of :func:`approval_sign_in_max_age` old. The console uses it to
    offer a new sign-in instead of the Approve button.
    """
    return asked_for_credentials(operator) and has_fresh_sign_in(
        operator, now=now, max_age_seconds=_longest_sign_in_max_age(settings)
    )


def check_fresh_web_approval_sign_in(
    operator: Operator,
    settings: Settings,
    *,
    now: float | None = None,
) -> ApprovalAuth:
    """Return the proof for an approve in the web interface, or raise ``401``.

    The console session's token is the proof. It must pass
    :func:`has_fresh_web_approval_sign_in`: the same ``acr`` and age
    checks as :func:`check_fresh_approval_sign_in`, without the client
    check (the console got the token from its own login; the browser
    never holds it). The limit of the request's own risk level and the
    ``destructive`` one-use rule are applied in
    :func:`~meho_backplane.operations.approval_queue.approve_request`. The
    refusal is the same ``401`` challenge, with the console's text; the
    console shows it with a link to a new sign-in.
    """
    auth_time = operator.auth_time
    if auth_time is None or not has_fresh_web_approval_sign_in(operator, settings, now=now):
        structlog.get_logger(__name__).warning(
            "approval_sign_in_not_fresh",
            operator_sub=operator.sub,
            has_auth_time=operator.auth_time is not None,
            approval_client=None,
            asked_for_credentials=asked_for_credentials(operator),
        )
        raise fresh_sign_in_challenge(
            f"fresh_sign_in_required: {WEB_FRESH_SIGN_IN_REMEDIATION}",
            max_age_seconds=settings.fresh_sign_in_max_age_seconds,
        )
    return _approval_auth(operator, settings, channel="web", auth_time=auth_time)
