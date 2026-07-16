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
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors._shared.vcf_auth import (
    ConnectorAuthError,
    session_establish_auth_error,
)
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.profile import (
    AuthSpec,
    ExecutionProfile,
    FingerprintSpec,
    PaginationSpec,
)
from meho_backplane.connectors.profiled import ProfiledRestConnector
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
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
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


class _EstablishAuthConnector(_Http401Connector):
    """Connector whose session **establish** raises :class:`ConnectorAuthError`.

    #2329: models the family establish sites (vSphere ``/api/session`` etc.)
    after the fix -- a login POST that returns 401/403 raises the structured
    ``ConnectorAuthError`` (chained from the transport error) instead of a
    bare ``RuntimeError``. Overrides ``_request_json`` to raise it directly so
    the dispatcher's new ``except ConnectorAuthError`` main-ladder arm is
    exercised without live HTTP. It has NO ``invalidate_session`` hook, so the
    dispatcher does not attempt the #2067 mid-session retry -- an establish-time
    bad password fails fast.
    """

    impl_id = "vcfops-rest-establish"
    establish_status: ClassVar[int] = 401

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
        http_exc = _make_http_status_error(
            headers=self.response_headers,
            json_body={"message": "Cannot complete login: incorrect user name or password"},
            status_code=self.establish_status,
        )
        message = f"vsphere session establish failed for target {getattr(target, 'name', None)!r}"
        raise (
            session_establish_auth_error(http_exc, message=message, target=target)
            or RuntimeError(message)
        ) from http_exc


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


class _ProfiledEstablish401Connector(ProfiledRestConnector):
    """A real profiled connector whose ``session_login`` login POST returns 401.

    #2414: unlike the ``_Http4xx`` fakes (which short-circuit ``_request_json``),
    this exercises the genuine ``ProfiledRestConnector`` auth flow --
    ``auth_headers`` -> ``_session_token`` -> ``_mint_session_token`` ->
    ``_post_login`` -- so the login-POST 401 travels the exact path the bug
    traversed. A profiled connector advertises ``invalidate_session``, so before
    the fix the raw login-POST ``HTTPStatusError`` was re-dispatched once and
    stamped ``session_dispatch_401_after_relogin``; after the fix ``_post_login``
    raises ``ConnectorAuthError`` (establish stage) -> ``session_establish_401``.
    The login endpoint is mocked with respx; the credential loader is stubbed so
    no Vault read is needed (the dispatcher instantiates the class with no args).
    """

    product = "vcfops"
    version = "9"
    impl_id = "vcfops-profiled-establish-401"
    supported_version_range = ">=9,<10"
    profile: ClassVar[ExecutionProfile] = ExecutionProfile(
        product="vcfops",
        version="9",
        auth=AuthSpec(scheme="session_login", secret_fields=("username", "password")),
        fingerprint=FingerprintSpec(
            path="/api/version", version_key="version", version_splitter="none"
        ),
        probe="delegate",
        pagination=PaginationSpec(strategy="none", items_key="value"),
    )

    def __init__(self) -> None:
        async def _loader(_target: Any, _operator: Operator) -> dict[str, str]:
            return {"username": "svc", "password": "pw"}

        super().__init__(credentials_loader=_loader)


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
    # Names the likely cause (per-request credential rejected OR auth scheme).
    assert "auth_model" in out.error
    assert "Vault" in out.error
    # #2400: the default (no invalidate_session hook) is the ``dispatch`` stage,
    # so no session stage happened -- the false "already re-logged-in and
    # retried once" sentence must NOT appear here.
    assert "already re-logged-in" not in out.error
    # #2400 remediation imperative names the REAL staging command.
    assert "meho vault kv put" in out.error
    assert "meho target credential set" not in out.error
    assert "docs/architecture/connector-auth.md" in out.error
    assert "docs/codebase/error-message-shape.md" in out.error
    # The upstream body message tails the operator-facing string.
    assert "session token invalid or expired" in out.error

    extras = out.extras
    assert extras["error_code"] == "connector_auth_failed"
    # #2400: no session stage happened -> the cause drops the ``session_``
    # prefix (it is not ``session_dispatch_401``).
    assert extras["cause"] == "dispatch_401"
    assert extras["http_status"] == 401
    assert extras["target"] == "vrli-lab"
    assert extras["host"] == "vrli.lab.internal"
    assert extras["secret_ref"] is None
    assert "meho vault kv put" in extras["remediation"]
    # The httpx (dispatch) path has no connector-composed establish message.
    assert extras["raw_message"] is None
    assert extras["upstream_message"] == "session token invalid or expired"


def test_result_connector_auth_failed_after_relogin_cause_and_no_restage() -> None:
    """#2400: ``reestablished`` -> ``_after_relogin`` cause + scheme (not restage) remediation.

    The forced re-login SUCCEEDED and the fresh session was STILL rejected, so
    the credential logs in fine: the ONLY stage that keeps the "already
    re-logged-in and retried once" sentence, and its remediation must NOT tell
    the operator to restage.
    """
    exc = _make_http_status_error(
        status_code=401,
        json_body={"message": "session token invalid or expired"},
    )
    out = result_connector_auth_failed(
        "GET:/api/v2/events",
        exc,
        _FakeTarget(name="vrops-lab", host="vrops.lab.internal"),
        duration_ms=1.0,
        relogin="reestablished",
    )
    assert out.error is not None
    # The re-login sentence lives here (and, per the sibling tests, nowhere else).
    assert "already re-logged-in and retried once" in out.error
    # Do-NOT-restage guidance; no phantom command, and no restage command.
    assert "do NOT restage" in out.error
    assert "meho target credential set" not in out.error
    assert "meho vault kv put" not in out.error
    assert out.extras["cause"] == "session_dispatch_401_after_relogin"
    assert out.extras["http_status"] == 401
    assert "do NOT restage" in out.extras["remediation"]


def test_result_connector_auth_failed_dispatch_causes_carry_no_session_prefix() -> None:
    """#2400: the no-session-stage cause is ``dispatch_<s>`` for every auth-class status."""
    for status in (401, 440):
        exc = _make_http_status_error(status_code=status, json_body={})
        out = result_connector_auth_failed(
            "GET:/api/v2/events", exc, _FakeTarget(), duration_ms=1.0
        )
        assert out.extras["cause"] == f"dispatch_{status}"
        assert out.error is not None
        assert "already re-logged-in" not in out.error


def test_auth_failed_remediation_names_only_commands_that_exist() -> None:
    """#2400: the remediation names only real CLI commands, not the phantom one.

    Grounds the acceptance criterion against the CLI tree: the referenced verb
    must be a real cobra command, and the phantom command that sent both MFC
    401 reporters (#2395, #2396) down a credential rabbit hole must exist
    nowhere in the CLI.
    """
    cli_root = Path(__file__).resolve().parents[2] / "cli"
    assert cli_root.is_dir(), f"CLI tree not found at {cli_root}"
    go_sources = "\n".join(p.read_text() for p in cli_root.rglob("*.go"))

    # The phantom command exists in neither the emitted text nor the CLI tree.
    assert "meho target credential set" not in go_sources
    assert "credential set" not in go_sources

    # The staging verb the remediation names is a real cobra command:
    # `meho vault` -> `kv` -> `put <mount> <path>`.
    assert 'Use:   "vault"' in go_sources or 'Use:          "vault"' in go_sources
    assert '"kv"' in go_sources
    assert 'Use:   "put <mount> <path>"' in go_sources

    # Every stage's emitted remediation/summary names only that real command
    # (or, for after_relogin, no command at all).
    exc = _make_http_status_error(status_code=401, json_body={})
    dispatch_out = result_connector_auth_failed("op", exc, _FakeTarget(), duration_ms=1.0)
    assert "meho vault kv put" in dispatch_out.extras["remediation"]
    assert "meho target credential set" not in dispatch_out.error

    after = result_connector_auth_failed(
        "op", exc, _FakeTarget(), duration_ms=1.0, relogin="reestablished"
    )
    assert "meho target credential set" not in after.error
    assert "meho vault kv put" not in after.error  # do-not-restage stage names no command


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
# Establish-path builder shape (#2329 ConnectorAuthError branch)
# ---------------------------------------------------------------------------


def _make_establish_auth_error(
    *,
    status_code: int,
    target: object,
    json_body: dict[str, Any] | None = None,
) -> ConnectorAuthError:
    """Build a chained :class:`ConnectorAuthError` the way an establish site does.

    Mirrors the production ``raise session_establish_auth_error(...) from exc``
    seam so ``exc.__cause__`` carries the underlying transport error the
    builder reads the upstream body from.
    """
    http_exc = _make_http_status_error(status_code=status_code, json_body=json_body)
    message = f"vsphere session establish failed for target {getattr(target, 'name', None)!r}"
    auth_err = session_establish_auth_error(http_exc, message=message, target=target)
    assert auth_err is not None
    try:
        raise auth_err from http_exc
    except ConnectorAuthError as chained:
        return chained


def test_session_establish_auth_error_classifies_401_and_403() -> None:
    """401/403 at establish yield a ConnectorAuthError; other statuses do not."""
    target = _FakeTarget(name="vc-a", host="vc-a.lab")
    for status in (401, 403):
        exc = _make_http_status_error(status_code=status)
        err = session_establish_auth_error(exc, message="m", target=target)
        assert isinstance(err, ConnectorAuthError)
        assert err.status_code == status
        assert err.cause == f"session_establish_{status}"
        assert err.target_name == "vc-a"
    for status in (404, 500, 502):
        exc = _make_http_status_error(status_code=status)
        assert session_establish_auth_error(exc, message="m", target=target) is None


def test_result_connector_auth_failed_establish_401_shape() -> None:
    """Establish 401 -> cause sub-code + target + secret_ref + remediation + raw_message."""

    class _SecretTarget:
        name = "vc-prod"
        host = "vc-prod.lab.internal"
        secret_ref = "tenants/t1/vc-prod"

    auth_err = _make_establish_auth_error(
        status_code=401,
        target=_SecretTarget(),
        json_body={"message": "Cannot authenticate user"},
    )
    out = result_connector_auth_failed("vmware.vm.info", auth_err, _SecretTarget(), duration_ms=2.0)

    assert out.error is not None
    assert out.error.startswith("connector_auth_failed:")
    assert "vc-prod.lab.internal" in out.error
    assert "401" in out.error
    # #2400: establish stage names the login-rejected story + the REAL restage
    # command + the target's secret_ref.
    assert "login itself was rejected" in out.error
    assert "meho vault kv put" in out.error
    assert "meho target credential set" not in out.error
    assert "tenants/t1/vc-prod" in out.error

    extras = out.extras
    assert extras["error_code"] == "connector_auth_failed"
    assert extras["cause"] == "session_establish_401"
    assert extras["http_status"] == 401
    assert extras["target"] == "vc-prod"
    assert extras["host"] == "vc-prod.lab.internal"
    assert extras["secret_ref"] == "tenants/t1/vc-prod"
    assert "meho vault kv put" in extras["remediation"]
    # The connector's establish message is preserved for debugging.
    assert isinstance(extras["raw_message"], str)
    assert "session establish failed" in extras["raw_message"]
    # The upstream body (chained via ``__cause__``) still tails the message.
    assert extras["upstream_message"] == "Cannot authenticate user"


def test_result_connector_auth_failed_establish_403_cause_subcode() -> None:
    """Establish 403 (locked-out account) carries the ``session_establish_403`` cause."""
    auth_err = _make_establish_auth_error(status_code=403, target=_FakeTarget(name="nsx-a"))
    out = result_connector_auth_failed("GET:/api/x", auth_err, _FakeTarget(name="nsx-a"), 1.0)
    assert out.extras["cause"] == "session_establish_403"
    assert out.extras["http_status"] == 403
    assert out.extras["target"] == "nsx-a"


def test_connector_auth_error_is_session_login_error() -> None:
    """Subclassing keeps every ``except SessionLoginError`` / RuntimeError caller working."""
    from meho_backplane.connectors._shared.vcf_auth import SessionLoginError

    err = ConnectorAuthError("m", status_code=401, cause="session_establish_401")
    assert isinstance(err, SessionLoginError)
    assert isinstance(err, RuntimeError)


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
    # Host + remediation in the operator-facing message. This connector has no
    # invalidate_session hook -> #2400 ``dispatch_401`` stage (no re-login).
    assert "vrli.lab.internal" in result.error
    assert "meho vault kv put" in result.error
    assert "meho target credential set" not in result.error
    assert "already re-logged-in" not in result.error
    assert result.extras["cause"] == "dispatch_401"
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
async def test_dispatch_converts_establish_auth_error_to_connector_auth_failed(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """#2329: an establish-time ``ConnectorAuthError`` -> ``connector_auth_failed``.

    A connector whose session establish raises the structured
    ``ConnectorAuthError`` (a rotated/stale password rejected at the login
    POST) is caught by the dispatcher's new ``except ConnectorAuthError`` arm
    ahead of the generic ``except Exception`` -- NOT flattened to the bare
    ``connector_error: RuntimeError`` the filing observed. The audit row +
    broadcast event still land (always-audit contract).
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-establish",
        cls=_EstablishAuthConnector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-establish",
        op_id="GET:/api/v2/events",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-establish-9",
        op_id="GET:/api/v2/events",
        target=_FakeTarget(name="vc-prod", host="vc-prod.lab.internal"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_auth_failed:")
    assert result.extras["error_code"] == "connector_auth_failed"
    assert result.extras["cause"] == "session_establish_401"
    assert result.extras["http_status"] == 401
    assert result.extras["target"] == "vc-prod"
    assert "meho vault kv put" in result.extras["remediation"]
    assert "meho target credential set" not in result.error
    # NOT the bare shape the filing observed.
    assert "connector_error" not in result.error
    assert "exception_message" not in result.extras

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


@pytest.mark.asyncio
async def test_dispatch_profiled_login_post_401_is_establish_not_after_relogin(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """#2414: a profiled login-POST 401 stamps establish stage, not after_relogin.

    Red-today end-to-end regression against the exact path the bug traversed. A
    real :class:`ProfiledRestConnector` dispatches an ingested op; its
    ``session_login`` establish POST returns 401. Before the fix the raw
    ``HTTPStatusError`` reached the dispatcher's retry arm (the connector
    advertises ``invalidate_session``) and was stamped
    ``session_dispatch_401_after_relogin`` with the do-NOT-restage remediation.
    After the fix ``_post_login`` raises ``ConnectorAuthError`` -> the
    ``session_establish_401`` cause + the restage remediation, and the
    ``already re-logged-in and retried once`` / ``do NOT restage`` text is
    absent.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-profiled-establish-401",
        cls=_ProfiledEstablish401Connector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="vcfops",
        version="9",
        impl_id="vcfops-profiled-establish-401",
        op_id="GET:/api/v2/events",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    target = _FakeTarget(name="vrli-lab", host="vrli.lab.internal")
    # The real profiled connector keys its session/client cache on the
    # tenant-unique ``(tenant_id, id)`` tuple (the typed ``_Http4xx`` fakes
    # never reach ``target_cache_key`` because they short-circuit
    # ``_request_json``), so the target needs a ``tenant_id``.
    target.tenant_id = _TENANT
    async with respx.mock(base_url="https://vrli.lab.internal") as mock:
        login = mock.post("/api/v2/sessions").respond(401, json={"message": "login refused"})
        result = await dispatch(
            operator=_make_operator(),
            connector_id="vcfops-profiled-establish-401-9",
            op_id="GET:/api/v2/events",
            target=target,
            params={},
        )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_auth_failed:")
    assert result.extras["error_code"] == "connector_auth_failed"
    # The load-bearing assertion: establish stage, NOT the retry arm's
    # after_relogin cause.
    assert result.extras["cause"] == "session_establish_401"
    assert result.extras["cause"] != "session_dispatch_401_after_relogin"
    # The establish stage carries the restage remediation, not the
    # do-NOT-restage after_relogin text.
    assert "meho vault kv put" in result.error
    assert "do NOT restage" not in result.error
    assert "already re-logged-in and retried once" not in result.error
    # The login POST was attempted; the establish failed fast (no wasteful
    # re-dispatch loop -- the ConnectorAuthError arm does not retry).
    assert login.call_count == 1
    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


# ---------------------------------------------------------------------------
# Dispatch-path session-expiry recovery (G0.29-T2 #2067)
# ---------------------------------------------------------------------------
#
# These assert at the level the bug actually traverses: a seeded
# ``source_kind='ingested'`` descriptor dispatched via ``dispatch()`` ->
# ``dispatch_ingested`` -> the connector's ``_request_json`` / ``_post_json``.
# The pre-#2067 ``test_connectors_vcf_logs_e2e.py`` drives
# ``_get_json_with_session_retry`` directly and stayed green while the bug
# shipped, because that helper has no caller on this path.


class _RecoveringConnector(_Http401Connector):
    """Auth-fails the first transport call per cache-key, then succeeds.

    Models a connector whose cached session expired server-side: the first
    dispatched call gets an auth-class status, and -- once the dispatcher
    calls :meth:`invalidate_session` and re-dispatches -- the next call (a
    fresh login on the connector's side) succeeds. Per-``(tenant_id,
    target.id)`` call counts let the tests assert exactly-one-retry and the
    cross-tenant isolation guard.
    """

    impl_id = "vcfops-rest-recovering"
    status_code: ClassVar[int] = 401

    def __init__(self) -> None:
        super().__init__()
        self.attempts: dict[tuple[str, str], int] = {}
        self.invalidations: list[tuple[str, str]] = []

    def _next(self, target: Any, method: str, path: str) -> dict[str, Any]:
        key = (str(getattr(target, "tenant_id", "")), str(getattr(target, "id", "")))
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if self.attempts[key] == 1:
            raise _make_http_status_error(
                headers=self.response_headers,
                json_body=self.response_body,
                status_code=self.status_code,
            )
        return {"ok": True, "method": method, "path": path}

    async def _request_json(  # type: ignore[override]
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
        return self._next(target, method, path)

    async def _post_json(  # type: ignore[override]
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
        return self._next(target, verb, path)

    async def invalidate_session(self, target: Any) -> None:
        self.invalidations.append(
            (str(getattr(target, "tenant_id", "")), str(getattr(target, "id", "")))
        )


class _Recovering440Connector(_RecoveringConnector):
    """vRLI shape: auth-fails once with a 440, then succeeds."""

    impl_id = "vcfops-rest-recovering-440"
    status_code: ClassVar[int] = 440
    response_body: ClassVar[dict[str, Any]] = {"message": "Login Time-out"}


class _PersistentAuthFailConnector(_RecoveringConnector):
    """Has the hook but auth-fails *every* call -- re-login genuinely failed."""

    impl_id = "vcfops-rest-persistent-401"

    def _next(self, target: Any, method: str, path: str) -> dict[str, Any]:
        key = (str(getattr(target, "tenant_id", "")), str(getattr(target, "id", "")))
        self.attempts[key] = self.attempts.get(key, 0) + 1
        raise _make_http_status_error(
            headers=self.response_headers,
            json_body=self.response_body,
            status_code=self.status_code,
        )


class _Recovering500Connector(_RecoveringConnector):
    """5xx on the first call -- the not-an-auth-failure boundary.

    Has the ``invalidate_session`` hook, but a 5xx must NOT trip the
    retry-and-invalidate seam (it may have executed); it flattens straight to
    ``connector_error`` with the session never invalidated.
    """

    impl_id = "vcfops-rest-recovering-500"
    status_code: ClassVar[int] = 500
    response_body: ClassVar[dict[str, Any]] = {"message": "Server Error"}


async def _seed(
    *,
    session: AsyncSession,
    impl_id: str,
    embedding: list[float],
    method: str = "GET",
    op_id: str = "GET:/api/v2/events",
    path: str = "/api/v2/events",
) -> None:
    await _insert_ingested_descriptor(
        session=session,
        product="vcfops",
        version="9",
        impl_id=impl_id,
        op_id=op_id,
        embedding=embedding,
        method=method,
        path=path,
    )


@pytest.mark.asyncio
async def test_dispatch_recovers_401_via_invalidate_and_retry_once(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A typed-vCenter-shape 401 on an expired session recovers via one re-login.

    The dispatcher evicts the cached session (``invalidate_session``) and
    re-dispatches the same ingested GET exactly once; the retry succeeds and
    the call returns ``ok`` -- no backplane restart. Exactly one *success*
    audit row, no spurious error row.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-recovering",
        cls=_RecoveringConnector,
    )
    await _seed(
        session=session,
        impl_id="vcfops-rest-recovering",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    target = _FakeTarget(name="rdc-vcenter", host="vcenter.lab.internal")
    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-recovering-9",
        op_id="GET:/api/v2/events",
        target=target,
        params={},
    )

    assert result.status == "ok", result.error
    # The dispatcher cached one instance for the class; fetch it to inspect
    # the per-cache-key attempt count + invalidations.
    connector = get_or_create_connector_instance(_RecoveringConnector)
    assert isinstance(connector, _RecoveringConnector)
    # Exactly two transport attempts (initial 401 + recovered retry), one
    # invalidation, for this single cache key.
    key = ("", str(target.id))  # _FakeTarget has no tenant_id attr -> ""
    assert connector.attempts[key] == 2
    assert connector.invalidations == [key]

    # Exactly one audit row, and it is the *success* row (no spurious error).
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "GET:/api/v2/events")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "ok"
    assert len(captured_events) == 1
    assert captured_events[0].result_status == "ok"


@pytest.mark.asyncio
async def test_dispatch_recovers_440_via_invalidate_and_retry_once(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A generic-ingested vRLI GET that 440s after idle-expiry recovers once.

    This is the soak-confirmed #1135/#1139 case: the dispatched op (NOT the
    uncalled ``_get_json_with_session_retry`` helper) recovers via one
    automatic re-login + single retry.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-recovering-440",
        cls=_Recovering440Connector,
    )
    await _seed(
        session=session,
        impl_id="vcfops-rest-recovering-440",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    target = _FakeTarget(name="vrli-lab", host="vrli.lab.internal")
    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-recovering-440-9",
        op_id="GET:/api/v2/events",
        target=target,
        params={},
    )

    assert result.status == "ok", result.error
    connector = get_or_create_connector_instance(_Recovering440Connector)
    assert isinstance(connector, _Recovering440Connector)
    assert connector.attempts[("", str(target.id))] == 2
    assert len(connector.invalidations) == 1
    assert len(captured_events) == 1
    assert captured_events[0].result_status == "ok"


@pytest.mark.asyncio
async def test_dispatch_recovers_non_idempotent_post_on_401(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A non-idempotent POST on an expired session also recovers via one retry.

    A 401/440 is rejected pre-execution (the stale token never reached the
    op), so retry-once is safe for POST too -- the AC's
    ``POST:/vcenter/*`` recovery.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-recovering",
        cls=_RecoveringConnector,
    )
    await _seed(
        session=session,
        impl_id="vcfops-rest-recovering",
        embedding=stub_embedding_service.encode_one.return_value,
        method="POST",
        op_id="POST:/api/v2/events",
        path="/api/v2/events",
    )

    target = _FakeTarget(name="rdc-vcenter", host="vcenter.lab.internal")
    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-recovering-9",
        op_id="POST:/api/v2/events",
        target=target,
        params={},
    )

    assert result.status == "ok", result.error
    connector = get_or_create_connector_instance(_RecoveringConnector)
    assert isinstance(connector, _RecoveringConnector)
    assert connector.attempts[("", str(target.id))] == 2
    assert connector.invalidations == [("", str(target.id))]


@pytest.mark.asyncio
async def test_dispatch_second_auth_failure_resolves_to_auth_failed_no_loop(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Re-login also fails -> ``connector_auth_failed`` with no retry loop.

    Exactly two transport attempts (initial + the single retry); the second
    auth failure falls through to the structured error, and exactly one
    *error* audit row lands.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-persistent-401",
        cls=_PersistentAuthFailConnector,
    )
    await _seed(
        session=session,
        impl_id="vcfops-rest-persistent-401",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    target = _FakeTarget(name="rdc-vcenter", host="vcenter.lab.internal")
    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-persistent-401-9",
        op_id="GET:/api/v2/events",
        target=target,
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_auth_failed:")
    assert result.extras["error_code"] == "connector_auth_failed"
    # #2400: the re-login succeeded (invalidate_session ran, the re-dispatch
    # re-established) yet the fresh session was still rejected -> the ONLY
    # stage that carries the ``_after_relogin`` qualifier + the re-login
    # sentence, and its remediation must NOT tell the operator to restage.
    assert result.extras["cause"] == "session_dispatch_401_after_relogin"
    assert "already re-logged-in and retried once" in result.error
    assert "do NOT restage" in result.error
    assert "meho target credential set" not in result.error
    connector = get_or_create_connector_instance(_PersistentAuthFailConnector)
    assert isinstance(connector, _PersistentAuthFailConnector)
    # Initial attempt + exactly one retry, then it gave up (no loop).
    assert connector.attempts[("", str(target.id))] == 2
    assert len(connector.invalidations) == 1

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
async def test_dispatch_5xx_with_hook_is_not_retried(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A 5xx is never retried, even when the connector has the hook.

    Recovery is gated on the auth-class set only -- a 5xx may have executed,
    so it flattens straight to ``connector_error`` with the session never
    invalidated (exactly one transport attempt).
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-recovering-500",
        cls=_Recovering500Connector,
    )
    await _seed(
        session=session,
        impl_id="vcfops-rest-recovering-500",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    target = _FakeTarget(name="rdc-vcenter", host="vcenter.lab.internal")
    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-recovering-500-9",
        op_id="GET:/api/v2/events",
        target=target,
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert "connector_auth_failed" not in result.error
    connector = get_or_create_connector_instance(_Recovering500Connector)
    assert isinstance(connector, _Recovering500Connector)
    # No retry, no invalidation.
    assert connector.attempts[("", str(target.id))] == 1
    assert connector.invalidations == []


@pytest.mark.asyncio
async def test_dispatch_stateless_connector_without_hook_is_not_retried(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A connector with no ``invalidate_session`` hook is unaffected (no retry).

    :class:`_Http401Connector` is stateless (no hook). A 401 still maps to
    ``connector_auth_failed`` exactly as before #2067, with a single
    transport attempt -- the seam never fires.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest",
        cls=_Http401Connector,
    )
    await _seed(
        session=session,
        impl_id="vcfops-rest",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    assert not hasattr(_Http401Connector, "invalidate_session")
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
    assert result.extras["http_status"] == 401


@pytest.mark.asyncio
async def test_dispatch_recovery_preserves_per_tenant_cache_isolation(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Two same-named targets in different tenants recover independently.

    The invalidate-and-retry is keyed on ``(tenant_id, target.id)`` via the
    connector's own per-key state, so evicting + recovering one target never
    collapses or clobbers the other's slot (#1642/#1672/#1684).
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-recovering",
        cls=_RecoveringConnector,
    )
    await _seed(
        session=session,
        impl_id="vcfops-rest-recovering",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    # Two distinct targets (distinct ``.id``) standing in for two tenants'
    # same-named appliance. Each independently fails-once-then-recovers.
    target_a = _FakeTarget(name="vcenter", host="vcenter.lab.internal")
    target_b = _FakeTarget(name="vcenter", host="vcenter.lab.internal")
    assert target_a.id != target_b.id

    for tgt in (target_a, target_b):
        result = await dispatch(
            operator=_make_operator(),
            connector_id="vcfops-rest-recovering-9",
            op_id="GET:/api/v2/events",
            target=tgt,
            params={},
        )
        assert result.status == "ok", result.error

    connector = get_or_create_connector_instance(_RecoveringConnector)
    assert isinstance(connector, _RecoveringConnector)
    # Each cache key took its own initial-fail + one-retry; neither leaked
    # into the other's count.
    assert connector.attempts[("", str(target_a.id))] == 2
    assert connector.attempts[("", str(target_b.id))] == 2
    assert ("", str(target_a.id)) in connector.invalidations
    assert ("", str(target_b.id)) in connector.invalidations


# ---------------------------------------------------------------------------
# #2396: establish-auth-failure credential eviction
#
# The dispatcher must call a duck-typed ``invalidate_credentials(target)`` hook
# from BOTH ``ConnectorAuthError`` arms -- the first-establish arm on the main
# ladder and the post-invalidation arm inside
# ``_retry_after_session_invalidation`` -- so a connector that cached the
# credential bytes it read from Vault *before* a rejected login re-reads the
# store on the next dispatch after an operator restage, with no backplane
# restart. The hook is duck-typed (getattr) so its absence stays harmless.
# ---------------------------------------------------------------------------


class _EstablishAuthEvictConnector(_EstablishAuthConnector):
    """Establish-auth-fails AND advertises the #2396 credential-eviction hook.

    Models a real caching connector (the sddc_manager shape): its login POST
    401s with credential bytes it cached before the attempt, and it exposes the
    duck-typed ``invalidate_credentials`` hook the dispatcher must call from the
    first-establish ``ConnectorAuthError`` arm. Records evictions keyed on
    ``(tenant_id, target.id)`` so the test can assert exactly-one eviction.
    """

    impl_id = "vcfops-rest-establish-evict"

    def __init__(self) -> None:
        super().__init__()
        self.evictions: list[tuple[str, str]] = []

    async def invalidate_credentials(self, target: Any) -> None:
        self.evictions.append(
            (str(getattr(target, "tenant_id", "")), str(getattr(target, "id", "")))
        )


class _RetryThenEstablishAuthConnector(_Http401Connector):
    """401 on first dispatch, then a ``ConnectorAuthError`` on the #2067 re-dispatch.

    Models the mid-session-recovery path converging on #2396: a stale session
    token 401s, ``invalidate_session`` evicts it, and the cold-cache
    re-establish re-reads Vault yet the (also-stale) credential is itself
    rejected -- the login POST raises ``ConnectorAuthError``, landing in the
    dispatcher's post-invalidation ``ConnectorAuthError`` arm inside
    ``_retry_after_session_invalidation``. That arm must evict credentials so
    the next dispatch after a restage re-reads. Advertises both hooks and
    records their calls per cache key.
    """

    impl_id = "vcfops-rest-retry-establish"

    def __init__(self) -> None:
        super().__init__()
        self.attempts: dict[tuple[str, str], int] = {}
        self.session_invalidations: list[tuple[str, str]] = []
        self.cred_evictions: list[tuple[str, str]] = []

    def _key(self, target: Any) -> tuple[str, str]:
        return (str(getattr(target, "tenant_id", "")), str(getattr(target, "id", "")))

    def _next(self, target: Any) -> dict[str, Any]:
        key = self._key(target)
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if self.attempts[key] == 1:
            # First dispatch: a stale session token yields an upstream 401.
            raise _make_http_status_error(
                headers=self.response_headers,
                json_body=self.response_body,
                status_code=401,
            )
        # Re-dispatch after invalidate_session: the cold re-establish re-reads
        # the still-stale credential and the login POST is itself rejected.
        http_exc = _make_http_status_error(
            headers=self.response_headers,
            json_body={"message": "Cannot complete login: incorrect user name or password"},
            status_code=401,
        )
        message = f"sddc session establish failed for target {getattr(target, 'name', None)!r}"
        raise (
            session_establish_auth_error(http_exc, message=message, target=target)
            or RuntimeError(message)
        ) from http_exc

    async def _request_json(  # type: ignore[override]
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
        return self._next(target)

    async def _post_json(  # type: ignore[override]
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
        return self._next(target)

    async def invalidate_session(self, target: Any) -> None:
        self.session_invalidations.append(self._key(target))

    async def invalidate_credentials(self, target: Any) -> None:
        self.cred_evictions.append(self._key(target))


@pytest.mark.asyncio
async def test_dispatch_establish_auth_failure_evicts_cached_credentials(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """#2396: the first-establish ``ConnectorAuthError`` arm calls ``invalidate_credentials``.

    A connector that caches credentials before a rejected login advertises the
    duck-typed hook; the dispatcher evicts on the establish-auth failure so the
    operator's next-dispatch restage re-reads the store with no restart. The
    result is still the structured ``connector_auth_failed`` envelope.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-establish-evict",
        cls=_EstablishAuthEvictConnector,
    )
    await _seed(
        session=session,
        impl_id="vcfops-rest-establish-evict",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    target = _FakeTarget(name="sddc-prod", host="sddc.lab.internal")
    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-establish-evict-9",
        op_id="GET:/api/v2/events",
        target=target,
        params={},
    )

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_auth_failed"
    assert result.extras["cause"] == "session_establish_401"

    connector = get_or_create_connector_instance(_EstablishAuthEvictConnector)
    assert isinstance(connector, _EstablishAuthEvictConnector)
    # Exactly one eviction for this cache key -- the establish arm fired the hook.
    assert connector.evictions == [("", str(target.id))]


@pytest.mark.asyncio
async def test_dispatch_establish_auth_failure_without_hook_is_harmless(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A connector advertising no ``invalidate_credentials`` hook still resolves cleanly.

    The eviction call is ``getattr``-guarded, so a stateless connector (no
    credential cache, no hook) surfaces the same ``connector_auth_failed``
    envelope with no error -- absence is harmless (the nsx / vmware_rest case).
    """
    assert not hasattr(_EstablishAuthConnector, "invalidate_credentials")
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-establish",
        cls=_EstablishAuthConnector,
    )
    await _seed(
        session=session,
        impl_id="vcfops-rest-establish",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-establish-9",
        op_id="GET:/api/v2/events",
        target=_FakeTarget(name="vc-prod", host="vc-prod.lab.internal"),
        params={},
    )

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_auth_failed"


@pytest.mark.asyncio
async def test_dispatch_post_invalidation_establish_auth_failure_evicts_credentials(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """#2396: the post-invalidation ``ConnectorAuthError`` arm also evicts credentials.

    The #2067 mid-session recovery evicts the session token and re-dispatches;
    when that cold-cache re-establish is itself rejected with a
    ``ConnectorAuthError``, the credential is stale. The dispatcher must evict
    it (in addition to the token ``invalidate_session`` already dropped) so the
    next dispatch after a restage re-reads. The result stays
    ``connector_auth_failed``.
    """
    register_connector_v2(
        product="vcfops",
        version="9",
        impl_id="vcfops-rest-retry-establish",
        cls=_RetryThenEstablishAuthConnector,
    )
    await _seed(
        session=session,
        impl_id="vcfops-rest-retry-establish",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    target = _FakeTarget(name="vc-prod", host="vc-prod.lab.internal")
    result = await dispatch(
        operator=_make_operator(),
        connector_id="vcfops-rest-retry-establish-9",
        op_id="GET:/api/v2/events",
        target=target,
        params={},
    )

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_auth_failed"

    connector = get_or_create_connector_instance(_RetryThenEstablishAuthConnector)
    assert isinstance(connector, _RetryThenEstablishAuthConnector)
    key = ("", str(target.id))
    # Initial 401 + one re-dispatch that establish-auth-fails.
    assert connector.attempts[key] == 2
    # The #2067 token eviction fired, and the #2396 credential eviction fired.
    assert connector.session_invalidations == [key]
    assert connector.cred_evictions == [key]
