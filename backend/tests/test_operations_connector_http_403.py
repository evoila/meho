# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``connector_http_403`` / ``connector_http_422`` errors.

G0.24-T4 (#1649) acceptance criteria:

* A dispatch raising :exc:`httpx.HTTPStatusError` with ``status_code ==
  403`` returns a structured ``connector_http_403``
  :class:`OperationResult` -- NOT the bare ``connector_error:
  HTTPStatusError`` that buried GitHub's actionable 403 (body message +
  ``X-Accepted-GitHub-Permissions`` / ``x-oauth-scopes`` headers) in
  ``extras["exception_message"]`` (consumer
  ``claude-rdc-hetzner-dc#1138``). The operator-facing ``error`` names
  the likely insufficient-permission cause; ``extras`` carries
  ``http_status=403``, the upstream ``upstream_message``, and the
  permission headers **when present**.
* A dispatch raising :exc:`httpx.HTTPStatusError` with ``status_code ==
  422`` returns a structured ``connector_http_422`` result: the
  operator-facing ``error`` names the invalid-payload cause; ``extras``
  carries ``http_status=422``, the upstream ``upstream_message``, and the
  GitHub-style ``validation_errors`` (the body's ``errors[]``) **when
  present**.
* A non-403/422 :exc:`httpx.HTTPStatusError` (e.g. 500) is unchanged --
  it falls through to the existing generic ``connector_error`` flatten.
* A successful dispatch is unaffected.
* #1627's :exc:`NotImplementedError` -> ``connector_unsupported`` arm is
  not regressed.

The cause is kept **connector-agnostic** -- any upstream 403/422, GitHub
or not, yields the structured cause; the GitHub permission headers
(403) / ``errors[]`` array (422) are echoed only when the upstream sent
them. The builder-shape tests mirror the #1627
``test_operations_connector_unsupported`` discipline
(``docs/codebase/error-message-shape.md``): stable code, diagnostic
human message with a remediation imperative + doc reference, structured
``extras`` payload.
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
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._errors import (
    result_connector_http_403,
    result_connector_http_422,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Settings / isolation fixtures (the sibling dispatcher-test pattern)
# ---------------------------------------------------------------------------

_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000004f3")


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


def _make_operator(*, sub: str = "op-conn-http-403") -> Operator:
    """Construct an :class:`Operator` directly -- no JWT round-trip."""
    return Operator(
        sub=sub,
        name="Connector-HTTP-403 Test Operator",
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
        product: str = "gh",
        version: str | None = "3",
        name: str = "gh-target",
        host: str = "api.github.com",
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
    status_code: int = 403,
) -> httpx.HTTPStatusError:
    """Build a real :exc:`httpx.HTTPStatusError` off a synthetic response.

    The error is produced the same way the production path produces it
    -- ``response.raise_for_status()`` on a non-2xx -- so the test
    exercises the genuine httpx exception shape (``exc.response`` access,
    case-insensitive headers) rather than a hand-faked stand-in. Shared
    by the 403 and 422 cases via the ``status_code`` argument.
    """
    request = httpx.Request("POST", "https://api.github.com/repos/o/r/issues")
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


class _Http403Connector(HttpConnector):
    """Connector whose write transport raises an upstream 403.

    Overrides ``_post_json`` / ``_request_json`` to raise the same
    :exc:`httpx.HTTPStatusError` the real :class:`HttpConnector` raises
    when ``resp.raise_for_status()`` sees a 403 -- the gh-rest write
    under an App with ``issues: read`` but not ``issues: write``. The
    transport is short-circuited so the test needs no live HTTP / Vault.
    """

    product = "gh"
    version = "3"
    impl_id = "gh-rest"
    supported_version_range = ">=3,<4"
    priority = 1

    #: Echoed verbatim by the structured error when present.
    response_headers: ClassVar[dict[str, str]] = {
        "X-Accepted-GitHub-Permissions": "issues=write",
        "x-oauth-scopes": "repo, read:org",
        "Content-Type": "application/json",
    }
    response_body: ClassVar[dict[str, Any]] = {
        "message": "Resource not accessible by integration",
        "documentation_url": "https://docs.github.com/rest/issues/issues#create-an-issue",
    }
    status_code: ClassVar[int] = 403

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise _make_http_status_error(
            headers=self.response_headers,
            json_body=self.response_body,
            status_code=self.status_code,
        )

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
        # Ingested dispatch routes through _post_json / _request_json, not
        # execute; this concrete impl only makes the abstract base
        # instantiable.
        raise NotImplementedError


class _Http422Connector(_Http403Connector):
    """Same shape as :class:`_Http403Connector` but raises a 422.

    The gh-rest write whose request body the upstream rejected as invalid
    (the requestBody-mangling bug T5 #1656). GitHub returns a
    ``Validation Failed`` message + an ``errors[]`` array naming the
    offending fields; the structured ``connector_http_422`` echoes both.
    """

    impl_id = "gh-rest-422"
    status_code: ClassVar[int] = 422
    #: No permission headers on a 422 -- a validation failure, not a scope one.
    response_headers: ClassVar[dict[str, str]] = {"Content-Type": "application/json"}
    response_body: ClassVar[dict[str, Any]] = {
        "message": "Validation Failed",
        "errors": [
            {"resource": "Issue", "code": "missing_field", "field": "title"},
            {
                "resource": "Issue",
                "code": "custom",
                "field": "labels",
                "message": "labels must be an array of strings",
            },
        ],
        "documentation_url": "https://docs.github.com/rest/issues/issues#create-an-issue",
    }


class _Http500Connector(_Http403Connector):
    """Same shape as :class:`_Http403Connector` but raises a 500.

    Pins the scope boundary: a non-403/422 ``HTTPStatusError`` falls
    through the dispatcher's ``connector_http_403`` / ``connector_http_422``
    branches to the generic ``connector_error`` flatten unchanged.
    """

    impl_id = "gh-rest-500"
    status_code: ClassVar[int] = 500
    response_body: ClassVar[dict[str, Any]] = {"message": "Server Error"}


class _OkConnector(HttpConnector):
    """Connector whose write transport succeeds -- the regression baseline."""

    product = "ok"
    version = "1"
    impl_id = "ok-rest"
    supported_version_range = ">=1,<2"
    priority = 1

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"number": 7, "state": "open"}

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


class _NotImplementedConnector(HttpConnector):
    """Connector raising :exc:`NotImplementedError` -- the #1627 regression.

    The 403 arm must not capture (and the new ``import httpx`` /
    re-ordered catches must not perturb) #1627's
    ``connector_unsupported`` classification.
    """

    product = "nie"
    version = "1"
    impl_id = "nie-rest"
    supported_version_range = ">=1,<2"
    priority = 1

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError(
            f"_NotImplementedConnector does not implement write dispatch for target {target.name!r}"
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
        raise NotImplementedError


async def _insert_ingested_descriptor(
    *,
    session: AsyncSession,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
    embedding: list[float],
    method: str = "POST",
    path: str = "/repos/o/r/issues",
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
        summary="Create issue.",
        description="Ingested write test op.",
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


def test_result_connector_http_403_shape_with_github_headers() -> None:
    """GitHub 403: code, http_status, upstream message, echoed headers, remediation."""
    exc = _make_http_status_error(
        headers={
            "X-Accepted-GitHub-Permissions": "issues=write",
            "x-oauth-scopes": "repo, read:org",
        },
        json_body={"message": "Resource not accessible by integration"},
    )
    out = result_connector_http_403("POST:/repos/o/r/issues", exc, duration_ms=1.0)

    assert out.status == "error"
    assert out.op_id == "POST:/repos/o/r/issues"
    assert out.error is not None
    assert out.error.startswith("connector_http_403:")
    # The cause is named connector-agnostically as a permission problem.
    assert "may lack the permission" in out.error
    assert "403" in out.error
    # Remediation imperative + doc reference.
    assert "Grant the missing permission" in out.error
    assert "docs/codebase/error-message-shape.md" in out.error
    # The upstream body message tails the operator-facing string.
    assert "Resource not accessible by integration" in out.error
    # Structured, machine-usable extras.
    assert out.extras["error_code"] == "connector_http_403"
    assert out.extras["http_status"] == 403
    assert out.extras["upstream_message"] == "Resource not accessible by integration"
    assert out.extras["permission_headers"] == {
        "X-Accepted-GitHub-Permissions": "issues=write",
        "x-oauth-scopes": "repo, read:org",
    }


def test_result_connector_http_403_headers_matched_case_insensitively() -> None:
    """Lower-cased upstream header keys still echo under the canonical names."""
    exc = _make_http_status_error(
        headers={
            "x-accepted-github-permissions": "issues=write,metadata=read",
            "X-OAUTH-SCOPES": "repo",
        },
        json_body={"message": "Resource not accessible by integration"},
    )
    out = result_connector_http_403("POST:/x", exc, duration_ms=1.0)
    assert out.extras["permission_headers"] == {
        "X-Accepted-GitHub-Permissions": "issues=write,metadata=read",
        "x-oauth-scopes": "repo",
    }


def test_result_connector_http_403_non_github_403_has_empty_headers() -> None:
    """A non-GitHub 403 (no permission headers) still yields the structured cause."""
    exc = _make_http_status_error(
        headers={"Content-Type": "application/json"},
        json_body={"message": "Forbidden"},
    )
    out = result_connector_http_403("POST:/x", exc, duration_ms=1.0)
    assert out.status == "error"
    assert out.error is not None
    assert out.error.startswith("connector_http_403:")
    assert out.extras["http_status"] == 403
    assert out.extras["upstream_message"] == "Forbidden"
    # Echoed, never required -- empty when the upstream sent none.
    assert out.extras["permission_headers"] == {}


def test_result_connector_http_403_non_json_body_falls_back_to_text() -> None:
    """A non-JSON 403 body is surfaced verbatim (capped) as the upstream message."""
    exc = _make_http_status_error(text_body="Forbidden by WAF rule 42")
    out = result_connector_http_403("POST:/x", exc, duration_ms=1.0)
    assert out.extras["upstream_message"] == "Forbidden by WAF rule 42"
    assert out.error is not None
    assert "Forbidden by WAF rule 42" in out.error


def test_result_connector_http_403_empty_body_yields_null_message() -> None:
    """An empty 403 body yields ``upstream_message=None`` (not an empty string)."""
    exc = _make_http_status_error()
    out = result_connector_http_403("POST:/x", exc, duration_ms=1.0)
    assert out.extras["upstream_message"] is None
    # The operator-facing string still names the permission cause.
    assert out.error is not None
    assert "may lack the permission" in out.error
    # No dangling "Upstream said:" tail when there was no message.
    assert "Upstream said:" not in out.error


def test_result_connector_http_403_caps_oversized_message() -> None:
    """A pathological upstream message is capped like the sibling builders."""
    exc = _make_http_status_error(json_body={"message": "x" * 400})
    out = result_connector_http_403("POST:/x", exc, duration_ms=0.5)
    message = out.extras["upstream_message"]
    assert isinstance(message, str)
    assert message.endswith("...<truncated>")
    assert len(message) == 256 + len("...<truncated>")


def test_result_connector_http_422_shape_with_errors_array() -> None:
    """GitHub 422: code, http_status, upstream message, echoed errors[], remediation."""
    exc = _make_http_status_error(
        status_code=422,
        json_body={
            "message": "Validation Failed",
            "errors": [
                {"resource": "Issue", "code": "missing_field", "field": "title"},
                {"resource": "Issue", "code": "custom", "field": "labels"},
            ],
        },
    )
    out = result_connector_http_422("POST:/repos/o/r/issues", exc, duration_ms=1.0)

    assert out.status == "error"
    assert out.op_id == "POST:/repos/o/r/issues"
    assert out.error is not None
    assert out.error.startswith("connector_http_422:")
    # The cause is named connector-agnostically as a payload-validation problem.
    assert "rejected the request payload as invalid" in out.error
    assert "422" in out.error
    # Remediation imperative + doc reference.
    assert "extras.validation_errors" in out.error
    assert "docs/codebase/error-message-shape.md" in out.error
    # The upstream body message tails the operator-facing string.
    assert "Validation Failed" in out.error
    # Structured, machine-usable extras.
    assert out.extras["error_code"] == "connector_http_422"
    assert out.extras["http_status"] == 422
    assert out.extras["upstream_message"] == "Validation Failed"
    assert out.extras["validation_errors"] == [
        {"resource": "Issue", "code": "missing_field", "field": "title"},
        {"resource": "Issue", "code": "custom", "field": "labels"},
    ]


def test_result_connector_http_422_non_github_422_has_empty_errors() -> None:
    """A non-GitHub 422 (no errors[]) still yields the structured cause."""
    exc = _make_http_status_error(
        status_code=422,
        json_body={"message": "Unprocessable Entity"},
    )
    out = result_connector_http_422("POST:/x", exc, duration_ms=1.0)
    assert out.status == "error"
    assert out.error is not None
    assert out.error.startswith("connector_http_422:")
    assert out.extras["http_status"] == 422
    assert out.extras["upstream_message"] == "Unprocessable Entity"
    # Echoed, never required -- empty when the upstream sent no errors[].
    assert out.extras["validation_errors"] == []


def test_result_connector_http_422_non_json_body_empty_errors_text_message() -> None:
    """A non-JSON 422 body: empty errors[], raw text surfaced as the message."""
    exc = _make_http_status_error(status_code=422, text_body="rejected by gateway")
    out = result_connector_http_422("POST:/x", exc, duration_ms=1.0)
    assert out.extras["validation_errors"] == []
    assert out.extras["upstream_message"] == "rejected by gateway"
    assert out.error is not None
    assert "rejected by gateway" in out.error


def test_result_connector_http_422_non_list_errors_field_ignored() -> None:
    """An ``errors`` field that is not a list is not echoed (no fabricated shape)."""
    exc = _make_http_status_error(
        status_code=422,
        json_body={"message": "Validation Failed", "errors": "not-a-list"},
    )
    out = result_connector_http_422("POST:/x", exc, duration_ms=1.0)
    assert out.extras["validation_errors"] == []
    assert out.extras["upstream_message"] == "Validation Failed"


# ---------------------------------------------------------------------------
# Dispatcher conversion (the #1649 acceptance-criterion unit tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_converts_403_to_connector_http_403(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """403 write dispatch -> structured ``connector_http_403`` + audit row + event.

    The dispatcher catches the connector's :exc:`httpx.HTTPStatusError`
    (403) ahead of the generic ``except Exception`` and emits the
    structured shape -- not the pre-#1649 bare ``connector_error:
    HTTPStatusError`` that buried the GitHub body + headers in
    ``extras["exception_message"]``.
    """
    register_connector_v2(
        product="gh",
        version="3",
        impl_id="gh-rest",
        cls=_Http403Connector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="gh",
        version="3",
        impl_id="gh-rest",
        op_id="POST:/repos/o/r/issues",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-3",
        op_id="POST:/repos/o/r/issues",
        target=_FakeTarget(name="gh-prod"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_http_403:")
    assert result.extras["error_code"] == "connector_http_403"
    assert result.extras["http_status"] == 403
    assert result.extras["upstream_message"] == "Resource not accessible by integration"
    assert result.extras["permission_headers"] == {
        "X-Accepted-GitHub-Permissions": "issues=write",
        "x-oauth-scopes": "repo, read:org",
    }
    # NOT the pre-#1649 flattened shape.
    assert "connector_error" not in result.error
    assert result.extras["error_code"] != "connector_error"
    assert "exception_message" not in result.extras

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "POST:/repos/o/r/issues")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


@pytest.mark.asyncio
async def test_dispatch_converts_422_to_connector_http_422(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """422 write dispatch -> structured ``connector_http_422`` + audit row + event.

    The dispatcher catches the connector's :exc:`httpx.HTTPStatusError`
    (422) ahead of the generic ``except Exception`` and emits the
    structured shape -- not the pre-#1649 bare ``connector_error:
    HTTPStatusError`` that buried GitHub's ``Validation Failed`` body +
    ``errors[]`` array in ``extras["exception_message"]`` (the shape that
    slowed the diagnosis of the requestBody-mangling bug T5 #1656).
    """
    register_connector_v2(
        product="gh",
        version="3",
        impl_id="gh-rest-422",
        cls=_Http422Connector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="gh",
        version="3",
        impl_id="gh-rest-422",
        op_id="POST:/repos/o/r/issues",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-422-3",
        op_id="POST:/repos/o/r/issues",
        target=_FakeTarget(name="gh-prod"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_http_422:")
    assert result.extras["error_code"] == "connector_http_422"
    assert result.extras["http_status"] == 422
    assert result.extras["upstream_message"] == "Validation Failed"
    assert result.extras["validation_errors"] == [
        {"resource": "Issue", "code": "missing_field", "field": "title"},
        {
            "resource": "Issue",
            "code": "custom",
            "field": "labels",
            "message": "labels must be an array of strings",
        },
    ]
    # NOT the pre-#1649 flattened shape, and not the 403 sibling.
    assert "connector_error" not in result.error
    assert result.extras["error_code"] != "connector_error"
    assert "exception_message" not in result.extras
    assert "connector_http_403" not in result.error
    assert "permission_headers" not in result.extras

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "POST:/repos/o/r/issues")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


@pytest.mark.asyncio
async def test_dispatch_non_403_or_422_status_error_falls_through_to_connector_error(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A 500 ``HTTPStatusError`` is unchanged -- generic ``connector_error`` flatten.

    Scope boundary (#1649 AC): only 403 / 422 are siphoned into the
    structured ``connector_http_*`` shapes; every other status falls
    through those branches into the existing generic catch.
    """
    register_connector_v2(
        product="gh",
        version="3",
        impl_id="gh-rest-500",
        cls=_Http500Connector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="gh",
        version="3",
        impl_id="gh-rest-500",
        op_id="POST:/repos/o/r/issues",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-500-3",
        op_id="POST:/repos/o/r/issues",
        target=_FakeTarget(name="gh-prod"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "HTTPStatusError"
    # Did NOT get reclassified as the 403 / 422 shapes.
    assert "connector_http_403" not in (result.error or "")
    assert "connector_http_422" not in (result.error or "")
    assert "http_status" not in result.extras

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


@pytest.mark.asyncio
async def test_dispatch_successful_write_is_unaffected(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A successful write dispatch returns ``ok`` -- the 403 arm is inert (#1649 AC)."""
    register_connector_v2(
        product="ok",
        version="1",
        impl_id="ok-rest",
        cls=_OkConnector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="ok",
        version="1",
        impl_id="ok-rest",
        op_id="POST:/repos/o/r/issues",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="ok-rest-1",
        op_id="POST:/repos/o/r/issues",
        target=_FakeTarget(product="ok", version="1", name="ok-prod"),
        params={},
    )

    assert result.status == "ok"
    assert result.error is None
    assert result.result == {"number": 7, "state": "open"}

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "ok"


@pytest.mark.asyncio
async def test_dispatch_notimplemented_still_maps_to_connector_unsupported(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """#1627 regression: NotImplementedError still -> ``connector_unsupported``.

    The new ``except httpx.HTTPStatusError`` arm sits between the
    ``NotImplementedError`` arm and the generic catch;
    ``HTTPStatusError`` and ``NotImplementedError`` are disjoint, so the
    #1627 classification must be untouched.
    """
    register_connector_v2(
        product="nie",
        version="1",
        impl_id="nie-rest",
        cls=_NotImplementedConnector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="nie",
        version="1",
        impl_id="nie-rest",
        op_id="POST:/repos/o/r/issues",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="nie-rest-1",
        op_id="POST:/repos/o/r/issues",
        target=_FakeTarget(product="nie", version="1", name="nie-prod"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_unsupported:")
    assert result.extras["error_code"] == "connector_unsupported"
    assert result.extras["cause"] == "unsupported_feature"
    assert result.extras["connector_class"] == "_NotImplementedConnector"
    # Not reclassified as connector_http_403.
    assert "connector_http_403" not in result.error
    assert "http_status" not in result.extras


# ---------------------------------------------------------------------------
# REST / MCP parity: the shared call_operation funnel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_operation_envelope_carries_connector_http_403(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """The serialized envelope both transports return carries the 403 shape.

    ``POST /api/v1/operations/call`` returns ``await call_operation(...)``
    verbatim and the MCP ``call_operation`` tool is a thin shim over the
    same function, so asserting on the serialized dict envelope here
    covers both surfaces -- the same parity argument the
    ``connector_unsupported`` / ``composite_l2_*`` envelopes rely on.
    """
    register_connector_v2(
        product="gh",
        version="3",
        impl_id="gh-rest",
        cls=_Http403Connector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="gh",
        version="3",
        impl_id="gh-rest",
        op_id="POST:/repos/o/r/issues",
        embedding=stub_embedding_service.encode_one.return_value,
    )
    # Seed a target row resolve_target() can find by name.
    target_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s, s.begin():
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT,
                name="gh-prod",
                aliases=[],
                product="gh",
                version="3",
                host="api.github.com",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    envelope = await call_operation(
        _make_operator(),
        {
            "connector_id": "gh-rest-3",
            "op_id": "POST:/repos/o/r/issues",
            "target": {"name": "gh-prod"},
            "params": {},
        },
    )

    assert envelope["status"] == "error"
    assert envelope["error"].startswith("connector_http_403:")
    assert envelope["extras"]["error_code"] == "connector_http_403"
    assert envelope["extras"]["http_status"] == 403
    assert envelope["extras"]["upstream_message"] == "Resource not accessible by integration"
    assert envelope["extras"]["permission_headers"] == {
        "X-Accepted-GitHub-Permissions": "issues=write",
        "x-oauth-scopes": "repo, read:org",
    }
