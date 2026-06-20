# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the read-only dispatch request preview (#1683).

G0.24 follow-up (#1683) -- the observability counterpart to T5 #1656
(requestBody unwrap) and T4 #1649 (structured 4xx error shape). When an
ingested-L2 **write** dispatch fails upstream, an operator could not read
back what meho put on the wire: the audit row persists only a hashed
``params_hash``. This surface returns the literal would-be HTTP request
(``{method, resolved_path, query, redacted_body}``) WITHOUT dispatching.

Acceptance criteria covered:

* **AC1** -- a read-only preview path resolves an op + params to the
  literal request (method + substituted path + query + redacted body) and
  returns it WITHOUT dispatching. A gh-rest issue-create preview shows
  ``POST`` + ``/repos/{owner}/{repo}/issues`` + body ``{"title": "…"}``.
  Proven the connector's HTTP transport is never called.
* **AC2** -- redaction reuses the existing connector-boundary pipeline: a
  body field the redactor masks (a labelled credential under the
  default-safe policy; a UUID under a registered override) is masked in
  the preview. No new raw-secret surface.
* **AC3** -- the persisted audit row is unchanged: a preview writes **no**
  ``AuditLog`` row and publishes **no** broadcast event (regression).
* **AC4** -- the error-echo option is **deferred** (see the module-level
  note in :mod:`meho_backplane.operations._request_preview` and the PR
  body): the dedicated read-only preview is the diagnosis surface; the
  4xx error's ``extras`` is not extended in this change.

Plus scope boundaries: a ``typed`` / ``composite`` op (no single literal
HTTP request) returns ``status="unavailable"``; ``invalid_params`` /
``unknown_op`` come back as structured envelopes; the previewed request
matches what ``dispatch_ingested`` actually sends (shared-resolver
regression); REST + MCP parity over the shared meta-tool funnel.

Mirrors the sibling #1649 ``test_operations_connector_http_403`` fixtures
(in-memory SQLite via the autouse-migrated engine, connector registry +
dispatcher-cache reset, deterministic embedding stub).
"""

from __future__ import annotations

import textwrap
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.meta_tools import preview_operation
from meho_backplane.redaction import clear_overrides, parse_policy, register_policy
from meho_backplane.settings import get_settings

_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000016b3")


# ---------------------------------------------------------------------------
# Fixtures (the sibling dispatcher-test pattern)
# ---------------------------------------------------------------------------


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
    """Reset dispatcher caches + connector registry + redaction overrides."""
    reset_dispatcher_caches()
    clear_registry()
    clear_overrides()
    yield
    reset_dispatcher_caches()
    clear_registry()
    clear_overrides()


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
    """Replace :func:`publish_event` with a recording stub.

    Lets each test assert the preview path publishes **zero** broadcast
    events (it never dispatches), the broadcast-side half of AC3.
    """
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


def _make_operator(*, sub: str = "op-preview") -> Operator:
    """Construct an :class:`Operator` directly -- no JWT round-trip."""
    return Operator(
        sub=sub,
        name="Request-Preview Test Operator",
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
        self.fqdn: str | None = None


class _RecordingHttpConnector(HttpConnector):
    """Connector whose write/read transport records calls instead of sending.

    The preview path must resolve the request WITHOUT touching the HTTP
    transport. These ``_post_json`` / ``_request_json`` overrides append to
    :attr:`calls` and would return a sentinel if ever reached -- a non-empty
    ``calls`` list after a preview is the AC1 failure signal (the preview
    dispatched).
    """

    product = "gh"
    version = "3"
    impl_id = "gh-rest"
    supported_version_range = ">=3,<4"
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append({"verb": "POST", "path": path, "json": json})
        return {"sent": True}

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
        self.calls.append({"verb": method, "path": path, "params": params, "json": json})
        return {"sent": True}

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


def _register_recording_gh_connector() -> _RecordingHttpConnector:
    """Register :class:`_RecordingHttpConnector` under ``gh-rest-3`` + return the singleton.

    The v2 registry stores the **class**; the dispatcher / preview resolve
    the **cached instance** via :func:`get_or_create_connector_instance`.
    Returning that same singleton lets a test assert on its ``calls`` list
    -- a non-empty ``calls`` after a preview is the AC1 "it dispatched"
    failure signal.
    """
    register_connector_v2(
        product="gh",
        version="3",
        impl_id="gh-rest",
        cls=_RecordingHttpConnector,
    )
    connector = get_or_create_connector_instance(_RecordingHttpConnector)
    assert isinstance(connector, _RecordingHttpConnector)
    return connector


async def _insert_ingested_descriptor(
    *,
    session: AsyncSession,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
    embedding: list[float],
    method: str = "POST",
    path: str = "/repos/{owner}/{repo}/issues",
    parameter_schema: dict[str, Any] | None = None,
) -> None:
    """Seed one enabled ``source_kind='ingested'`` descriptor row.

    Default ``parameter_schema`` models a gh-rest issue-create: ``owner`` /
    ``repo`` as ``x-meho-param-loc: path`` and ``body`` as
    ``x-meho-param-loc: body`` (the single requestBody container param
    #1656 unwraps), so the preview substitutes the path and unwraps the
    body exactly as the real dispatch would.
    """
    if parameter_schema is None:
        parameter_schema = {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "x-meho-param-loc": "path"},
                "repo": {"type": "string", "x-meho-param-loc": "path"},
                "body": {"type": "object", "x-meho-param-loc": "body"},
            },
        }
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
        parameter_schema=parameter_schema,
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


async def _assert_no_audit_rows(path: str) -> None:
    """AC3 regression: the preview wrote no ``AuditLog`` row for *path*."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (await fresh.execute(select(AuditLog).where(AuditLog.path == path))).scalars().all()
    assert rows == [], f"preview must not persist an audit row; found {len(rows)}"


# ---------------------------------------------------------------------------
# AC1 -- the literal request, without dispatching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_gh_issue_create_returns_literal_request_without_dispatching(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """AC1: gh-rest issue-create preview -> POST + substituted path + unwrapped body.

    The keystone acceptance criterion: a preview of the gh-rest
    issue-create op resolves to ``POST`` + ``/repos/{owner}/{repo}/issues``
    (placeholders substituted) + body ``{"title": "…"}`` (the requestBody
    container unwrapped, #1656) and returns it WITHOUT calling the
    connector's HTTP transport.
    """
    connector = _register_recording_gh_connector()
    await _insert_ingested_descriptor(
        session=session,
        product="gh",
        version="3",
        impl_id="gh-rest",
        op_id="POST:/repos/{owner}/{repo}/issues",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    envelope = await preview_dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-3",
        op_id="POST:/repos/{owner}/{repo}/issues",
        target=_FakeTarget(name="gh-prod"),
        params={
            "owner": "evoila",
            "repo": "meho",
            "body": {"title": "diagnose me"},
        },
    )

    assert envelope["status"] == "ok"
    assert envelope["op_id"] == "POST:/repos/{owner}/{repo}/issues"
    assert envelope["connector_id"] == "gh-rest-3"
    assert envelope["source_kind"] == "ingested"
    assert envelope["method"] == "POST"
    # Placeholders substituted to the literal path.
    assert envelope["resolved_path"] == "/repos/evoila/meho/issues"
    # requestBody container unwrapped (#1656) -- NOT {"body": {"title": ...}}.
    assert envelope["redacted_body"] == {"title": "diagnose me"}
    assert envelope["query"] is None

    # AC1: nothing was dispatched -- the HTTP transport was never reached.
    assert connector.calls == []
    # AC3: no broadcast event, no audit row.
    assert captured_events == []
    await _assert_no_audit_rows("POST:/repos/{owner}/{repo}/issues")


@pytest.mark.asyncio
async def test_preview_get_op_splits_query_and_path(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A GET op previews its substituted path + query bucket, no body.

    Pins the read-verb branch: ``x-meho-param-loc: query`` params land in
    ``query`` (the httpx ``params=`` value), a path placeholder is
    substituted, and there is no body. Still no dispatch.
    """
    connector = _register_recording_gh_connector()
    await _insert_ingested_descriptor(
        session=session,
        product="gh",
        version="3",
        impl_id="gh-rest",
        op_id="GET:/repos/{owner}/{repo}/issues",
        embedding=stub_embedding_service.encode_one.return_value,
        method="GET",
        path="/repos/{owner}/{repo}/issues",
        parameter_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "x-meho-param-loc": "path"},
                "repo": {"type": "string", "x-meho-param-loc": "path"},
                "state": {"type": "string", "x-meho-param-loc": "query"},
            },
        },
    )

    envelope = await preview_dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-3",
        op_id="GET:/repos/{owner}/{repo}/issues",
        target=_FakeTarget(name="gh-prod"),
        params={"owner": "evoila", "repo": "meho", "state": "open"},
    )

    assert envelope["status"] == "ok"
    assert envelope["method"] == "GET"
    assert envelope["resolved_path"] == "/repos/evoila/meho/issues"
    assert envelope["query"] == {"state": "open"}
    assert envelope["redacted_body"] is None
    assert connector.calls == []


@pytest.mark.asyncio
async def test_preview_matches_what_dispatch_ingested_sends(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Shared-resolver regression: the previewed request == the sent request.

    Both :func:`dispatch_ingested` and the preview resolve the literal
    request through :func:`resolve_ingested_request`, so the previewed
    method/path/body must equal what a real dispatch puts on the wire. This
    guards against drift -- the whole point of factoring the resolver.
    """
    from meho_backplane.operations.dispatcher import dispatch

    connector = _register_recording_gh_connector()
    await _insert_ingested_descriptor(
        session=session,
        product="gh",
        version="3",
        impl_id="gh-rest",
        op_id="POST:/repos/{owner}/{repo}/issues",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    args: dict[str, Any] = {
        "owner": "evoila",
        "repo": "meho",
        "body": {"title": "parity"},
    }
    preview = await preview_dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-3",
        op_id="POST:/repos/{owner}/{repo}/issues",
        target=_FakeTarget(name="gh-prod"),
        params=args,
    )
    assert connector.calls == []  # preview did not send

    # Now actually dispatch the same args and compare the recorded wire call.
    await dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-3",
        op_id="POST:/repos/{owner}/{repo}/issues",
        target=_FakeTarget(name="gh-prod"),
        params=args,
    )
    assert len(connector.calls) == 1
    sent = connector.calls[0]
    assert sent["verb"] == "POST"
    assert sent["path"] == preview["resolved_path"]
    # The body the connector received equals the previewed (pre-redaction)
    # body -- here nothing is masked, so redacted_body == the sent body.
    assert sent["json"] == preview["redacted_body"] == {"title": "parity"}


# ---------------------------------------------------------------------------
# AC2 -- redaction reuses the existing connector-boundary pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_masks_credential_body_field_default_policy(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """AC2: a credential-shaped body value is masked by the default-safe policy.

    No override registered -> the conservative default policy fires. The
    Tier-1 engine matches on the **string value shape** (not the dict key
    name), so a bearer token carried in a request-body value is redacted in
    the preview exactly as it would be at the connector response boundary --
    proving the preview is not a new raw-secret surface and reuses the same
    named-pattern library.
    """
    connector = _register_recording_gh_connector()
    await _insert_ingested_descriptor(
        session=session,
        product="gh",
        version="3",
        impl_id="gh-rest",
        op_id="POST:/repos/{owner}/{repo}/secrets",
        embedding=stub_embedding_service.encode_one.return_value,
        path="/repos/{owner}/{repo}/secrets",
    )

    secret = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.sig"
    envelope = await preview_dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-3",
        op_id="POST:/repos/{owner}/{repo}/secrets",
        target=_FakeTarget(name="gh-prod"),
        params={
            "owner": "evoila",
            "repo": "meho",
            "body": {"title": "visible", "upstream_auth": secret},
        },
    )

    assert envelope["status"] == "ok"
    body = envelope["redacted_body"]
    assert isinstance(body, dict)
    # The non-secret field is preserved verbatim...
    assert body["title"] == "visible"
    # ...the bearer token in the body value is redacted (default-safe policy).
    assert "[REDACTED:bearer_token]" in body["upstream_auth"]
    assert secret not in str(body)
    assert connector.calls == []


@pytest.mark.asyncio
async def test_preview_redaction_honours_registered_override(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """AC2: the preview resolves the per-(connector_id) policy override.

    A UUID-only override registered for ``gh-rest-3`` masks a UUID in the
    body while leaving a bearer token (no rule) untouched -- proving the
    preview routes through the same
    :func:`resolve_policy`-backed pipeline the dispatcher uses, not a
    hard-coded default.
    """
    uuid_only = parse_policy(
        textwrap.dedent(
            """
            id: preview-uuid-only-test
            version: 1
            rules:
              - name: redact-uuid
                pattern: uuid
                action: redact
                reason: "test override"
            """
        ).strip()
    )
    register_policy(uuid_only, connector_id="gh-rest-3")

    _register_recording_gh_connector()
    await _insert_ingested_descriptor(
        session=session,
        product="gh",
        version="3",
        impl_id="gh-rest",
        op_id="POST:/repos/{owner}/{repo}/issues",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    a_uuid = "123e4567-e89b-12d3-a456-426614174000"
    envelope = await preview_dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-3",
        op_id="POST:/repos/{owner}/{repo}/issues",
        target=_FakeTarget(name="gh-prod"),
        params={
            "owner": "evoila",
            "repo": "meho",
            "body": {"ref_id": a_uuid, "token": "Bearer eyJhbGciOi"},
        },
    )

    assert envelope["status"] == "ok"
    serialised = str(envelope["redacted_body"])
    # UUID redacted by the override...
    assert "[REDACTED:uuid]" in serialised
    assert a_uuid not in serialised
    # ...bearer survived (override has no bearer rule -> proves the default
    # was NOT applied; the override fired).
    assert "Bearer eyJhbGciOi" in serialised


# ---------------------------------------------------------------------------
# Scope boundaries / structured envelopes
# ---------------------------------------------------------------------------


async def _handler_returning_ok(
    *, operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """A trivial typed handler (never invoked by the preview path)."""
    return {"ok": True}


@pytest.mark.asyncio
async def test_preview_typed_op_is_unavailable(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """A typed op has no single literal HTTP request -> status=unavailable.

    Scope boundary: ``typed`` / ``composite`` ops run Python handlers
    (which may make zero or many HTTP calls). The preview says so
    explicitly rather than fabricating a request.
    """
    register_connector_v2(
        product="demo",
        version="",
        impl_id="",
        cls=_RecordingHttpConnector,
    )
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id="demo.typed.read",
        handler=_handler_returning_ok,
        summary="A typed op.",
        description="Typed.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    envelope = await preview_dispatch(
        operator=_make_operator(),
        connector_id="demo-1.x",
        op_id="demo.typed.read",
        target=_FakeTarget(product="demo", version="1.x", name="demo-prod"),
        params={},
    )

    assert envelope["status"] == "unavailable"
    assert envelope["source_kind"] == "typed"
    assert envelope["extras"]["error_code"] == "preview_unavailable"
    assert envelope["extras"]["reason"] == "not_ingested"
    assert "method" not in envelope
    assert "resolved_path" not in envelope


@pytest.mark.asyncio
async def test_preview_invalid_params_returns_structured_error(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Params failing parameter_schema -> structured invalid_params envelope.

    The preview validates params the same way ``dispatch`` does, so an
    agent that previews a bad shape gets ``extras.validation_errors`` to
    self-correct from -- without ever resolving a request.
    """
    connector = _register_recording_gh_connector()
    await _insert_ingested_descriptor(
        session=session,
        product="gh",
        version="3",
        impl_id="gh-rest",
        op_id="POST:/repos/{owner}/{repo}/issues",
        embedding=stub_embedding_service.encode_one.return_value,
        parameter_schema={
            "type": "object",
            "properties": {"owner": {"type": "string", "x-meho-param-loc": "path"}},
            "required": ["owner"],
            "additionalProperties": False,
        },
    )

    envelope = await preview_dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-3",
        op_id="POST:/repos/{owner}/{repo}/issues",
        target=_FakeTarget(name="gh-prod"),
        params={"unexpected": "field"},  # missing required owner + extra
    )

    assert envelope["status"] == "error"
    assert envelope["error"].startswith("invalid_params:")
    assert envelope["extras"]["error_code"] == "invalid_params"
    assert isinstance(envelope["extras"]["validation_errors"], list)
    assert envelope["extras"]["validation_errors"]
    assert connector.calls == []


@pytest.mark.asyncio
async def test_preview_unknown_op_returns_structured_error(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """An op id that resolves no descriptor -> structured unknown_op envelope."""
    register_connector_v2(
        product="gh",
        version="3",
        impl_id="gh-rest",
        cls=_RecordingHttpConnector,
    )
    envelope = await preview_dispatch(
        operator=_make_operator(),
        connector_id="gh-rest-3",
        op_id="POST:/does/not/exist",
        target=_FakeTarget(name="gh-prod"),
        params={},
    )
    assert envelope["status"] == "error"
    assert envelope["error"].startswith("unknown_op:")
    assert envelope["extras"]["error_code"] == "unknown_op"


# ---------------------------------------------------------------------------
# REST / MCP parity over the shared meta-tool funnel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_operation_meta_tool_resolves_target_by_name(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """The shared ``preview_operation`` funnel both surfaces call resolves the target.

    ``POST /api/v1/operations/preview`` returns ``await
    preview_operation(...)`` verbatim and the MCP ``preview_operation``
    tool is a thin shim over the same function, so asserting on the
    envelope here covers both surfaces -- the same parity argument the
    ``call_operation`` envelope relies on. Also re-confirms the
    no-dispatch / no-audit contract end-to-end through the funnel.
    """
    connector = _register_recording_gh_connector()
    await _insert_ingested_descriptor(
        session=session,
        product="gh",
        version="3",
        impl_id="gh-rest",
        op_id="POST:/repos/{owner}/{repo}/issues",
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

    envelope = await preview_operation(
        _make_operator(),
        {
            "connector_id": "gh-rest-3",
            "op_id": "POST:/repos/{owner}/{repo}/issues",
            "target": "gh-prod",  # bare-string shape
            "params": {
                "owner": "evoila",
                "repo": "meho",
                "body": {"title": "via-funnel"},
            },
        },
    )

    assert envelope["status"] == "ok"
    assert envelope["method"] == "POST"
    assert envelope["resolved_path"] == "/repos/evoila/meho/issues"
    assert envelope["redacted_body"] == {"title": "via-funnel"}
    assert connector.calls == []
    assert captured_events == []
    await _assert_no_audit_rows("POST:/repos/{owner}/{repo}/issues")


@pytest.mark.asyncio
async def test_preview_operation_missing_target_name_raises_valueerror() -> None:
    """A dict target without ``name`` raises ValueError (route maps to 400).

    Mirrors ``call_operation``'s sole ValueError surface so the
    ``/preview`` route's 400 mapping is the same contract as ``/call``.
    """
    with pytest.raises(ValueError, match="must include a 'name' field"):
        await preview_operation(
            _make_operator(),
            {
                "connector_id": "gh-rest-3",
                "op_id": "POST:/repos/{owner}/{repo}/issues",
                "target": {"fqdn": "no-name-here"},
                "params": {},
            },
        )
