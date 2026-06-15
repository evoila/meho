# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``connector_tls_verify_failed`` dispatch error.

Initiative #1774 T3 (#1782) acceptance criteria:

* A dispatch whose TLS verification fails (an :exc:`httpx.ConnectError`
  whose ``__cause__`` is an :exc:`ssl.SSLCertVerificationError`) returns a
  structured ``connector_tls_verify_failed`` :class:`OperationResult` --
  NOT the bare ``connector_error: ConnectError`` that discarded the SSL
  cause, leaving the operator with ``[SSL: CERTIFICATE_VERIFY_FAILED]``
  and no guidance. The ``error`` names the **host** + **both**
  remediations (the secure ``SSL_CERT_FILE`` / CA-bundle path and the
  ``verify_tls=false`` audited last resort); ``extras`` carries
  ``error_code='connector_tls_verify_failed'``, ``host``, the raw SSL
  string preserved in ``exception_message`` (capped ~256).
* A non-SSL :exc:`httpx.ConnectError` (DNS failure, connection refused,
  connect timeout) still returns ``connector_error`` -- never
  mislabelled as a TLS fault.
* The ``CERTIFICATE_VERIFY_FAILED`` substring fallback classifies a
  TLS-verify failure even when the ``__cause__`` chain is empty.
* The new arm calls :func:`audit_and_broadcast_safe` with
  ``result_status='error'`` before returning (always-audit + never-raises
  contract): an audit row + broadcast event land.
* #1649's 403/422 ``HTTPStatusError`` arm is not regressed (a
  ``ConnectError`` and an ``HTTPStatusError`` are disjoint).

The builder-shape tests mirror the #1649
``test_operations_connector_http_403`` discipline
(``docs/codebase/error-message-shape.md``): stable code, diagnostic human
message naming the host with a remediation imperative + doc reference,
structured ``extras`` payload.
"""

from __future__ import annotations

import ssl
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._errors import result_connector_tls_verify_failed
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Settings / isolation fixtures (the sibling dispatcher-test pattern)
# ---------------------------------------------------------------------------

_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000017ec")

#: The raw message httpx surfaces on a self-signed cert-chain failure.
_SSL_VERIFY_MESSAGE: str = (
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
    "self-signed certificate in certificate chain (_ssl.c:1010)"
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry around every test."""
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so descriptor inserts don't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Replace :func:`publish_event` with a recording stub."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_operator(*, sub: str = "op-conn-tls") -> Operator:
    """Construct an :class:`Operator` directly -- no JWT round-trip."""
    return Operator(
        sub=sub,
        name="Connector-TLS Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeFingerprint:
    """Duck-typed fingerprint for resolver lookups."""

    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """Minimal target shape the resolver / dispatcher / connectors read."""

    def __init__(
        self,
        *,
        product: str = "vcfops",
        version: str | None = "9",
        name: str = "vrli-lab",
        host: str = "vrli.lab.internal",
        port: int = 443,
        auth_model: str | None = "shared_service_account",
    ) -> None:
        self.product = product
        self.fingerprint = _FakeFingerprint(version=version)
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = name
        self.host = host
        self.port = port
        self.auth_model = auth_model
        self.secret_ref: str | None = None


def _make_tls_verify_connect_error(message: str = _SSL_VERIFY_MESSAGE) -> httpx.ConnectError:
    """Build a :exc:`httpx.ConnectError` chained from an SSL verify error.

    Reproduces the production shape (verified against httpx 0.28.1): the
    transport raises :exc:`httpx.ConnectError` ``from`` the underlying
    :exc:`ssl.SSLCertVerificationError`, so ``conn_exc.__cause__`` is the
    ssl error the dispatcher's ``isinstance`` narrowing keys on.
    """
    try:
        try:
            raise ssl.SSLCertVerificationError(1, message)
        except ssl.SSLCertVerificationError as ssl_exc:
            raise httpx.ConnectError(message) from ssl_exc
    except httpx.ConnectError as conn_exc:
        return conn_exc
    raise AssertionError("ConnectError was not produced")  # pragma: no cover


class _TlsVerifyFailConnector(HttpConnector):
    """Connector whose transport raises a TLS-verify ``ConnectError``.

    The self-signed / internal-CA appliance case (Initiative #1774): the
    socket opens, the host answers, only cert-chain verification fails.
    Overrides ``_request_json`` / ``_post_json`` to raise the same chained
    ``ConnectError`` the real :class:`HttpConnector` surfaces once
    ``_retryable`` exhausts its retries, so the test needs no live TLS.
    """

    product = "vcfops"
    version = "9"
    impl_id = "vcfops-rest"
    supported_version_range = ">=9,<10"
    priority = 1

    async def _request_json(
        self,
        target: Any,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise _make_tls_verify_connect_error()

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise _make_tls_verify_connect_error()

    async def fingerprint(  # type: ignore[override]
        self,
        target: Any,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError


class _ConnRefusedConnector(_TlsVerifyFailConnector):
    """Same shape but raises a non-SSL ``ConnectError`` (connection refused).

    Pins the narrowing boundary: a DNS / refused / timeout ``ConnectError``
    matches neither the ``__cause__`` ``isinstance`` nor the
    ``CERTIFICATE_VERIFY_FAILED`` substring, so it MUST fall through to the
    generic ``connector_error`` -- never mislabelled TLS.
    """

    impl_id = "vcfops-rest-refused"

    async def _request_json(
        self,
        target: Any,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise httpx.ConnectError("[Errno 111] Connection refused")

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise httpx.ConnectError("[Errno 111] Connection refused")


async def _insert_ingested_descriptor(
    *,
    session: AsyncSession,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
    embedding: list[float],
    method: str = "GET",
    path: str = "/version",
) -> None:
    """Seed one enabled ``source_kind='ingested'`` descriptor row."""
    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
        source_kind="ingested",
        method=method,
        path=path,
        handler_ref=None,
        summary="Get version.",
        description="Ingested read test op.",
        tags=[],
        parameter_schema={"type": "object", "properties": {}},
        response_schema=None,
        llm_instructions=None,
        safety_level="safe",
        requires_approval=False,
        is_enabled=True,
        embedding=embedding,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(descriptor)
    await session.commit()


# ---------------------------------------------------------------------------
# Builder shape (docs/codebase/error-message-shape.md / #1141 convention)
# ---------------------------------------------------------------------------


def test_result_connector_tls_verify_failed_shape() -> None:
    """Code, host, both remediations, raw SSL string, doc reference."""
    exc = _make_tls_verify_connect_error()
    out = result_connector_tls_verify_failed(
        "GET:/version", exc, _FakeTarget(host="vrli.lab.internal"), duration_ms=1.0
    )

    assert out.status == "error"
    assert out.error is not None
    assert out.error.startswith("connector_tls_verify_failed:")
    # Names the host.
    assert "vrli.lab.internal" in out.error
    # Names BOTH remediations: the secure path (preferred) and the
    # verify_tls=false last resort with the MITM caveat.
    assert "SSL_CERT_FILE" in out.error
    assert "verify_tls=false" in out.error
    assert "man-in-the-middle" in out.error
    # Doc reference.
    assert "docs/codebase/error-message-shape.md" in out.error

    extras = out.extras
    assert extras["error_code"] == "connector_tls_verify_failed"
    assert extras["host"] == "vrli.lab.internal"
    assert extras["exception_class"] == "ConnectError"
    # Raw SSL string preserved verbatim (it fits under the cap).
    assert extras["exception_message"] == _SSL_VERIFY_MESSAGE
    assert "CERTIFICATE_VERIFY_FAILED" in extras["exception_message"]
    assert "SSL_CERT_FILE" in extras["remediation_secure"]
    assert "verify_tls=false" in extras["remediation_last_resort"]


def test_result_connector_tls_verify_failed_caps_oversized_message() -> None:
    """An oversized raw SSL string is capped at ~256 chars in extras."""
    long_message = "[SSL: CERTIFICATE_VERIFY_FAILED] " + ("x" * 500)
    exc = httpx.ConnectError(long_message)
    out = result_connector_tls_verify_failed("GET:/version", exc, _FakeTarget(), duration_ms=0.5)
    captured = out.extras["exception_message"]
    assert captured.endswith("...<truncated>")
    assert len(captured) <= 256 + len("...<truncated>")


def test_result_connector_tls_verify_failed_missing_host_degrades_gracefully() -> None:
    """A target without ``.host`` yields a bare host label, never raises."""

    class _HostlessTarget:
        pass

    out = result_connector_tls_verify_failed(
        "GET:/version", httpx.ConnectError("boom"), _HostlessTarget(), duration_ms=1.0
    )
    assert out.extras["host"] == "the target host"
    assert "the target host" in (out.error or "")


# ---------------------------------------------------------------------------
# Dispatcher integration: the except httpx.ConnectError arm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_tls_verify_failure_to_connector_tls_verify_failed(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """TLS-verify dispatch -> structured result + audit row + event.

    The dispatcher catches the connector's chained
    :exc:`httpx.ConnectError` (cause = :exc:`ssl.SSLCertVerificationError`)
    ahead of the generic ``except Exception`` and emits the structured
    shape naming the host + both remediations -- not the pre-#1782 bare
    ``connector_error: ConnectError`` that discarded the SSL cause.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest",
        cls=_TlsVerifyFailConnector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="vcfops",
        version="9",
        impl_id="vcfops-rest",
        op_id="GET:/version",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-9",
        op_id="GET:/version",
        target=_FakeTarget(name="vrli-lab", host="vrli.lab.internal"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_tls_verify_failed:")
    # Host + both remediations in the operator-facing message.
    assert "vrli.lab.internal" in result.error
    assert "SSL_CERT_FILE" in result.error
    assert "verify_tls=false" in result.error
    assert result.extras["error_code"] == "connector_tls_verify_failed"
    assert result.extras["host"] == "vrli.lab.internal"
    assert "CERTIFICATE_VERIFY_FAILED" in result.extras["exception_message"]
    # NOT the pre-#1782 flattened shape.
    assert "connector_error" not in result.error
    assert result.extras["error_code"] != "connector_error"

    # The arm audited with result_status='error' before returning.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "GET:/version")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


@pytest.mark.asyncio
async def test_dispatch_non_ssl_connect_error_falls_through_to_connector_error(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A non-SSL ``ConnectError`` (connection refused) stays ``connector_error``.

    Narrowing boundary (#1782 AC): only TLS-verify failures are siphoned
    into ``connector_tls_verify_failed``; DNS / refused / timeout
    ``ConnectError``s fall through to the generic catch -- never
    mislabelled TLS.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-refused",
        cls=_ConnRefusedConnector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-refused",
        op_id="GET:/version",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-refused-9",
        op_id="GET:/version",
        target=_FakeTarget(name="vrli-lab", host="vrli.lab.internal"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "ConnectError"
    # Did NOT get reclassified as the TLS shape.
    assert "connector_tls_verify_failed" not in result.error
    assert "host" not in result.extras
    assert "remediation_secure" not in result.extras

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


def test_substring_fallback_classifies_when_cause_chain_empty() -> None:
    """The ``CERTIFICATE_VERIFY_FAILED`` substring fallback fires sans ``__cause__``.

    Belt-and-suspenders for any future/edge httpx that surfaces the
    verify failure in the message but leaves ``__cause__`` empty -- the
    dispatcher's ``or "CERTIFICATE_VERIFY_FAILED" in str(...)`` branch
    must still classify it as TLS. Asserts the exact predicate the
    dispatcher arm evaluates.
    """
    conn_exc = httpx.ConnectError(_SSL_VERIFY_MESSAGE)
    assert conn_exc.__cause__ is None
    is_tls_verify_failure = isinstance(conn_exc.__cause__, ssl.SSLCertVerificationError) or (
        "CERTIFICATE_VERIFY_FAILED" in str(conn_exc)
    )
    assert is_tls_verify_failure is True

    # And the negative: a refused error matches neither limb.
    refused = httpx.ConnectError("[Errno 111] Connection refused")
    assert refused.__cause__ is None
    assert not (
        isinstance(refused.__cause__, ssl.SSLCertVerificationError)
        or ("CERTIFICATE_VERIFY_FAILED" in str(refused))
    )
