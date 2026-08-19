# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Tempo read-only, multi-tenant connector (#2903).

Coverage matrix (per Task #2903 acceptance criteria):

* **Registration** — ``tempo`` resolves via ``register_connector_v2`` (versioned
  triple + wildcard), appears in ``all_connectors_v2()`` and
  ``registered_product_tokens()``.
* **Read-only gate** — :func:`assert_tempo_read_only` rejects non-GET and paths
  outside ``/api``, with no upstream call.
* **Wire-path pinning** — each curated op funnels through ``_tempo_get`` at its
  exact documented ``/api`` path; the fingerprint/probe literals are pinned
  from the live module constants (the always-on half of the #2980 reconcile
  standard, folded into the unit test since Tempo's shelf spec ships in the
  private consumer repo — see docstring on the pin test).
* **Multi-tenancy** — a ``search`` with a ``tenant`` selector renders the
  ``X-Scope-OrgID`` header; without it against a multi-tenant Tempo (401) the
  connector surfaces a tenant requirement rather than a bare 401.
* **Readiness probe** — ``GET /ready`` succeeds without a tenant header.
* **Live dispatch + recorded fixtures** — ``search`` / ``trace`` /
  ``search_tags`` / ``search_tag_values`` / ``metrics_query_range`` dispatch
  end-to-end and return the Tempo payload.
* **Optional auth** — ``secret_ref=None`` sends no Authorization header; a
  ``token`` secret yields Bearer; ``username``/``password`` yields Basic.

respx mocks the wire; the in-process Vault fake exercises the real credential
loader. Mirrors :mod:`tests.test_connectors_loki`.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
    registered_product_tokens,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.connectors.tempo import (
    TEMPO_OPS,
    TempoConnector,
    TempoReadOnlyError,
    TempoTenantRequiredError,
    assert_tempo_read_only,
)
from meho_backplane.connectors.tempo.connector import (
    _BUILDINFO_PATH,
    _ECHO_PATH,
    _READY_PATH,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_CANARY_TOKEN = "tempo-bearer-canary-must-not-leak"

_PRODUCT = "tempo"
_VERSION = "2.x"
_IMPL_ID = "tempo-api"
_CONNECTOR_ID = "tempo-api-2.x"

_TEMPO_HOST = "tempo-reads.test.invalid"
_TEMPO_PORT = 3200
_TEMPO_BASE_URL = f"http://{_TEMPO_HOST}:{_TEMPO_PORT}"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (Vault client + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher/handler caches + connector registry around every test."""
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=TempoConnector)
    register_connector_v2(product=_PRODUCT, version="", impl_id="", cls=TempoConnector)
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Deterministic embedding stub so registration doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384

    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _TempoTarget:
    """Target satisfying both the connector shape and the resolver shape."""

    def __init__(
        self,
        *,
        secret_ref: str | None = None,
        extras: dict[str, Any] | None = None,
    ) -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
        self.name = "tempo-reads"
        self.host = _TEMPO_HOST
        self.port = _TEMPO_PORT
        self.secret_ref = secret_ref
        self.auth_model = None
        self.verify_tls = True
        self.tls_ca_pin = None
        self.tls_server_name = None
        self.extras = extras or {}


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-reads-tempo",
        name="Tempo Reads Operator",
        email=None,
        raw_jwt="op.reads.tempo.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000c0c4"),
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_tempo_resolves_versioned_and_wildcard_and_appears_in_registry() -> None:
    """AC: tempo resolves via register_connector_v2 (versioned + wildcard)."""
    registry = all_connectors_v2()
    assert registry[("tempo", "2.x", "tempo-api")] is TempoConnector
    assert registry[("tempo", "", "")] is TempoConnector
    assert "tempo" in registered_product_tokens()

    # A fingerprinted target resolves to the connector; a version-less target
    # (fresh, unfingerprinted) still resolves through the wildcard fallback.
    assert resolve_connector(_TempoTarget()) is TempoConnector
    fresh = _TempoTarget()
    fresh.fingerprint = type("_FP", (), {"version": None})()
    assert resolve_connector(fresh) is TempoConnector


def test_every_op_is_safe_read_only_with_closed_schema() -> None:
    """AC: no write op — every registered op is safe/read-only/no-approval."""
    assert {op.op_id for op in TEMPO_OPS} == {
        "tempo.search",
        "tempo.trace",
        "tempo.search_tags",
        "tempo.search_tag_values",
        "tempo.metrics_query_range",
        "tempo.get",
    }
    for op in TEMPO_OPS:
        assert op.safety_level == "safe", op.op_id
        assert op.requires_approval is False, op.op_id
        assert "read-only" in op.tags, op.op_id
        assert op.parameter_schema.get("additionalProperties") is False, op.op_id


# ---------------------------------------------------------------------------
# Read-only gate — no upstream call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/search"),
        ("PUT", "/api/traces/abc"),
        ("DELETE", "/api/overrides"),
        ("PATCH", "/api/overrides"),
        ("GET", "/ready"),
        ("GET", "/flush"),
        ("GET", "/shutdown"),
        ("GET", "/metrics"),
        ("GET", "/status/version"),
        ("GET", "/"),
    ],
)
def test_read_only_gate_rejects_writes_and_off_surface(method: str, path: str) -> None:
    """AC: non-GET and any path outside /api are rejected with no upstream call."""
    with pytest.raises(TempoReadOnlyError):
        assert_tempo_read_only(method, path)


@pytest.mark.parametrize(
    "path",
    [
        "/api",
        "/api/search",
        "/api/traces/1234abcd",
        "/api/v2/traces/1234abcd",
        "/api/v2/search/tags",
        "/api/v2/search/tag/.service.name/values",
        "/api/metrics/query_range",
        "/api/echo",
    ],
)
def test_read_only_gate_accepts_reads(path: str) -> None:
    """A GET under /api passes the gate."""
    assert_tempo_read_only("GET", path)  # returns None; raises on violation


# ---------------------------------------------------------------------------
# Wire-path pinning — each curated op hits its exact documented /api path
# ---------------------------------------------------------------------------


class _RecordingTempo(TempoConnector):
    """Records every path the curated ops ask ``_tempo_get`` for; no wire, no auth."""

    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    async def _tempo_get(  # type: ignore[override]
        self,
        operator: Any,
        target: Any,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        del operator, target, params, tenant
        self.paths.append(path)
        return {}


def _minimal_params(parameter_schema: dict[str, Any]) -> dict[str, Any]:
    """Synthesise a minimal call payload from an op's *required* params.

    Driven by the live parameter schema (never a hardcoded param map). The
    universal placeholder ``"abc123"`` satisfies every required string prop's
    pattern here (``trace_id`` hex, ``tag``/``q`` non-whitespace); an array
    prop would get a one-item list.
    """
    properties = parameter_schema.get("properties", {})
    payload: dict[str, Any] = {}
    for name in parameter_schema.get("required", []):
        prop_type = properties.get(name, {}).get("type")
        payload[name] = ["abc123"] if prop_type == "array" else "abc123"
    return payload


def _captured_paths_by_op() -> dict[str, set[str]]:
    """Run every curated op's handler against the recording seam.

    The agent-path passthrough (``tempo.get``) is excluded — it hand-codes no
    vendor path.
    """

    async def _capture() -> dict[str, set[str]]:
        connector = _RecordingTempo()
        captured: dict[str, set[str]] = {}
        for op in TEMPO_OPS:
            if op.op_id == "tempo.get":
                continue
            handler = getattr(connector, op.handler_attr)
            connector.paths = []
            await handler(object(), object(), _minimal_params(op.parameter_schema))
            captured[op.op_id] = set(connector.paths)
        return captured

    return asyncio.run(_capture())


def test_each_curated_op_hits_its_exact_api_path() -> None:
    """Guard: every curated op funnels through _tempo_get at its documented path.

    A handler that grows a new inline path literal, stops funnelling through
    ``_tempo_get``, or a new curated op that skips the capture changes this
    mapping and fails here. ``tempo.get`` is absent by design (agent-supplied
    path). This is the always-on half of the #2980 reconcile standard; the
    served-set reconcile against a pinned Tempo API reference ships as a
    follow-up once ``tempo-2.x`` is pinned on the consumer spec shelf (the
    loki #2991 : connector #2235 split).
    """
    assert _captured_paths_by_op() == {
        "tempo.search": {"/api/search"},
        "tempo.trace": {"/api/traces/abc123"},
        "tempo.search_tags": {"/api/v2/search/tags"},
        "tempo.search_tag_values": {"/api/v2/search/tag/abc123/values"},
        "tempo.metrics_query_range": {"/api/metrics/query_range"},
    }


def test_fingerprint_probe_literals_are_pinned() -> None:
    """Guard: the tenant-free fingerprint/probe constants can't drift uncovered.

    ``_BUILDINFO_PATH`` / ``_READY_PATH`` / ``_ECHO_PATH`` ride the
    ``_unauth_get`` seam (a different path than the curated ops' ``_tempo_get``),
    so a new hand-coded probe path is a conscious edit here.
    """
    assert _BUILDINFO_PATH == "/api/status/buildinfo"
    assert _READY_PATH == "/ready"
    assert _ECHO_PATH == "/api/echo"


# ---------------------------------------------------------------------------
# Live dispatch — each op hits the right path and returns the payload
# ---------------------------------------------------------------------------

_SEARCH_RESPONSE: dict[str, Any] = {
    "traces": [
        {
            "traceID": "1234abcd",
            "rootServiceName": "api",
            "rootTraceName": "GET /orders",
            "startTimeUnixNano": "1720000000000000000",
            "durationMs": 42,
        }
    ],
    "metrics": {"inspectedTraces": 1},
}
_TRACE_RESPONSE: dict[str, Any] = {"batches": [{"resource": {}, "scopeSpans": []}]}
_TAGS_RESPONSE: dict[str, Any] = {
    "scopes": [{"name": "resource", "tags": ["service.name"]}],
    "metrics": {"inspectedBytes": "1"},
}
_TAG_VALUES_RESPONSE: dict[str, Any] = {
    "tagValues": [{"type": "string", "value": "api"}],
    "metrics": {"inspectedBytes": "1"},
}
_METRICS_RESPONSE: dict[str, Any] = {"status": "success", "data": {"result": []}}


async def _register_ops(_stub_embedding: AsyncMock) -> None:
    await TempoConnector.register_operations()


@pytest.mark.parametrize(
    ("op_id", "params", "path", "payload"),
    [
        (
            "tempo.search",
            {"q": '{ .service.name = "api" }'},
            "/api/search",
            _SEARCH_RESPONSE,
        ),
        (
            "tempo.trace",
            {"trace_id": "1234abcd", "start": 1720000000},
            "/api/traces/1234abcd",
            _TRACE_RESPONSE,
        ),
        ("tempo.search_tags", {"scope": "resource"}, "/api/v2/search/tags", _TAGS_RESPONSE),
        (
            "tempo.search_tag_values",
            {"tag": ".service.name"},
            "/api/v2/search/tag/.service.name/values",
            _TAG_VALUES_RESPONSE,
        ),
        (
            "tempo.metrics_query_range",
            {"q": "{ } | rate()", "since": "1h"},
            "/api/metrics/query_range",
            _METRICS_RESPONSE,
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_read_op_dispatches_live_and_returns_payload(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    params: dict[str, Any],
    path: str,
    payload: dict[str, Any],
) -> None:
    """AC: search / trace / tags / tag_values / metrics dispatch and return the payload."""
    await _register_ops(_stub_embedding)

    async with respx.mock(base_url=_TEMPO_BASE_URL, assert_all_called=False) as mock:
        route = mock.get(path).respond(200, json=payload)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=_TempoTarget(),
            params=params,
        )

    assert result.status == "ok", result.error
    assert result.result == payload
    assert route.called and route.call_count == 1
    # Unauthenticated target: no Authorization header on the wire.
    assert route.calls[0].request.headers.get("authorization") is None


@pytest.mark.asyncio
async def test_metrics_query_range_sends_traceql_q(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """tempo.metrics_query_range forwards the required TraceQL 'q' as a query param."""
    await _register_ops(_stub_embedding)

    async with respx.mock(base_url=_TEMPO_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/api/metrics/query_range").respond(200, json=_METRICS_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="tempo.metrics_query_range",
            target=_TempoTarget(),
            params={"q": "{ } | rate()"},
        )

    assert result.status == "ok", result.error
    assert "q=" in str(route.calls[0].request.url)


@pytest.mark.asyncio
async def test_get_passthrough_rejects_off_surface_without_upstream_call(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """AC: the tempo.get passthrough refuses a path outside /api, no wire call."""
    await _register_ops(_stub_embedding)

    async with respx.mock(base_url=_TEMPO_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/flush").respond(200, json={})
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="tempo.get",
            target=_TempoTarget(),
            params={"path": "/flush"},
        )

    assert result.status == "error"
    assert not route.called


@pytest.mark.asyncio
async def test_get_passthrough_reaches_api_path(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """The tempo.get passthrough dispatches a GET to a read path under /api."""
    await _register_ops(_stub_embedding)

    async with respx.mock(base_url=_TEMPO_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/api/v2/traces/1234abcd").respond(200, json=_TRACE_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="tempo.get",
            target=_TempoTarget(),
            params={"path": "/api/v2/traces/1234abcd"},
        )

    assert result.status == "ok", result.error
    assert result.result == _TRACE_RESPONSE
    assert route.called


# ---------------------------------------------------------------------------
# Multi-tenancy — X-Scope-OrgID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_with_tenant_sends_x_scope_orgid(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """AC: a search with a tenant selector renders the X-Scope-OrgID header."""
    await _register_ops(_stub_embedding)

    async with respx.mock(base_url=_TEMPO_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/api/search").respond(200, json=_SEARCH_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="tempo.search",
            target=_TempoTarget(),
            params={"q": "{ }", "tenant": "team-a"},
        )

    assert result.status == "ok", result.error
    assert route.calls[0].request.headers.get("x-scope-orgid") == "team-a"


@pytest.mark.asyncio
async def test_search_without_tenant_surfaces_tenant_requirement_on_401(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """AC: a tenant-less search against a multi-tenant Tempo (401) surfaces the requirement."""
    await _register_ops(_stub_embedding)

    async with respx.mock(base_url=_TEMPO_BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/search").respond(401, text="no org id")
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="tempo.search",
            target=_TempoTarget(),
            params={"q": "{ }"},
        )

    assert result.status == "error"
    assert result.error is not None
    # The tenant requirement is surfaced as the dedicated error type, not a
    # bare 401 / HTTPStatusError passthrough.
    assert "tenantrequired" in result.error.lower().replace("_", "")
    assert "httpstatuserror" not in result.error.lower()


@pytest.mark.asyncio
async def test_tempo_tenant_required_error_raised_directly() -> None:
    """The _tempo_get helper raises TempoTenantRequiredError on a tenant-less 401."""
    connector = TempoConnector()
    try:
        async with respx.mock(base_url=_TEMPO_BASE_URL, assert_all_called=False) as mock:
            mock.get("/api/search").respond(401, text="no org id")
            with pytest.raises(TempoTenantRequiredError):
                await connector._tempo_get(
                    _make_operator(), _TempoTarget(), "/api/search", params={"q": "x"}
                )
    finally:
        await connector.aclose()


# ---------------------------------------------------------------------------
# Readiness probe — tenant-free
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_ok_without_tenant_header() -> None:
    """AC: GET /ready succeeds without a tenant header."""
    connector = TempoConnector()
    try:
        async with respx.mock(base_url=_TEMPO_BASE_URL, assert_all_called=False) as mock:
            route = mock.get("/ready").respond(200, text="ready\n")
            result = await connector.probe(_TempoTarget())
    finally:
        await connector.aclose()

    assert result.ok is True
    assert route.called
    assert route.calls[0].request.headers.get("x-scope-orgid") is None


@pytest.mark.asyncio
async def test_probe_not_ready_maps_to_not_ok() -> None:
    """A 503 from /ready maps to a non-ok probe with a structured reason."""
    connector = TempoConnector()
    try:
        async with respx.mock(base_url=_TEMPO_BASE_URL, assert_all_called=False) as mock:
            mock.get("/ready").respond(503, text="Ingester not ready")
            result = await connector.probe(_TempoTarget())
    finally:
        await connector.aclose()

    assert result.ok is False
    assert result.reason is not None


@pytest.mark.asyncio
async def test_fingerprint_from_buildinfo_ready_and_echo() -> None:
    """fingerprint reads version+revision from buildinfo, ready flag, and echo_ok."""
    connector = TempoConnector()
    try:
        async with respx.mock(base_url=_TEMPO_BASE_URL, assert_all_called=False) as mock:
            mock.get("/api/status/buildinfo").respond(
                200, json={"version": "2.6.1", "revision": "abc123", "branch": "HEAD"}
            )
            mock.get("/ready").respond(200, text="ready")
            mock.get("/api/echo").respond(200, text="echo")
            fp = await connector.fingerprint(_TempoTarget())
    finally:
        await connector.aclose()

    assert fp.reachable is True
    assert fp.vendor == "grafana"
    assert fp.product == "tempo"
    assert fp.version == "2.6.1"
    assert fp.extras["revision"] == "abc123"
    assert fp.extras["ready"] is True
    assert fp.extras["echo_ok"] is True


@pytest.mark.asyncio
async def test_fingerprint_unreachable_maps_to_not_reachable() -> None:
    """A transport failure on buildinfo maps to reachable=False with an error."""
    connector = TempoConnector()
    try:
        async with respx.mock(base_url=_TEMPO_BASE_URL, assert_all_called=False) as mock:
            mock.get("/api/status/buildinfo").mock(
                side_effect=__import__("httpx").ConnectError("boom")
            )
            fp = await connector.fingerprint(_TempoTarget())
    finally:
        await connector.aclose()

    assert fp.reachable is False
    assert "error" in fp.extras


# ---------------------------------------------------------------------------
# Optional auth — Bearer / Basic / none
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_optional_when_no_secret_ref() -> None:
    """AC: secret_ref=None sends no Authorization header (optional auth)."""
    connector = TempoConnector()
    headers = await connector.auth_headers(_TempoTarget(secret_ref=None), _make_operator())
    assert headers == {}


@pytest.mark.asyncio
async def test_bearer_auth_when_token_secret(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token secret yields an Authorization: Bearer header on the wire."""
    await _register_ops(_stub_embedding)
    install_fake_client(monkeypatch, secret={"token": _CANARY_TOKEN})

    async with respx.mock(base_url=_TEMPO_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/api/v2/search/tags").respond(200, json=_TAGS_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="tempo.search_tags",
            target=_TempoTarget(secret_ref="targets/op-reads/tempo"),
            params={},
        )

    assert result.status == "ok", result.error
    assert route.calls[0].request.headers.get("authorization") == f"Bearer {_CANARY_TOKEN}"


@pytest.mark.asyncio
async def test_basic_auth_when_username_password_secret(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A username/password secret yields an Authorization: Basic header."""
    await _register_ops(_stub_embedding)
    install_fake_client(monkeypatch, secret={"username": "tempo-ro", "password": "s3cr3t"})

    async with respx.mock(base_url=_TEMPO_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/api/v2/search/tags").respond(200, json=_TAGS_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="tempo.search_tags",
            target=_TempoTarget(secret_ref="targets/op-reads/tempo"),
            params={},
        )

    assert result.status == "ok", result.error
    expected = base64.b64encode(b"tempo-ro:s3cr3t").decode("ascii")
    assert route.calls[0].request.headers.get("authorization") == f"Basic {expected}"
