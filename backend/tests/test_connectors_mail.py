# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the mail.* SMTP-send connector — #2717 (Initiative #2716).

Covers the connector's load-bearing contracts on the ``net.*`` mould:

* ``mail.send`` is a **synthetic** targetless typed op: it dispatches
  with ``target=None``, the wire ``connector_id`` ``mail-smtp-1.x``
  round-trips through the parser, and the descriptor row carries
  ``safety_level="caution"`` + ``requires_approval=False`` (operators
  auto-run; agents ride the policy gate's needs-approval default).
* The recipient allowlist ``MAIL_RECIPIENT_ALLOWLIST`` has **inverted**
  semantics: empty ⇒ every send refused *before any SMTP connection
  opens*; full-address and domain entries admit recipients.
* The **return-failures contract**: allowlist-refused, unconfigured,
  and every SMTP delivery failure return ``{sent: false, reason}`` with
  dispatch ``status="ok"`` — never a ``connector_*`` error. Each
  ``SMTPException`` subclass maps to its stable reason code.
* STARTTLS + LOGIN sequencing: ``ehlo → starttls → ehlo → login →
  send_message`` when configured; no ``login`` without a username; no
  ``starttls`` when disabled; port 465 selects implicit TLS
  (``SMTP_SSL``); and ``login`` never runs on a cleartext channel —
  STARTTLS off plus a username refuses with
  ``smtp_auth_requires_tls``.
* Header injection is refused one layer above the transport: the
  ``subject`` schema pattern is the exact complement of the stdlib
  header guard, so a line break of any kind is ``invalid_params``
  rather than a ``ValueError`` escaping as a ``connector_error``.
* The durable audit row records ``to``/``subject`` via ``raw_payload``
  and never the body.

All SMTP traffic is stubbed by monkeypatching ``smtplib.SMTP`` /
``SMTP_SSL`` as seen from the transport module — no test opens a
socket. The autouse ``_default_database_url`` conftest fixture migrates
the SQLite DB to head so the ``endpoint_descriptor`` /
``operation_group`` / ``audit_log`` tables exist before the registrar
runs.
"""

from __future__ import annotations

import re
import smtplib
import socket
from collections.abc import AsyncIterator, Iterator
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.mail import ops as mail_ops
from meho_backplane.connectors.mail import transport as mail_transport
from meho_backplane.connectors.mail.allowlist import (
    RecipientNotAllowedError,
    assert_recipient_allowed,
    parse_recipient_allowlist,
)
from meho_backplane.connectors.mail.ops import register_mail_typed_operations
from meho_backplane.connectors.mail.transport import MailSendResult, send_email
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._lookup import parse_connector_id
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "mail-smtp-1.x"
_OP_ID = "mail.send"

_MAIL_ENV_VARS = (
    "MAIL_SMTP_HOST",
    "MAIL_SMTP_PORT",
    "MAIL_SMTP_STARTTLS",
    "MAIL_SMTP_USERNAME",
    "MAIL_SMTP_PASSWORD",
    "MAIL_FROM",
    "MAIL_RECIPIENT_ALLOWLIST",
)


# ---------------------------------------------------------------------------
# Settings env + dispatcher isolation + SMTP stubbing
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the minimal Settings env + reset dispatcher caches per test.

    Every ``MAIL_*`` var starts unset: allowlist empty ⇒ connector
    inert, host empty ⇒ transport unconfigured. Tests opt into config
    via :func:`_configure_mail_env`.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    for var in _MAIL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


def _configure_mail_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Set a working SMTP block + allowlist, then apply *overrides*.

    ``get_settings`` is process-cached, so the cache is cleared after
    every env mutation — the transport and allowlist read the live
    singleton per call.
    """
    env = {
        "MAIL_SMTP_HOST": "smtp.internal",
        "MAIL_FROM": "meho@example.com",
        "MAIL_RECIPIENT_ALLOWLIST": "example.com",
    }
    env.update(overrides)
    for key, value in env.items():
        if value == "":
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    get_settings.cache_clear()


class _RecordingSMTP:
    """Stand-in for ``smtplib.SMTP`` recording the full session sequence."""

    instances: ClassVar[list[_RecordingSMTP]]

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.sent_messages: list[EmailMessage] = []
        type(self).instances.append(self)

    def __enter__(self) -> _RecordingSMTP:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.calls.append(("quit", ()))

    def ehlo(self) -> None:
        self.calls.append(("ehlo", ()))

    def starttls(self, *, context: object | None = None) -> None:
        self.calls.append(("starttls", ()))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", (user, password)))

    def send_message(
        self,
        msg: EmailMessage,
        from_addr: str | None = None,
        to_addrs: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("send_message", (from_addr, tuple(to_addrs or ()))))
        self.sent_messages.append(msg)
        return {}


class _ExplodingSMTP:
    """Fails the test if the transport ever opens an SMTP connection."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("smtplib.SMTP must not be constructed when the send is refused")


@pytest.fixture
def recording_smtp(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingSMTP]:
    """Patch both SMTP entrypoints with a per-test recording class."""

    class _PerTestSMTP(_RecordingSMTP):
        instances: ClassVar[list[_RecordingSMTP]] = []

    monkeypatch.setattr(mail_transport.smtplib, "SMTP", _PerTestSMTP)
    monkeypatch.setattr(mail_transport.smtplib, "SMTP_SSL", _PerTestSMTP)
    return _PerTestSMTP


@pytest.fixture
def exploding_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch both SMTP entrypoints to fail on any connection attempt."""
    monkeypatch.setattr(mail_transport.smtplib, "SMTP", _ExplodingSMTP)
    monkeypatch.setattr(mail_transport.smtplib, "SMTP_SSL", _ExplodingSMTP)


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registration doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_mail_op(stub_embedding_service: AsyncMock) -> AsyncIterator[None]:
    """Upsert the ``mail.send`` descriptor row for dispatch-driving tests."""
    await register_mail_typed_operations(embedding_service=stub_embedding_service)
    yield


def _make_operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch_send(params: dict[str, Any]) -> OperationResult:
    """Dispatch ``mail.send`` through the real targetless path.

    ``target`` is ``None`` (synthetic product, no connector instance /
    registered target); the handler is module-level. The op is
    ``requires_approval=False`` and the caller a human operator, so the
    policy gate auto-executes despite ``safety_level="caution"``.
    """
    return await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=None,
        params=params,
    )


# ---------------------------------------------------------------------------
# Synthetic identity + registration posture + no-connector-class
# ---------------------------------------------------------------------------


def test_mail_connector_id_round_trips() -> None:
    """The wire connector_id resolves to the registered natural key."""
    assert parse_connector_id(_CONNECTOR_ID) == ("mail", "1.x", "mail-smtp")


async def test_mail_send_registered_as_caution_ungated_typed_op(
    _registered_mail_op: None,
) -> None:
    """The descriptor row carries the exact synthetic identity + posture.

    ``caution`` + ``requires_approval=False``: operators auto-run the
    outward-facing send while agent principals land on the policy
    gate's needs-approval default — the allowlist is the floor either way.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.product == "mail",
                EndpointDescriptor.version == "1.x",
                EndpointDescriptor.impl_id == "mail-smtp",
                EndpointDescriptor.op_id == _OP_ID,
            )
        )
        row = result.scalar_one()
    assert row.source_kind == "typed"
    assert row.safety_level == "caution"
    assert row.requires_approval is False


def test_mail_connector_registers_no_connector_class() -> None:
    """``mail`` is synthetic — no ``register_connector_v2`` anywhere under it."""
    mail_pkg = Path(mail_ops.__file__).parent
    sources = "\n".join(p.read_text() for p in mail_pkg.glob("*.py"))
    assert "register_connector_v2(" not in sources
    assert "register_connector(" not in sources


# ---------------------------------------------------------------------------
# Refusal floors — allowlist (inert when empty) and unconfigured transport
# ---------------------------------------------------------------------------


async def test_empty_allowlist_refuses_before_any_smtp_connection(
    monkeypatch: pytest.MonkeyPatch,
    exploding_smtp: None,
    _registered_mail_op: None,
) -> None:
    """Empty ``MAIL_RECIPIENT_ALLOWLIST`` ⇒ structured refusal, no connection.

    The host is configured, so only the allowlist stands between the
    dispatch and the (exploding) SMTP constructor — proving the refusal
    happens before any connection.
    """
    _configure_mail_env(monkeypatch, MAIL_RECIPIENT_ALLOWLIST="")

    result = await _dispatch_send({"to": ["oncall@example.com"], "subject": "s", "body": "b"})

    assert result.status == "ok", result.error
    assert result.result == {
        "sent": False,
        "reason": "not_in_recipient_allowlist",
        "to": ["oncall@example.com"],
        "subject": "s",
    }


async def test_recipient_outside_allowlist_refuses_whole_send(
    monkeypatch: pytest.MonkeyPatch,
    exploding_smtp: None,
) -> None:
    """One unlisted recipient refuses the send even when others are listed."""
    _configure_mail_env(monkeypatch)

    result = await send_email(to=["oncall@example.com", "exfil@evil.test"], subject="s", body="b")

    assert result == MailSendResult(sent=False, reason="not_in_recipient_allowlist")


async def test_unconfigured_host_refuses_with_smtp_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    exploding_smtp: None,
) -> None:
    """Allowlisted recipient but no ``MAIL_SMTP_HOST`` ⇒ ``smtp_unconfigured``."""
    _configure_mail_env(monkeypatch, MAIL_SMTP_HOST="")

    result = await send_email(to=["oncall@example.com"], subject="s", body="b")

    assert result == MailSendResult(sent=False, reason="smtp_unconfigured")


async def test_unconfigured_from_refuses_with_smtp_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    exploding_smtp: None,
) -> None:
    """A host without ``MAIL_FROM`` is still an unconfigured transport."""
    _configure_mail_env(monkeypatch, MAIL_FROM="")

    result = await send_email(to=["oncall@example.com"], subject="s", body="b")

    assert result == MailSendResult(sent=False, reason="smtp_unconfigured")


# ---------------------------------------------------------------------------
# SMTP session shape — STARTTLS/login sequencing, implicit TLS, message body
# ---------------------------------------------------------------------------


async def test_starttls_and_login_run_in_sequence_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    recording_smtp: type[_RecordingSMTP],
) -> None:
    """Configured STARTTLS + auth ⇒ ehlo → starttls → ehlo → login → send.

    The post-``starttls`` ``ehlo`` is the RFC 3207 re-discovery on the
    encrypted channel; ``login`` runs only after it.
    """
    _configure_mail_env(
        monkeypatch,
        MAIL_SMTP_USERNAME="meho-mailer",
        MAIL_SMTP_PASSWORD="s3cret",
    )

    result = await send_email(
        to=["oncall@example.com"], subject="Disk alert", body="datastore at 91%"
    )

    assert result == MailSendResult(sent=True, reason=None)
    (client,) = recording_smtp.instances
    assert (client.host, client.port) == ("smtp.internal", 587)
    assert client.timeout == mail_transport._SMTP_TIMEOUT_SECONDS
    assert [name for name, _ in client.calls] == [
        "ehlo",
        "starttls",
        "ehlo",
        "login",
        "send_message",
        "quit",
    ]
    assert ("login", ("meho-mailer", "s3cret")) in client.calls
    assert ("send_message", ("meho@example.com", ("oncall@example.com",))) in client.calls
    (msg,) = client.sent_messages
    assert msg["From"] == "meho@example.com"
    assert msg["To"] == "oncall@example.com"
    assert msg["Subject"] == "Disk alert"
    assert msg.get_content() == "datastore at 91%\n"


async def test_no_starttls_and_no_login_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    recording_smtp: type[_RecordingSMTP],
) -> None:
    """STARTTLS off + no username ⇒ plain ehlo → send, no upgrade, no auth."""
    _configure_mail_env(monkeypatch, MAIL_SMTP_STARTTLS="false")

    result = await send_email(to=["oncall@example.com"], subject="s", body="b")

    assert result == MailSendResult(sent=True, reason=None)
    (client,) = recording_smtp.instances
    assert [name for name, _ in client.calls] == ["ehlo", "send_message", "quit"]


async def test_port_465_uses_implicit_tls_without_starttls(
    monkeypatch: pytest.MonkeyPatch,
    recording_smtp: type[_RecordingSMTP],
) -> None:
    """Port 465 (RFC 8314 SMTPS) connects via ``SMTP_SSL`` — no ``starttls``.

    The starttls flag stays at its true default; implicit TLS makes the
    upgrade moot, so the session must not attempt it.
    """
    monkeypatch.setattr(mail_transport.smtplib, "SMTP", _ExplodingSMTP)
    _configure_mail_env(monkeypatch, MAIL_SMTP_PORT="465")

    result = await send_email(to=["oncall@example.com"], subject="s", body="b")

    assert result == MailSendResult(sent=True, reason=None)
    (client,) = recording_smtp.instances
    assert client.port == 465
    assert [name for name, _ in client.calls] == ["ehlo", "send_message", "quit"]


async def test_plaintext_channel_refuses_auth_instead_of_logging_in(
    monkeypatch: pytest.MonkeyPatch,
    recording_smtp: type[_RecordingSMTP],
) -> None:
    """STARTTLS off + a username ⇒ refuse; never AUTH over cleartext.

    ``smtplib.login()`` imposes no TLS precondition, and AUTH
    LOGIN/PLAIN only base64-wraps the credentials — on a plaintext
    socket they are readable on the wire. The misconfiguration is
    refused (and the message is not sent) rather than silently obeyed.
    """
    _configure_mail_env(
        monkeypatch,
        MAIL_SMTP_STARTTLS="false",
        MAIL_SMTP_USERNAME="meho-mailer",
        MAIL_SMTP_PASSWORD="s3cret",
    )

    result = await send_email(to=["oncall@example.com"], subject="s", body="b")

    assert result == MailSendResult(sent=False, reason="smtp_auth_requires_tls")
    (client,) = recording_smtp.instances
    assert [name for name, _ in client.calls] == ["ehlo", "quit"]
    assert "login" not in [name for name, _ in client.calls]
    assert not client.sent_messages


async def test_implicit_tls_admits_login_with_starttls_disabled(
    monkeypatch: pytest.MonkeyPatch,
    recording_smtp: type[_RecordingSMTP],
) -> None:
    """The guard tracks the channel, not the ``starttls`` flag.

    Port 465 is encrypted from the first byte, so ``MAIL_SMTP_STARTTLS``
    being off there is not a plaintext-auth case and ``login`` must
    still run.
    """
    _configure_mail_env(
        monkeypatch,
        MAIL_SMTP_PORT="465",
        MAIL_SMTP_STARTTLS="false",
        MAIL_SMTP_USERNAME="meho-mailer",
        MAIL_SMTP_PASSWORD="s3cret",
    )

    result = await send_email(to=["oncall@example.com"], subject="s", body="b")

    assert result == MailSendResult(sent=True, reason=None)
    (client,) = recording_smtp.instances
    assert [name for name, _ in client.calls] == ["ehlo", "login", "send_message", "quit"]


async def test_dispatch_surfaces_plaintext_auth_refusal_as_status_ok(
    monkeypatch: pytest.MonkeyPatch,
    recording_smtp: type[_RecordingSMTP],
    _registered_mail_op: None,
) -> None:
    """The new reason code rides the return-failures contract like the rest."""
    _configure_mail_env(
        monkeypatch,
        MAIL_SMTP_STARTTLS="false",
        MAIL_SMTP_USERNAME="meho-mailer",
        MAIL_SMTP_PASSWORD="s3cret",
    )

    result = await _dispatch_send({"to": ["oncall@example.com"], "subject": "s", "body": "b"})

    assert result.status == "ok", result.error
    assert result.result == {
        "sent": False,
        "reason": "smtp_auth_requires_tls",
        "to": ["oncall@example.com"],
        "subject": "s",
    }


def test_every_transport_reason_code_is_documented_in_the_response_schema() -> None:
    """The op's advertised reason list stays in step with the transport.

    The response schema and llm_instructions are what an agent reads to
    decide whether a refusal is retryable, so a reason code the
    transport can return but the descriptor never names is a silent
    contract gap.
    """
    advertised = mail_ops._MAIL_SEND_RESPONSE_SCHEMA["properties"]["reason"]["description"]
    instructions = mail_ops._MAIL_SEND_LLM_INSTRUCTIONS["output_shape"]
    docstring = mail_transport.__doc__ or ""
    for code in (
        "not_in_recipient_allowlist",
        "smtp_unconfigured",
        "smtp_connect_error",
        "smtp_auth_requires_tls",
        "smtp_auth_error",
        "smtp_recipients_refused",
        "smtp_error",
    ):
        assert code in advertised, code
        assert code in instructions, code
        assert code in docstring, code


# ---------------------------------------------------------------------------
# Return-failures contract — every delivery failure maps to a reason code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc,expected_reason",
    [
        (smtplib.SMTPAuthenticationError(535, b"authentication failed"), "smtp_auth_error"),
        (
            smtplib.SMTPRecipientsRefused({"oncall@example.com": (550, b"mailbox unavailable")}),
            "smtp_recipients_refused",
        ),
        (smtplib.SMTPConnectError(421, b"service not available"), "smtp_connect_error"),
        (smtplib.SMTPServerDisconnected("connection lost"), "smtp_error"),
        (smtplib.SMTPException("malformed reply"), "smtp_error"),
    ],
    ids=["auth", "recipients-refused", "connect", "disconnected", "generic"],
)
async def test_smtp_exception_subclasses_map_to_reason_codes(
    monkeypatch: pytest.MonkeyPatch,
    recording_smtp: type[_RecordingSMTP],
    exc: smtplib.SMTPException,
    expected_reason: str,
) -> None:
    """Each ``SMTPException`` subclass maps to its stable reason, never raises.

    Pins the except-arm ordering: ``SMTPException`` subclasses
    ``OSError``, so a specific SMTP failure must not fall through to the
    socket-level ``smtp_connect_error`` arm.
    """
    _configure_mail_env(monkeypatch)

    def _raise(*_a: object, **_kw: object) -> None:
        raise exc

    monkeypatch.setattr(recording_smtp, "send_message", _raise)

    result = await send_email(to=["oncall@example.com"], subject="s", body="b")

    assert result == MailSendResult(sent=False, reason=expected_reason)


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError("connection refused"),
        socket.gaierror("name resolution failed"),
        TimeoutError("timed out"),
    ],
    ids=["refused", "gaierror", "timeout"],
)
async def test_socket_level_connect_failures_map_to_smtp_connect_error(
    monkeypatch: pytest.MonkeyPatch,
    exc: OSError,
) -> None:
    """Raw ``OSError``s from the constructor's connect map to a reason code."""
    _configure_mail_env(monkeypatch)

    class _FailingConnectSMTP:
        def __init__(self, *_a: object, **_kw: object) -> None:
            raise exc

    monkeypatch.setattr(mail_transport.smtplib, "SMTP", _FailingConnectSMTP)

    result = await send_email(to=["oncall@example.com"], subject="s", body="b")

    assert result == MailSendResult(sent=False, reason="smtp_connect_error")


async def test_send_success_returns_sent_true(
    monkeypatch: pytest.MonkeyPatch,
    recording_smtp: type[_RecordingSMTP],
) -> None:
    """The success path: MTA accepts ⇒ ``MailSendResult(sent=True, reason=None)``."""
    _configure_mail_env(monkeypatch)

    result = await send_email(to=["oncall@example.com"], subject="s", body="b")

    assert result == MailSendResult(sent=True, reason=None)


# ---------------------------------------------------------------------------
# Shared-dispatcher parity — call_operation path + durable audit row
# ---------------------------------------------------------------------------


async def test_dispatch_mail_send_returns_ok_and_writes_audit_row(
    monkeypatch: pytest.MonkeyPatch,
    recording_smtp: type[_RecordingSMTP],
    _registered_mail_op: None,
) -> None:
    """Dispatching ``("mail-smtp-1.x", "mail.send")`` is status=ok + audited.

    The durable audit row's ``raw_payload`` carries the literal
    ``to``/``subject`` (the "who was mailed" answer) and never the
    message body.
    """
    _configure_mail_env(monkeypatch)

    result = await _dispatch_send(
        {
            "to": ["oncall@example.com"],
            "subject": "Dashboard critical",
            "body": "the secret-adjacent body text",
        }
    )

    assert result.status == "ok", result.error
    assert result.result == {
        "sent": True,
        "reason": None,
        "to": ["oncall@example.com"],
        "subject": "Dashboard critical",
    }

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    mail_rows = [r for r in rows if r.path == _OP_ID]
    assert len(mail_rows) == 1
    raw = mail_rows[0].raw_payload
    assert raw is not None
    assert raw["to"] == ["oncall@example.com"]
    assert raw["subject"] == "Dashboard critical"
    assert "body" not in raw


# ---------------------------------------------------------------------------
# Header-injection rejection — the schema, not the transport, refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "alert\r\nBcc: evil@example.com",
        "alert\nBcc: evil@example.com",
        "alert\rBcc: evil@example.com",
        "alert\vBcc: evil@example.com",
        "alert\fBcc: evil@example.com",
        "alert\x1cBcc: evil@example.com",
        "alert\x1dBcc: evil@example.com",
        "alert\x1eBcc: evil@example.com",
        "alert\x85Bcc: evil@example.com",
        "alert\u2028Bcc: evil@example.com",
        "alert\u2029Bcc: evil@example.com",
    ],
    ids=["crlf", "lf", "cr", "vt", "ff", "fs", "gs", "rs", "nel", "ls", "ps"],
)
async def test_line_break_subject_is_invalid_params_not_connector_error(
    monkeypatch: pytest.MonkeyPatch,
    exploding_smtp: None,
    _registered_mail_op: None,
    subject: str,
) -> None:
    """A subject carrying any line break is refused by the parameter schema.

    ``EmailMessage`` guards header assignment with
    ``len(value.splitlines()) > 1`` and raises ``ValueError``, which
    would escape ``send_email`` as a ``connector_error`` and break the
    return-failures contract. The schema pattern is the exact complement
    of that guard — so the dispatcher rejects the call at validation,
    before the handler runs and before any SMTP connection opens.
    """
    _configure_mail_env(monkeypatch)

    result = await _dispatch_send({"to": ["oncall@example.com"], "subject": subject, "body": "b"})

    assert result.status == "error"
    assert result.extras is not None
    assert result.extras["error_code"] == "invalid_params"
    assert any(err["validator"] == "pattern" for err in result.extras["validation_errors"])


def test_subject_pattern_is_the_exact_complement_of_the_stdlib_guard() -> None:
    """No subject the schema admits can make ``EmailMessage`` raise.

    Sweeps the whole Unicode range rather than a hand-picked list: the
    stdlib guard keys off :meth:`str.splitlines`, whose boundary set is
    wider than CR/LF and has grown across Python versions. If a future
    interpreter adds a boundary character, this test fails instead of
    the contract silently reopening.
    """
    pattern = re.compile(mail_ops._SINGLE_LINE_PATTERN)
    admitted_but_rejected = [
        hex(codepoint)
        for codepoint in range(0x110000)
        if len(f"a{chr(codepoint)}b".splitlines()) > 1
        and pattern.search(f"a{chr(codepoint)}b") is not None
    ]
    assert admitted_but_rejected == []

    # …and the pattern is not so wide it refuses an ordinary subject.
    for allowed in ("Disk alert", "datastore-01 at 91%", "Ärger: Füllstand", "a\tb"):
        assert pattern.search(allowed) is not None


async def test_single_line_subject_still_reaches_the_transport(
    monkeypatch: pytest.MonkeyPatch,
    recording_smtp: type[_RecordingSMTP],
    _registered_mail_op: None,
) -> None:
    """The pattern is a line-break guard only — normal subjects dispatch."""
    _configure_mail_env(monkeypatch)

    result = await _dispatch_send(
        {"to": ["oncall@example.com"], "subject": "Ärger: 91% voll", "body": "b"}
    )

    assert result.status == "ok", result.error
    (msg,) = recording_smtp.instances[0].sent_messages
    assert msg["Subject"] == "Ärger: 91% voll"


# ---------------------------------------------------------------------------
# Allowlist unit behaviour
# ---------------------------------------------------------------------------


def test_assert_recipient_allowed_empty_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAIL_RECIPIENT_ALLOWLIST", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RecipientNotAllowedError):
        assert_recipient_allowed("oncall@example.com")


def test_assert_recipient_allowed_full_address_and_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAIL_RECIPIENT_ALLOWLIST", "oncall@ops.test, @example.com")
    get_settings.cache_clear()
    assert_recipient_allowed("oncall@ops.test")
    assert_recipient_allowed("OnCall@Ops.Test")  # case-insensitive
    assert_recipient_allowed("anyone@example.com")  # domain entry, leading @ accepted
    assert_recipient_allowed("anyone@example.com.")  # trailing dot tolerated
    with pytest.raises(RecipientNotAllowedError):
        assert_recipient_allowed("other@ops.test")  # address entry ≠ domain grant
    with pytest.raises(RecipientNotAllowedError):
        assert_recipient_allowed("anyone@sub.example.com")  # no subdomain widening


@pytest.mark.parametrize(
    "recipient",
    ["", "no-at-sign", "two@@example.com", "a@b@c", "evil@example.com\r\nBcc: x@y.z"],
    ids=["empty", "no-at", "double-at", "two-ats", "crlf-injection"],
)
def test_assert_recipient_allowed_rejects_malformed_recipient(
    monkeypatch: pytest.MonkeyPatch,
    recipient: str,
) -> None:
    """An address the floor cannot positively parse is refused, not guessed."""
    monkeypatch.setenv("MAIL_RECIPIENT_ALLOWLIST", "example.com")
    get_settings.cache_clear()
    with pytest.raises(RecipientNotAllowedError):
        assert_recipient_allowed(recipient)


@pytest.mark.parametrize(
    "allowlist",
    [
        "bad entry@example.com",
        "a@b@c",
        "foo@",
        "@",
        "@example.com, foo@",
        "...",
        "@.",
    ],
    ids=[
        "embedded-space",
        "double-at",
        "empty-domain",
        "bare-at",
        "one-good-one-bad",
        "dots-only",
        "at-dot",
    ],
)
def test_parse_recipient_allowlist_rejects_malformed_entry(
    monkeypatch: pytest.MonkeyPatch,
    allowlist: str,
) -> None:
    """A malformed allowlist entry fails loud — never silently kept."""
    monkeypatch.setenv("MAIL_RECIPIENT_ALLOWLIST", allowlist)
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="MAIL_RECIPIENT_ALLOWLIST"):
        assert_recipient_allowed("oncall@example.com")


def test_bare_at_entry_no_longer_masks_the_inert_connector_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lone ``@`` is a config error, not a silently-empty domain grant.

    It used to parse to ``domains={""}``: matching nothing, yet
    non-empty enough to route the refusal past the "allowlist is empty,
    the connector is inert" branch — so the operator got the generic
    "not listed" message for a typo that had disabled every send.
    """
    monkeypatch.setenv("MAIL_RECIPIENT_ALLOWLIST", "@")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="not a valid address or domain"):
        parse_recipient_allowlist()


def test_parse_recipient_allowlist_accepts_the_documented_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tightened validation still admits every documented entry form."""
    monkeypatch.setenv(
        "MAIL_RECIPIENT_ALLOWLIST", "OnCall@Ops.Test, @example.com, example.org, corp.test."
    )
    get_settings.cache_clear()

    addresses, domains = parse_recipient_allowlist()

    assert addresses == frozenset({"oncall@ops.test"})
    assert domains == frozenset({"example.com", "example.org", "corp.test"})
