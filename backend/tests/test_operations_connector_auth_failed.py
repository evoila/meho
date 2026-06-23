# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``connector_auth_failed`` dispatch error.

G0.26-T5 (#1804) acceptance criteria:

* A ``401`` :exc:`httpx.HTTPStatusError` from a connector dispatch returns
  the structured ``connector_auth_failed`` :class:`OperationResult` -- NOT
  the bare ``connector_error: HTTPStatusError`` that buried the auth cause
  in ``extras["exception_message"]`` and made the #1798 vRLI dispatch
  (seen as ``connector_error (440)``) look like a stub-auth problem. The
  operator-facing ``error`` names the connector/host, the status, the
  likely session/credential-expiry or misconfigured-``auth_model`` cause,
  and the verify-the-Vault-credential/``auth_model`` remediation.
* The recognised auth-status set is explicit and documented in the builder
  (``401`` -- the load-bearing case -- plus vRLI's ``440``, which the team
  opted to recognise); a regression test asserts ``404`` and a ``5xx``
  still return ``connector_error``.
* The ``403`` / ``422`` builders (#1649) and the
  ``ConnectError`` -> ``connector_tls_verify_failed`` (#1782) arm are
  unchanged (regression test): a ``403`` still maps to
  ``connector_http_403``.
* The new arm calls :func:`audit_and_broadcast_safe` with
  ``result_status='error'`` before returning (always-audit + never-raises
  contract): an audit row + broadcast event land.

The cause is kept **connector-agnostic** -- any upstream auth-class
status, vRLI or not, yields the structured cause. The builder-shape tests
mirror the #1649 ``test_operations_connector_http_403`` discipline
(``docs/codebase/error-message-shape.md``): stable code, diagnostic human
message naming the host with a remediation imperative + doc reference,
structured ``extras`` payload.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar
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
from meho_backplane.connectors.profile import (
    AuthSpec,
    ExecutionProfile,
    FingerprintSpec,
    PaginationSpec,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._errors import (
    is_auth_failed_status,
    result_connector_auth_failed,
)
from meho_backplane.operations.dispatcher import _profile_expiry_statuses
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Settings / isolation fixtures (the sibling dispatcher-test pattern)
# ---------------------------------------------------------------------------

_TENANT: UUID = UUID("00000000-0000-0000-0000-00000000a401")


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


def _make_operator(*, sub: str = "op-conn-auth") -> Operator:
    """Construct an :class:`Operator` directly -- no JWT round-trip."""
    return Operator(
        sub=sub,
        name="Connector-Auth Test Operator",
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


def _make_http_status_error(
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    text_body: str | None = None,
    status_code: int = 401,
) -> httpx.HTTPStatusError:
    """Build a real :exc:`httpx.HTTPStatusError` off a synthetic response.

    The error is produced the same way the production path produces it
    -- ``response.raise_for_status()`` on a non-2xx -- so the test
    exercises the genuine httpx exception shape (``exc.response`` access)
    rather than a hand-faked stand-in.
    """
    request = httpx.Request("GET", "https://vrli.lab.internal/api/v2/events")
    kwargs: dict[str, Any] = {"headers": headers or {}, "request": request}
    if json_body is not None:
        kwargs["json"] = json_body
    elif text_body is not None:
        kwargs["text"] = text_body
    response = httpx.Response(status_code, **kwargs)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError(f"status {status_code} did not raise")  # pragma: no cover


class _Http401Connector(HttpConnector):
    """Connector whose transport raises an upstream 401 (re-login failed).

    Overrides ``_request_json`` / ``_post_json`` to raise the same
    :exc:`httpx.HTTPStatusError` the real :class:`HttpConnector` raises
    when ``resp.raise_for_status()`` sees a 401 -- the session connector
    whose internal ``_get_json_with_session_retry`` already retried once
    and re-login still failed. The transport is short-circuited so the
    test needs no live HTTP / Vault.
    """

    product = "vcfops"
    version = "9"
    impl_id = "vcfops-rest"
    supported_version_range = ">=9,<10"
    priority = 1

    status_code: ClassVar[int] = 401
    response_headers: ClassVar[dict[str, str]] = {"Content-Type": "application/json"}
    response_body: ClassVar[dict[str, Any]] = {
        "message": "Authentication required: session token invalid or expired"
    }

    async def _request_json(
        self,
        target: Any,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        raise _make_http_status_error(
            headers=self.response_headers,
            json_body=self.response_body,
            status_code=self.status_code,
        )

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        raise _make_http_status_error(
            headers=self.response_headers,
            json_body=self.response_body,
            status_code=self.status_code,
        )

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
        # Ingested dispatch routes through _request_json / _post_json, not
        # execute; this concrete impl only makes the abstract base
        # instantiable.
        raise NotImplementedError


class _Http440Connector(_Http401Connector):
    """Same shape as :class:`_Http401Connector` but raises vRLI's 440.

    The literal appliance status the operator saw flattened to
    ``connector_error (440)`` on the #1798 dispatch -- the team opted to
    recognise it as the same auth class.
    """

    impl_id = "vcfops-rest-440"
    status_code: ClassVar[int] = 440
    response_body: ClassVar[dict[str, Any]] = {"message": "Login Time-out"}


class _Http440DefaultProfileConnector(_Http440Connector):
    """A profiled connector raising 440 whose profile declares only {401}.

    #1973: the dispatcher classifies a *profiled* connector's auth failure
    against its profile's ``expiry_statuses``, not the typed-connector
    global. With the default {401}-only set, a 440 is NOT a recognised
    expiry status, so it falls through to the generic ``connector_error``
    -- the opposite of the typed :class:`_Http440Connector`, proving the
    profile's declaration drives the arm.
    """

    impl_id = "vcfops-rest-440-default-profile"
    profile: ClassVar[ExecutionProfile] = ExecutionProfile(
        product="vcfops",
        version="9",
        auth=AuthSpec(scheme="session_login", secret_fields=("username", "password")),
        fingerprint=FingerprintSpec(
            path="/api/version", version_key="version", version_splitter="none"
        ),
        probe="delegate",
        pagination=PaginationSpec(strategy="none", items_key="value"),
        # expiry_statuses omitted -> default {401}.
    )


class _Http440VrliProfileConnector(_Http440Connector):
    """A profiled connector raising 440 whose profile declares {401, 440}.

    The vRLI shape expressed as a profile: 440 is a declared expiry status,
    so the dispatcher siphons it into ``connector_auth_failed`` from the
    profile's set -- the same outcome as the typed connector, but sourced
    from the single profile declaration.
    """

    impl_id = "vcfops-rest-440-vrli-profile"
    profile: ClassVar[ExecutionProfile] = ExecutionProfile(
        product="vcfops",
        version="9",
        auth=AuthSpec(scheme="session_login", secret_fields=("username", "password")),
        fingerprint=FingerprintSpec(
            path="/api/version", version_key="version", version_splitter="none"
        ),
        probe="delegate",
        pagination=PaginationSpec(strategy="none", items_key="value"),
        expiry_statuses=frozenset({401, 440}),
    )


class _Http404Connector(_Http401Connector):
    """Same shape but raises a 404 -- the non-auth fall-through boundary.

    Pins the scope boundary: a non-403/422/auth-class ``HTTPStatusError``
    falls through the dispatcher's structured branches to the generic
    ``connector_error`` flatten unchanged.
    """

    impl_id = "vcfops-rest-404"
    status_code: ClassVar[int] = 404
    response_body: ClassVar[dict[str, Any]] = {"message": "Not Found"}


class _Http500Connector(_Http401Connector):
    """Same shape but raises a 500 -- the other non-auth fall-through case."""

    impl_id = "vcfops-rest-500"
    status_code: ClassVar[int] = 500
    response_body: ClassVar[dict[str, Any]] = {"message": "Server Error"}


class _Http403Connector(_Http401Connector):
    """Same shape but raises a 403 -- the #1649 sibling-arm regression.

    The auth-class arm must not perturb #1649's ``connector_http_403``
    classification: a 403 still maps to the permission builder, not the
    new auth builder.
    """

    impl_id = "vcfops-rest-403"
    status_code: ClassVar[int] = 403
    response_headers: ClassVar[dict[str, str]] = {
        "X-Accepted-GitHub-Permissions": "issues=write",
        "Content-Type": "application/json",
    }
    response_body: ClassVar[dict[str, Any]] = {"message": "Resource not accessible by integration"}


async def _insert_ingested_descriptor(
    *,
    session: AsyncSession,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
    embedding: list[float],
    method: str = "GET",
    path: str = "/api/v2/events",
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
        summary="Get events.",
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
# The recognised auth-status set (single source of truth)
# ---------------------------------------------------------------------------


def test_is_auth_failed_status_recognises_401_and_440() -> None:
    """``401`` (load-bearing) and vRLI's ``440`` are the recognised set."""
    assert is_auth_failed_status(401) is True
    assert is_auth_failed_status(440) is True


def test_is_auth_failed_status_excludes_non_auth_statuses() -> None:
    """Non-auth statuses (403/404/422/429/5xx) are not in the set."""
    for status in (200, 403, 404, 422, 429, 500, 502, 503):
        assert is_auth_failed_status(status) is False, status


def test_is_auth_failed_status_profile_set_overrides_global() -> None:
    """A profiled connector's declared set is authoritative for its dispatch (#1973)."""
    # vRLI profile declares {401, 440} -> both classify as auth-failed.
    vrli = frozenset({401, 440})
    assert is_auth_failed_status(401, vrli) is True
    assert is_auth_failed_status(440, vrli) is True
    # The {401}-only default profile does NOT siphon 440 into auth-failed.
    default = frozenset({401})
    assert is_auth_failed_status(401, default) is True
    assert is_auth_failed_status(440, default) is False


def test_is_auth_failed_status_none_falls_back_to_global() -> None:
    """A typed connector (no profile) keeps the unchanged {401, 440} global."""
    assert is_auth_failed_status(401, None) is True
    assert is_auth_failed_status(440, None) is True
    assert is_auth_failed_status(404, None) is False


# ---------------------------------------------------------------------------
# Builder shape (docs/codebase/error-message-shape.md / #1141 convention)
# ---------------------------------------------------------------------------


def test_result_connector_auth_failed_shape_401() -> None:
    """401: code, host, status, cause, remediation, doc references, upstream msg."""
    exc = _make_http_status_error(
        status_code=401,
        json_body={"message": "session token invalid or expired"},
    )
    out = result_connector_auth_failed(
        "GET:/api/v2/events", exc, _FakeTarget(host="vrli.lab.internal"), duration_ms=1.0
    )

    assert out.status == "error"
    assert out.op_id == "GET:/api/v2/events"
    assert out.error is not None
    assert out.error.startswith("connector_auth_failed:")
    # Names the host + the actual status.
    assert "vrli.lab.internal" in out.error
    assert "401" in out.error
    # Names the likely cause (session/credential expiry OR auth_model).
    assert "auth_model" in out.error
    assert "Vault" in out.error
    # Remediation imperative ("Verify ... then retry") + both doc refs.
    assert "Verify the target's Vault credential" in out.error
    assert "docs/architecture/connector-auth.md" in out.error
    assert "docs/codebase/error-message-shape.md" in out.error
    # The upstream body message tails the operator-facing string.
    assert "session token invalid or expired" in out.error

    extras = out.extras
    assert extras["error_code"] == "connector_auth_failed"
    assert extras["http_status"] == 401
    assert extras["host"] == "vrli.lab.internal"
    assert extras["upstream_message"] == "session token invalid or expired"


def test_result_connector_auth_failed_shape_440_carries_actual_status() -> None:
    """440 (vRLI): the structured shape carries the actual status, not a hard 401."""
    exc = _make_http_status_error(status_code=440, json_body={"message": "Login Time-out"})
    out = result_connector_auth_failed("GET:/api/v2/events", exc, _FakeTarget(), duration_ms=1.0)
    assert out.error is not None
    assert out.error.startswith("connector_auth_failed:")
    assert "440" in out.error
    assert out.extras["http_status"] == 440
    assert out.extras["upstream_message"] == "Login Time-out"


def test_result_connector_auth_failed_empty_body_yields_null_message() -> None:
    """An empty auth-failure body yields ``upstream_message=None`` (no dangling tail)."""
    exc = _make_http_status_error(status_code=401)
    out = result_connector_auth_failed("GET:/x", exc, _FakeTarget(), duration_ms=1.0)
    assert out.extras["upstream_message"] is None
    assert out.error is not None
    # The operator-facing string still names the auth cause.
    assert "auth/session failure" in out.error
    # No dangling "Upstream said:" tail when there was no message.
    assert "Upstream said:" not in out.error


def test_result_connector_auth_failed_non_json_body_falls_back_to_text() -> None:
    """A non-JSON auth-failure body is surfaced verbatim as the upstream message."""
    exc = _make_http_status_error(status_code=401, text_body="Unauthorized by proxy")
    out = result_connector_auth_failed("GET:/x", exc, _FakeTarget(), duration_ms=1.0)
    assert out.extras["upstream_message"] == "Unauthorized by proxy"
    assert out.error is not None
    assert "Unauthorized by proxy" in out.error


def test_result_connector_auth_failed_caps_oversized_message() -> None:
    """A pathological upstream message is capped like the sibling builders."""
    exc = _make_http_status_error(status_code=401, json_body={"message": "x" * 400})
    out = result_connector_auth_failed("GET:/x", exc, _FakeTarget(), duration_ms=0.5)
    message = out.extras["upstream_message"]
    assert isinstance(message, str)
    assert message.endswith("...<truncated>")
    assert len(message) == 256 + len("...<truncated>")


def test_result_connector_auth_failed_missing_host_degrades_gracefully() -> None:
    """A target without ``.host`` yields a bare host label, never raises."""

    class _HostlessTarget:
        pass

    exc = _make_http_status_error(status_code=401)
    out = result_connector_auth_failed("GET:/x", exc, _HostlessTarget(), duration_ms=1.0)
    assert out.extras["host"] == "the target host"
    assert "the target host" in (out.error or "")


# ---------------------------------------------------------------------------
# Dispatcher conversion (the #1804 acceptance-criterion unit tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_converts_401_to_connector_auth_failed(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """401 dispatch -> structured ``connector_auth_failed`` + audit row + event.

    The dispatcher catches the connector's :exc:`httpx.HTTPStatusError`
    (401) ahead of the generic ``except Exception`` and emits the
    structured shape naming the host + remediation -- not the pre-#1804
    bare ``connector_error: HTTPStatusError`` that buried the auth cause
    in ``extras["exception_message"]`` and made #1798 unactionable.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest",
        cls=_Http401Connector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="vcfops",
        version="9",
        impl_id="vcfops-rest",
        op_id="GET:/api/v2/events",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-9",
        op_id="GET:/api/v2/events",
        target=_FakeTarget(name="vrli-lab", host="vrli.lab.internal"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_auth_failed:")
    # Host + remediation in the operator-facing message.
    assert "vrli.lab.internal" in result.error
    assert "Verify the target's Vault credential" in result.error
    assert result.extras["error_code"] == "connector_auth_failed"
    assert result.extras["http_status"] == 401
    assert result.extras["host"] == "vrli.lab.internal"
    # NOT the pre-#1804 flattened shape.
    assert "connector_error" not in result.error
    assert result.extras["error_code"] != "connector_error"
    assert "exception_message" not in result.extras

    # The arm audited with result_status='error' before returning.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "GET:/api/v2/events")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


@pytest.mark.asyncio
async def test_dispatch_converts_440_to_connector_auth_failed(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """vRLI's 440 dispatch -> structured ``connector_auth_failed`` (the #1798 status).

    The literal status the operator saw flattened to ``connector_error
    (440)``; it now maps to the same actionable auth class.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-440",
        cls=_Http440Connector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-440",
        op_id="GET:/api/v2/events",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-440-9",
        op_id="GET:/api/v2/events",
        target=_FakeTarget(name="vrli-lab", host="vrli.lab.internal"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_auth_failed:")
    assert "440" in result.error
    assert result.extras["error_code"] == "connector_auth_failed"
    assert result.extras["http_status"] == 440
    # NOT the pre-#1804 flattened shape the operator saw on #1798.
    assert "connector_error" not in result.error

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


@pytest.mark.asyncio
async def test_dispatch_404_falls_through_to_connector_error(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A 404 ``HTTPStatusError`` is unchanged -- generic ``connector_error`` flatten.

    Scope boundary (#1804 AC): only the auth-class set (401 / 440) is
    siphoned into ``connector_auth_failed``; every other status (404 here)
    falls through to the existing generic catch.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-404",
        cls=_Http404Connector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-404",
        op_id="GET:/api/v2/events",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-404-9",
        op_id="GET:/api/v2/events",
        target=_FakeTarget(name="vrli-lab", host="vrli.lab.internal"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "HTTPStatusError"
    # Did NOT get reclassified as the auth shape.
    assert "connector_auth_failed" not in result.error
    assert "http_status" not in result.extras
    assert "host" not in result.extras

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


@pytest.mark.asyncio
async def test_dispatch_500_falls_through_to_connector_error(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A 5xx ``HTTPStatusError`` is unchanged -- generic ``connector_error`` flatten."""
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-500",
        cls=_Http500Connector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-500",
        op_id="GET:/api/v2/events",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-500-9",
        op_id="GET:/api/v2/events",
        target=_FakeTarget(name="vrli-lab", host="vrli.lab.internal"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras["error_code"] == "connector_error"
    # Did NOT get reclassified as the auth shape.
    assert "connector_auth_failed" not in result.error
    assert "http_status" not in result.extras

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


@pytest.mark.asyncio
async def test_dispatch_403_still_maps_to_connector_http_403(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """#1649 regression: a 403 still -> ``connector_http_403``, not the auth shape.

    The new ``connector_auth_failed`` branch sits *after* the 403 / 422
    checks in the same ``httpx.HTTPStatusError`` arm; 403 must still reach
    the permission builder.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-403",
        cls=_Http403Connector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-403",
        op_id="GET:/api/v2/events",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-403-9",
        op_id="GET:/api/v2/events",
        target=_FakeTarget(name="vrli-lab", host="vrli.lab.internal"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_http_403:")
    assert result.extras["error_code"] == "connector_http_403"
    assert result.extras["http_status"] == 403
    # Did NOT get reclassified as the auth shape.
    assert "connector_auth_failed" not in result.error

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


# ---------------------------------------------------------------------------
# Profile-declared expiry-status set (#1973): one source feeds the arm
# ---------------------------------------------------------------------------


def test_profile_expiry_statuses_reads_attached_profile() -> None:
    """The dispatcher helper extracts the profile's declared set."""
    assert _profile_expiry_statuses(_Http440VrliProfileConnector()) == frozenset({401, 440})
    assert _profile_expiry_statuses(_Http440DefaultProfileConnector()) == frozenset({401})


def test_profile_expiry_statuses_none_for_typed_connector() -> None:
    """A typed connector (no profile attr) yields None -> the global fallback."""
    assert _profile_expiry_statuses(_Http440Connector()) is None
    assert _profile_expiry_statuses(None) is None


@pytest.mark.asyncio
async def test_dispatch_440_with_default_profile_falls_through(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """#1973: a profiled connector with the default {401} set does NOT siphon 440.

    The profile is the single source: its {401}-only declaration means a
    440 is not a recognised expiry status for *this* connector, so it falls
    through to the generic ``connector_error`` -- proving the profile, not
    the typed-connector global, drives the dispatcher's auth-class arm.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-440-default-profile",
        cls=_Http440DefaultProfileConnector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-440-default-profile",
        op_id="GET:/api/v2/events",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-440-default-profile-9",
        op_id="GET:/api/v2/events",
        target=_FakeTarget(name="vrli-lab", host="vrli.lab.internal"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras["error_code"] == "connector_error"
    # The {401}-only profile did NOT reclassify 440 as the auth shape.
    assert "connector_auth_failed" not in result.error


@pytest.mark.asyncio
async def test_dispatch_440_with_vrli_profile_maps_to_auth_failed(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """#1973: a profiled connector declaring {401, 440} siphons 440 from one source.

    The vRLI shape expressed as a profile reaches the same
    ``connector_auth_failed`` outcome as the typed connector, but the status
    set comes from the profile declaration -- the same one the session-retry
    harness reads.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-440-vrli-profile",
        cls=_Http440VrliProfileConnector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-440-vrli-profile",
        op_id="GET:/api/v2/events",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-440-vrli-profile-9",
        op_id="GET:/api/v2/events",
        target=_FakeTarget(name="vrli-lab", host="vrli.lab.internal"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_auth_failed:")
    assert result.extras["error_code"] == "connector_auth_failed"
    assert result.extras["http_status"] == 440
