# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared SMTP send transport for the ``mail.*`` connector.

:func:`send_email` is the **single implementation** every mail-sending
path calls: the ``mail.send`` dispatch handler
(:mod:`meho_backplane.connectors.mail.ops`) and the checks notifier
(#2719) import this same function, so the recipient-allowlist floor and
the return-failures contract hold on every path — there is no way to
send mail from the backplane around them.

Stdlib only (task #2717: no new dependency): :mod:`smtplib` +
:class:`email.message.EmailMessage`. ``smtplib`` is blocking, so the
whole SMTP session (connect → STARTTLS → login → send → quit) runs in
one worker thread via :func:`asyncio.to_thread` — the socket is bound
to the thread that opened it, so the session cannot be split across
``to_thread`` hops.

TLS shape (RFC 8314): port 465 selects **implicit TLS**
(:class:`smtplib.SMTP_SSL` — the connection is TLS from the first
byte, so the ``mail_smtp_starttls`` flag is moot there). Any other
port opens plaintext via :class:`smtplib.SMTP` and, when
``mail_smtp_starttls`` is set (default), upgrades with ``starttls()``
followed by a fresh ``ehlo()`` — the RFC 3207 requirement to rediscover
extensions on the encrypted channel. ``login()`` runs only when a
username is configured (an internal relay commonly needs none) **and
only on an encrypted channel**: ``smtplib.login()`` imposes no TLS
precondition of its own, so AUTH LOGIN/PLAIN would put base64-wrapped —
not encrypted — credentials on a cleartext socket. Turning
``mail_smtp_starttls`` off on a non-465 port while a username is set is
therefore a misconfiguration the session refuses
(``smtp_auth_requires_tls``) rather than silently obeys; Prometheus
Alertmanager takes the same stance with ``require_tls`` defaulting to
true.

Return-failures contract (mould parity with ``net.*``): a refused,
unconfigured, or delivery-failed send is the **product**, not an error
— the caller gets ``MailSendResult(sent=False, reason=<code>)`` and
the dispatch stays ``status="ok"``. Only an unexpected bug propagates.
Reason codes are stable:

* ``not_in_recipient_allowlist`` — a recipient failed the allowlist
  floor (including the empty-allowlist inert state).
* ``smtp_unconfigured`` — ``mail_smtp_host`` or ``mail_from`` unset.
* ``smtp_connect_error`` — TCP/TLS/greeting failure reaching the MTA
  (:class:`smtplib.SMTPConnectError` or a raw socket-level
  :class:`OSError` — refused, DNS failure, timeout).
* ``smtp_auth_requires_tls`` — a username is configured but the
  channel is still cleartext, so the session refuses rather than
  handing the credentials to the wire (see above).
* ``smtp_auth_error`` — :class:`smtplib.SMTPAuthenticationError`.
* ``smtp_recipients_refused`` — the server rejected every recipient
  (:class:`smtplib.SMTPRecipientsRefused`).
* ``smtp_error`` — any other :class:`smtplib.SMTPException`.

The ``except`` ordering below is load-bearing: ``SMTPException``
subclasses :class:`OSError` (since Python 3.4), so the specific SMTP
arms and the generic ``SMTPException`` arm must precede the ``OSError``
arm or a refused login would misreport as a connect error.

Logging discipline: structlog events carry host/port/reason only —
never the password, never the message body. The recipient list and
subject are the *handler's* audit answer (they ride the dispatch
``raw_payload``), not the transport log's.
"""

from __future__ import annotations

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Final

import structlog

from meho_backplane.connectors.mail.allowlist import (
    RecipientNotAllowedError,
    assert_recipient_allowed,
)
from meho_backplane.settings import get_settings

__all__ = [
    "MailSendResult",
    "send_email",
]

_log = structlog.get_logger(__name__)

#: Hard bound on the blocking SMTP session (connect + every command).
#: Passed as ``timeout=`` to :class:`smtplib.SMTP` / ``SMTP_SSL`` so a
#: hung MTA cannot pin a worker thread indefinitely. Internal constant,
#: not a settings knob — no consumer has asked to tune it (#1177).
_SMTP_TIMEOUT_SECONDS: Final[float] = 30.0

#: Port that selects implicit TLS (:class:`smtplib.SMTP_SSL`) — the
#: IANA submissions-over-TLS port (RFC 8314 §7.3).
_IMPLICIT_TLS_PORT: Final[int] = 465


@dataclass(frozen=True, slots=True)
class MailSendResult:
    """Outcome of one :func:`send_email` call.

    ``sent=True`` ⇔ ``reason is None``. ``reason`` is one of the stable
    codes documented in the module docstring.
    """

    sent: bool
    reason: str | None = None


def _send_sync(
    msg: EmailMessage,
    *,
    host: str,
    port: int,
    starttls: bool,
    username: str,
    password: str,
    from_addr: str,
    to_addrs: list[str],
) -> str | None:
    """Run the blocking SMTP session; return a failure reason code or ``None``.

    Executes on a worker thread (via :func:`asyncio.to_thread`) — every
    line here may block on the socket. The ``with`` block issues
    ``QUIT`` and closes the connection on every exit path
    (:class:`smtplib.SMTP` is a context manager).

    ``encrypted`` tracks whether the channel is confidential — true from
    construction under implicit TLS, true again once ``starttls()``
    returns. It gates ``login()`` at the call site rather than being
    re-derived from ``starttls``/``port`` up front, so a later change to
    how TLS is established cannot leave the credential guard behind.
    """
    try:
        if port == _IMPLICIT_TLS_PORT:
            client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT_SECONDS)
            encrypted = True
        else:
            client = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_SECONDS)
            encrypted = False
        with client:
            client.ehlo()
            if starttls and port != _IMPLICIT_TLS_PORT:
                client.starttls()
                # RFC 3207: the pre-TLS EHLO response is void; rediscover
                # the server's extensions on the encrypted channel.
                client.ehlo()
                encrypted = True
            if username:
                if not encrypted:
                    return "smtp_auth_requires_tls"
                client.login(username, password)
            # Explicit envelope: the validated recipient list drives RCPT
            # TO verbatim rather than being re-derived from the headers.
            client.send_message(msg, from_addr=from_addr, to_addrs=to_addrs)
    except smtplib.SMTPAuthenticationError:
        return "smtp_auth_error"
    except smtplib.SMTPRecipientsRefused:
        return "smtp_recipients_refused"
    except smtplib.SMTPConnectError:
        return "smtp_connect_error"
    except smtplib.SMTPException:
        return "smtp_error"
    except OSError:
        # Raw socket-level connect failure (refused, DNS, timeout) —
        # smtplib raises these un-wrapped from the constructor's connect.
        return "smtp_connect_error"
    return None


async def send_email(*, to: list[str], subject: str, body: str) -> MailSendResult:
    """Send one plain-text email through the deployment-level SMTP block.

    The single shared transport for the ``mail.send`` typed op and the
    checks notifier (#2719). Flow: screen **every** recipient against
    the allowlist floor → refuse when the SMTP block is unconfigured →
    build the :class:`~email.message.EmailMessage` → run the blocking
    session on a worker thread.

    The allowlist screen is all-or-nothing: one refused recipient
    refuses the whole send. A partial send would make the audit row's
    "who was mailed" answer wrong, and the callers' recipient lists are
    short, operator-curated sets — not fan-out batches.

    A failed send is returned, never raised (return-failures contract);
    see the module docstring for the reason codes. A CR/LF-bearing
    *recipient* cannot survive the allowlist parse, so it never reaches
    the :class:`~email.message.EmailMessage` build on any path. A
    *subject* carrying a line break is screened one layer up, by
    ``mail.send``'s parameter schema
    (``ops._SINGLE_LINE_PATTERN``, the exact complement of the stdlib
    header guard): every dispatched call is rejected as
    ``invalid_params`` before the handler runs. **Direct in-process
    callers get no such screen** — passing a subject with a line break
    raises :class:`ValueError` out of this function rather than
    returning a result. Compose single-line subjects; the checks
    notifier (#2719) builds its own from check metadata.
    """
    settings = get_settings()
    try:
        for recipient in to:
            assert_recipient_allowed(recipient)
    except RecipientNotAllowedError:
        _log.info(
            "mail.send.refused",
            reason="not_in_recipient_allowlist",
            host=settings.mail_smtp_host,
            port=settings.mail_smtp_port,
        )
        return MailSendResult(sent=False, reason="not_in_recipient_allowlist")

    if not settings.mail_smtp_host or not settings.mail_from:
        _log.info("mail.send.refused", reason="smtp_unconfigured")
        return MailSendResult(sent=False, reason="smtp_unconfigured")

    msg = EmailMessage()
    msg["From"] = settings.mail_from
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)

    reason = await asyncio.to_thread(
        _send_sync,
        msg,
        host=settings.mail_smtp_host,
        port=settings.mail_smtp_port,
        starttls=settings.mail_smtp_starttls,
        username=settings.mail_smtp_username,
        password=settings.mail_smtp_password,
        from_addr=settings.mail_from,
        to_addrs=list(to),
    )
    if reason is not None:
        _log.warning(
            "mail.send.failed",
            reason=reason,
            host=settings.mail_smtp_host,
            port=settings.mail_smtp_port,
        )
        return MailSendResult(sent=False, reason=reason)
    _log.info(
        "mail.send.delivered",
        host=settings.mail_smtp_host,
        port=settings.mail_smtp_port,
        recipient_count=len(to),
    )
    return MailSendResult(sent=True, reason=None)
