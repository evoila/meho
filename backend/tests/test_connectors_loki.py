# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Loki read-only, multi-tenant connector (#2235).

Coverage matrix (per Task #2235 acceptance criteria):

* **Registration** — ``loki`` resolves via ``register_connector_v2`` (versioned
  triple + wildcard), appears in ``all_connectors_v2()`` and
  ``registered_product_tokens()``.
* **Read-only gate** — :func:`assert_loki_read_only` rejects non-GET, ``/push``,
  ``/delete*`` (even ``GET /delete``), and paths outside ``/loki/api/v1``, with
  no upstream call.
* **Multi-tenancy** — a ``query`` with a ``tenant`` selector renders the
  ``X-Scope-OrgID`` header; without it against an ``auth_enabled`` Loki
  (401) the connector surfaces a tenant requirement rather than a bare 401.
* **Readiness probe** — ``GET /ready`` succeeds without a tenant header.
* **Live dispatch + recorded fixtures** — ``query_range`` and ``label_values``
  (and the rest) dispatch end-to-end and return the Loki payload.
* **Optional auth** — ``secret_ref=None`` sends no Authorization header; a
  ``token`` secret yields Bearer; ``username``/``password`` yields Basic.

respx mocks the wire; the in-process Vault fake exercises the real credential
loader. Mirrors :mod:`tests.test_connectors_argocd_reads`.
"""

from __future__ import annotations

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
from meho_backplane.connectors.loki import (
    LOKI_OPS,
    LokiConnector,
    LokiReadOnlyError,
    LokiTenantRequiredError,
    assert_loki_read_only,
)
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
    registered_product_tokens,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_CANARY_TOKEN = "loki-bearer-canary-must-not-leak"

_PRODUCT = "loki"
_VERSION = "3.x"
_IMPL_ID = "loki-api"
_CONNECTOR_ID = "loki-api-3.x"

_LOKI_HOST = "loki-reads.test.invalid"
_LOKI_PORT = 3100
_LOKI_BASE_URL = f"http://{_LOKI_HOST}:{_LOKI_PORT}"


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
    register_connector_v2(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=LokiConnector)
    register_connector_v2(product=_PRODUCT, version="", impl_id="", cls=LokiConnector)
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


class _LokiTarget:
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
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
        self.name = "loki-reads"
        self.host = _LOKI_HOST
        self.port = _LOKI_PORT
        self.secret_ref = secret_ref
        self.auth_model = None
        self.verify_tls = True
        self.tls_ca_pin = None
        self.tls_server_name = None
        self.extras = extras or {}


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-reads-loki",
        name="Loki Reads Operator",
        email=None,
        raw_jwt="op.reads.loki.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000b0b4"),
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_loki_resolves_versioned_and_wildcard_and_appears_in_registry() -> None:
    """AC: loki resolves via register_connector_v2 (versioned + wildcard)."""
    registry = all_connectors_v2()
    assert registry[("loki", "3.x", "loki-api")] is LokiConnector
    assert registry[("loki", "", "")] is LokiConnector
    assert "loki" in registered_product_tokens()

    # A fingerprinted target resolves to the connector; a version-less target
    # (fresh, unfingerprinted) still resolves through the wildcard fallback.
    assert resolve_connector(_LokiTarget()) is LokiConnector
    fresh = _LokiTarget()
    fresh.fingerprint = type("_FP", (), {"version": None})()
    assert resolve_connector(fresh) is LokiConnector


def test_every_op_is_safe_read_only_with_closed_schema() -> None:
    """AC: no write op — every registered op is safe/read-only/no-approval."""
    assert {op.op_id for op in LOKI_OPS} == {
        "loki.query",
        "loki.query_range",
        "loki.labels",
        "loki.label_values",
        "loki.series",
        "loki.get",
    }
    for op in LOKI_OPS:
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
        ("POST", "/loki/api/v1/push"),
        ("PUT", "/loki/api/v1/push"),
        ("GET", "/loki/api/v1/push"),
        ("POST", "/loki/api/v1/delete"),
        ("DELETE", "/loki/api/v1/delete"),
        ("GET", "/loki/api/v1/delete"),
        ("GET", "/loki/api/v1/delete_requests"),
        ("POST", "/loki/api/v1/query"),
        ("GET", "/admin/api/v1/status"),
        ("GET", "/metrics"),
    ],
)
def test_read_only_gate_rejects_writes_and_off_surface(method: str, path: str) -> None:
    """AC: /push, /delete*, non-GET, and off-surface paths are rejected."""
    with pytest.raises(LokiReadOnlyError):
        assert_loki_read_only(method, path)


@pytest.mark.parametrize(
    "path",
    [
        "/loki/api/v1/query",
        "/loki/api/v1/query_range",
        "/loki/api/v1/labels",
        "/loki/api/v1/label/namespace/values",
        "/loki/api/v1/series",
        "/loki/api/v1/index/stats",
    ],
)
def test_read_only_gate_accepts_reads(path: str) -> None:
    """A GET under /loki/api/v1 that is not push/delete passes the gate."""
    assert_loki_read_only("GET", path)  # returns None; raises on violation


def test_read_only_gate_allows_label_value_named_like_a_write_verb() -> None:
    """A label *value* named 'pushgateway' is a segment, not the /push endpoint."""
    assert_loki_read_only(  # returns None; raises on violation
        "GET", "/loki/api/v1/label/job/values?query=pushgateway"
    )


# ---------------------------------------------------------------------------
# Live dispatch — each op hits the right path and returns the payload
# ---------------------------------------------------------------------------

_QUERY_RANGE_RESPONSE: dict[str, Any] = {
    "status": "success",
    "data": {
        "resultType": "streams",
        "result": [
            {"stream": {"app": "api"}, "values": [["1720000000000000000", "boot ok"]]},
        ],
    },
}
_LABEL_VALUES_RESPONSE: dict[str, Any] = {"status": "success", "data": ["prod", "staging"]}
_QUERY_RESPONSE: dict[str, Any] = {
    "status": "success",
    "data": {"resultType": "vector", "result": []},
}
_LABELS_RESPONSE: dict[str, Any] = {"status": "success", "data": ["app", "namespace"]}
_SERIES_RESPONSE: dict[str, Any] = {"status": "success", "data": [{"app": "api"}]}


async def _register_ops(_stub_embedding: AsyncMock) -> None:
    await LokiConnector.register_operations()


@pytest.mark.parametrize(
    ("op_id", "params", "path", "payload"),
    [
        (
            "loki.query",
            {"query": '{app="api"}'},
            "/loki/api/v1/query",
            _QUERY_RESPONSE,
        ),
        (
            "loki.query_range",
            {"query": '{app="api"}', "start": "1720000000", "limit": 100},
            "/loki/api/v1/query_range",
            _QUERY_RANGE_RESPONSE,
        ),
        ("loki.labels", {}, "/loki/api/v1/labels", _LABELS_RESPONSE),
        (
            "loki.label_values",
            {"name": "namespace"},
            "/loki/api/v1/label/namespace/values",
            _LABEL_VALUES_RESPONSE,
        ),
        (
            "loki.series",
            {"match": ['{app="api"}']},
            "/loki/api/v1/series",
            _SERIES_RESPONSE,
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
    """AC: query_range + label_values (and siblings) dispatch and return the payload."""
    await _register_ops(_stub_embedding)

    async with respx.mock(base_url=_LOKI_BASE_URL, assert_all_called=False) as mock:
        route = mock.get(path).respond(200, json=payload)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=_LokiTarget(),
            params=params,
        )

    assert result.status == "ok", result.error
    assert result.result == payload
    assert route.called and route.call_count == 1
    # Unauthenticated target: no Authorization header on the wire.
    assert route.calls[0].request.headers.get("authorization") is None


@pytest.mark.asyncio
async def test_series_forwards_repeated_match_param(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """loki.series sends each selector as a repeated match[] query param."""
    await _register_ops(_stub_embedding)

    async with respx.mock(base_url=_LOKI_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/loki/api/v1/series").respond(200, json=_SERIES_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="loki.series",
            target=_LokiTarget(),
            params={"match": ['{app="api"}', '{app="web"}']},
        )

    assert result.status == "ok", result.error
    sent = str(route.calls[0].request.url)
    assert sent.count("match%5B%5D=") == 2 or sent.count("match[]=") == 2


@pytest.mark.asyncio
async def test_get_passthrough_rejects_delete_without_upstream_call(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """AC: the loki.get passthrough refuses the /delete surface, no wire call."""
    await _register_ops(_stub_embedding)

    async with respx.mock(base_url=_LOKI_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/loki/api/v1/delete").respond(200, json={})
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="loki.get",
            target=_LokiTarget(),
            params={"path": "/loki/api/v1/delete"},
        )

    assert result.status == "error"
    assert not route.called


# ---------------------------------------------------------------------------
# Multi-tenancy — X-Scope-OrgID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_with_tenant_sends_x_scope_orgid(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """AC: a query with a tenant selector renders the X-Scope-OrgID header."""
    await _register_ops(_stub_embedding)

    async with respx.mock(base_url=_LOKI_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/loki/api/v1/query").respond(200, json=_QUERY_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="loki.query",
            target=_LokiTarget(),
            params={"query": '{app="api"}', "tenant": "team-a"},
        )

    assert result.status == "ok", result.error
    assert route.calls[0].request.headers.get("x-scope-orgid") == "team-a"


@pytest.mark.asyncio
async def test_query_without_tenant_surfaces_tenant_requirement_on_401(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """AC: a tenant-less query against auth_enabled Loki (401) surfaces the requirement."""
    await _register_ops(_stub_embedding)

    async with respx.mock(base_url=_LOKI_BASE_URL, assert_all_called=False) as mock:
        mock.get("/loki/api/v1/query").respond(401, text="no org id")
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="loki.query",
            target=_LokiTarget(),
            params={"query": '{app="api"}'},
        )

    assert result.status == "error"
    assert result.error is not None
    # The tenant requirement is surfaced as the dedicated error type, not a
    # bare 401 / HTTPStatusError passthrough.
    assert "tenantrequired" in result.error.lower().replace("_", "")
    assert "httpstatuserror" not in result.error.lower()


@pytest.mark.asyncio
async def test_loki_tenant_required_error_raised_directly() -> None:
    """The _loki_get helper raises LokiTenantRequiredError on a tenant-less 401."""
    connector = LokiConnector()
    try:
        async with respx.mock(base_url=_LOKI_BASE_URL, assert_all_called=False) as mock:
            mock.get("/loki/api/v1/query").respond(401, text="no org id")
            with pytest.raises(LokiTenantRequiredError):
                await connector._loki_get(
                    _make_operator(), _LokiTarget(), "/loki/api/v1/query", params={"query": "x"}
                )
    finally:
        await connector.aclose()


# ---------------------------------------------------------------------------
# Readiness probe — tenant-free
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_ok_without_tenant_header() -> None:
    """AC: GET /ready succeeds without a tenant header."""
    connector = LokiConnector()
    try:
        async with respx.mock(base_url=_LOKI_BASE_URL, assert_all_called=False) as mock:
            route = mock.get("/ready").respond(200, text="ready\n")
            result = await connector.probe(_LokiTarget())
    finally:
        await connector.aclose()

    assert result.ok is True
    assert route.called
    assert route.calls[0].request.headers.get("x-scope-orgid") is None


@pytest.mark.asyncio
async def test_probe_not_ready_maps_to_not_ok() -> None:
    """A 503 from /ready maps to a non-ok probe with a structured reason."""
    connector = LokiConnector()
    try:
        async with respx.mock(base_url=_LOKI_BASE_URL, assert_all_called=False) as mock:
            mock.get("/ready").respond(503, text="Ingester not ready")
            result = await connector.probe(_LokiTarget())
    finally:
        await connector.aclose()

    assert result.ok is False
    assert result.reason is not None


@pytest.mark.asyncio
async def test_fingerprint_from_buildinfo_ready_and_labels() -> None:
    """fingerprint reads version+revision from buildinfo and label_count from labels."""
    connector = LokiConnector()
    try:
        async with respx.mock(base_url=_LOKI_BASE_URL, assert_all_called=False) as mock:
            mock.get("/loki/api/v1/status/buildinfo").respond(
                200, json={"version": "3.1.0", "revision": "abc123", "branch": "HEAD"}
            )
            mock.get("/ready").respond(200, text="ready")
            mock.get("/loki/api/v1/labels").respond(
                200, json={"status": "success", "data": ["app", "namespace", "pod"]}
            )
            fp = await connector.fingerprint(_LokiTarget())
    finally:
        await connector.aclose()

    assert fp.reachable is True
    assert fp.vendor == "grafana"
    assert fp.product == "loki"
    assert fp.version == "3.1.0"
    assert fp.extras["revision"] == "abc123"
    assert fp.extras["ready"] is True
    assert fp.extras["label_count"] == 3


@pytest.mark.asyncio
async def test_fingerprint_unreachable_maps_to_not_reachable() -> None:
    """A transport failure on buildinfo maps to reachable=False with an error."""
    connector = LokiConnector()
    try:
        async with respx.mock(base_url=_LOKI_BASE_URL, assert_all_called=False) as mock:
            mock.get("/loki/api/v1/status/buildinfo").mock(
                side_effect=__import__("httpx").ConnectError("boom")
            )
            fp = await connector.fingerprint(_LokiTarget())
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
    connector = LokiConnector()
    headers = await connector.auth_headers(_LokiTarget(secret_ref=None), _make_operator())
    assert headers == {}


@pytest.mark.asyncio
async def test_bearer_auth_when_token_secret(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token secret yields an Authorization: Bearer header on the wire."""
    await _register_ops(_stub_embedding)
    install_fake_client(monkeypatch, secret={"token": _CANARY_TOKEN})

    async with respx.mock(base_url=_LOKI_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/loki/api/v1/labels").respond(200, json=_LABELS_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="loki.labels",
            target=_LokiTarget(secret_ref="targets/op-reads/loki"),
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
    install_fake_client(monkeypatch, secret={"username": "loki-ro", "password": "s3cr3t"})

    async with respx.mock(base_url=_LOKI_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/loki/api/v1/labels").respond(200, json=_LABELS_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="loki.labels",
            target=_LokiTarget(secret_ref="targets/op-reads/loki"),
            params={},
        )

    assert result.status == "ok", result.error
    expected = base64.b64encode(b"loki-ro:s3cr3t").decode("ascii")
    assert route.calls[0].request.headers.get("authorization") == f"Basic {expected}"
