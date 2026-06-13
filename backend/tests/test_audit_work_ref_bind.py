# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Audit ``work_ref`` request-boundary bind tests (work_ref I1-T2 #1657).

I1-T1 (#1655) landed ``audit_log.work_ref`` + the
:data:`meho_backplane.operations._audit.work_ref_var` ContextVar and the
three primary writers that stamp it; nothing SET the var, so every row
was NULL. This task wires the three bind sources so a ``work_ref``
supplied at a governed entry point flows onto the audit rows of that
call -- scoped per-request, with no leakage to the next call.

The three entry points, one test class each:

* **MCP tool argument / dispatch param** -- an optional ``work_ref``
  threaded through ``call_operation`` (the meta-tool shared by the MCP
  ``call_operation`` tool and the REST ``POST /api/v1/operations/call``
  route). :func:`meho_backplane.operations.meta_tools._call_operation_impl`
  binds it onto ``work_ref_var`` around the ``dispatch`` call, so the
  DISPATCH ``audit_log`` row carries it.
* **``Meho-Work-Ref`` HTTP header** -- read off the ASGI scope in
  :class:`meho_backplane.audit.AuditMiddleware` and bound around the
  chassis audit write, so the chassis ``audit_log`` row carries it.

Every case proves both halves of the contract: the value is stamped
when supplied, AND ``work_ref_var`` is reset afterward so a subsequent
unbound call lands a NULL ``work_ref`` (the per-call scoping the Goal
#1651 design requires -- "binds per-op / per-request", no session
store). The explicit per-op override is asserted where an arg and a
header carry *different* refs and the DISPATCH row keeps the arg's.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
import structlog
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import (
    create_engine_for_url,
    dispose_engine,
    get_sessionmaker,
    reset_engine_for_testing,
)
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.mcp.tools.operations import _call_operation_handler
from meho_backplane.operations import (
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks
from ._vault_fakes import install_fake_vault as _install_fake_vault

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_WORK_REF = "gh:evoila/meho#7"
_OTHER_REF = "gh:evoila/meho#99"


# ---------------------------------------------------------------------------
# Shared: typed-op stub + operator (mirrors tests.test_audit_work_ref)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    """Clear connector + dispatcher caches around each test."""
    clear_registry()
    reset_dispatcher_caches()
    yield
    clear_registry()
    reset_dispatcher_caches()


def _operator() -> Operator:
    """Test operator in tenant A."""
    return Operator(
        sub="alice@example.com",
        name="Alice",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
    )


class _StubConnector(Connector):
    """Minimal connector so the dispatcher resolver finds a class for the triple."""

    product = "stub"
    version = "1.x"
    impl_id = "stub"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _echo_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Typed handler: return a trivial result."""
    return {"ok": True}


async def _seed_typed_op(stub_embedding_service: AsyncMock) -> None:
    """Register the stub connector + typed op the dispatcher can find."""
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    await register_typed_operation(
        product="stub",
        version="1.x",
        impl_id="stub",
        op_id="stub.op_call",
        handler=_echo_handler,
        summary="Stub op for work_ref bind tests.",
        description="Echo a result; used to assert the DISPATCH row's work_ref.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )


async def _dispatch_rows(db_session: AsyncSession) -> list[AuditLog]:
    """Every DISPATCH ``audit_log`` row, oldest first."""
    result = await db_session.scalars(
        select(AuditLog).where(AuditLog.method == "DISPATCH").order_by(AuditLog.occurred_at)
    )
    return list(result.all())


# ===========================================================================
# Entry points 1 + 3: MCP tool argument / dispatch param (via call_operation)
# ===========================================================================


@pytest.fixture
def _meta_tool_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires (conftest provides the DB)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """One :class:`AsyncSession` per test for direct row inspection."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Embedding stub for the typed-op descriptor's embedding column."""
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


@pytest.mark.usefixtures("_meta_tool_env")
async def test_dispatch_param_binds_work_ref_on_dispatch_row(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """An explicit ``work_ref`` arg on ``call_operation`` lands on the DISPATCH row.

    Covers the dispatch-param entry point: the value is threaded through
    the ``arguments`` dict (the same shape the REST ``CallOperationBody``
    serialises to) and bound around the dispatcher.
    """
    await _seed_typed_op(stub_embedding_service)
    assert work_ref_var.get() is None

    await call_operation(
        _operator(),
        {
            "connector_id": "stub-1.x",
            "op_id": "stub.op_call",
            "target": None,
            "params": {},
            "work_ref": _WORK_REF,
        },
    )

    rows = await _dispatch_rows(db_session)
    assert len(rows) == 1
    assert rows[0].work_ref == _WORK_REF  # type: ignore[attr-defined]
    # The bind was per-call: nothing leaked back onto this task.
    assert work_ref_var.get() is None


@pytest.mark.usefixtures("_meta_tool_env")
async def test_mcp_tool_argument_binds_then_resets_no_leak(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """MCP ``call_operation`` tool arg binds; a second bare call leaves NULL.

    Drives the registered MCP handler (``_call_operation_handler``) so
    the entry point is the literal MCP-tool path, not just the meta-tool
    function. First call carries ``work_ref`` (acceptance criterion: the
    row carries it); second call omits it (acceptance criterion: NULL --
    per-call scoping, no leak across calls).
    """
    await _seed_typed_op(stub_embedding_service)
    operator = _operator()

    # 1. MCP tool call WITH work_ref.
    await _call_operation_handler(
        operator,
        {
            "connector_id": "stub-1.x",
            "op_id": "stub.op_call",
            "work_ref": _WORK_REF,
        },
    )
    # 2. MCP tool call WITHOUT work_ref -- must not inherit #1's ref.
    await _call_operation_handler(
        operator,
        {
            "connector_id": "stub-1.x",
            "op_id": "stub.op_call",
        },
    )

    rows = await _dispatch_rows(db_session)
    assert len(rows) == 2
    assert rows[0].work_ref == _WORK_REF  # type: ignore[attr-defined]
    assert rows[1].work_ref is None  # type: ignore[attr-defined]
    assert work_ref_var.get() is None


@pytest.mark.usefixtures("_meta_tool_env")
async def test_explicit_arg_overrides_ambient_binding(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """An explicit per-op ``work_ref`` arg overrides an ambient bind.

    Simulates the layered case the Goal #1651 design calls for: a
    ``Meho-Work-Ref`` header already bound ``work_ref_var`` ambiently
    (here set directly, the same way the chassis middleware would), and
    a differing per-op ``work_ref`` arg is supplied. The DISPATCH row
    must carry the ARG value (the override), and after the call the
    ambient binding must be restored intact -- the per-op override is
    scoped to the dispatch, it does not clobber the surrounding scope.
    """
    await _seed_typed_op(stub_embedding_service)

    ambient_token = work_ref_var.set(_OTHER_REF)
    try:
        await call_operation(
            _operator(),
            {
                "connector_id": "stub-1.x",
                "op_id": "stub.op_call",
                "work_ref": _WORK_REF,
            },
        )
        # The override is scoped to the dispatch: the ambient binding is
        # restored on return (reset(token) put _OTHER_REF back).
        assert work_ref_var.get() == _OTHER_REF
    finally:
        work_ref_var.reset(ambient_token)

    rows = await _dispatch_rows(db_session)
    assert len(rows) == 1
    assert rows[0].work_ref == _WORK_REF  # type: ignore[attr-defined]


@pytest.mark.usefixtures("_meta_tool_env")
async def test_bare_call_does_not_clobber_ambient_binding(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """No ``work_ref`` arg → the ambient (header) binding still reaches the row.

    The override is opt-in: a ``call_operation`` without a ``work_ref``
    arg must NOT reset the var to NULL. This is what lets a
    ``Meho-Work-Ref`` header bound at the chassis flow down to the inner
    DISPATCH row when the caller did not also pass a per-op override.
    """
    await _seed_typed_op(stub_embedding_service)

    ambient_token = work_ref_var.set(_OTHER_REF)
    try:
        await call_operation(
            _operator(),
            {
                "connector_id": "stub-1.x",
                "op_id": "stub.op_call",
                "params": {},
            },
        )
        assert work_ref_var.get() == _OTHER_REF
    finally:
        work_ref_var.reset(ambient_token)

    rows = await _dispatch_rows(db_session)
    assert len(rows) == 1
    assert rows[0].work_ref == _OTHER_REF  # type: ignore[attr-defined]


async def test_dispatcher_param_bind_resets_on_handler_error(
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``work_ref_var`` is reset even when ``dispatch`` raises.

    The ``try/finally`` around the dispatch must restore the prior value
    on the error path too, so a failed governed call cannot leak its ref
    onto the next request on the same task. Patches ``dispatch`` (as the
    meta-tool imports it) to raise, asserts the var is clean afterward.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()

    import meho_backplane.operations.meta_tools as meta_tools

    async def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("dispatch blew up")

    monkeypatch.setattr(meta_tools, "dispatch", _boom)

    assert work_ref_var.get() is None
    with pytest.raises(RuntimeError, match="dispatch blew up"):
        await call_operation(
            _operator(),
            {
                "connector_id": "stub-1.x",
                "op_id": "stub.op_call",
                "work_ref": _WORK_REF,
            },
        )
    # finally-block reset fired despite the exception.
    assert work_ref_var.get() is None
    get_settings.cache_clear()


# ===========================================================================
# Entry point 2: Meho-Work-Ref HTTP header (via AuditMiddleware on the app)
# ===========================================================================


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    """Empty the module-level JWKS cache around every test."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture
def _http_settings_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """Pin Settings env + a tmp-path SQLite DB (mirrors tests.test_audit_middleware)."""
    db_path = tmp_path / "audit.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def _audit_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Per-test SQLite URL + ``alembic upgrade head`` (sync; mirrors siblings)."""
    db_path = tmp_path / "audit.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
async def isolated_audit_engine(_audit_db_url: str) -> AsyncIterator[AsyncEngine]:
    """Per-test aiosqlite engine bound to the migrated audit DB."""
    reset_engine_for_testing()
    engine = create_engine_for_url(_audit_db_url, pool_size=5, pool_timeout=10.0)
    engine_module._engine = engine
    try:
        yield engine
    finally:
        await dispose_engine()
        reset_engine_for_testing()


async def _fetch_audit_rows(engine: AsyncEngine) -> list[AuditLog]:
    """Read every ``audit_log`` row in order."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


def _hit_health(monkeypatch: pytest.MonkeyPatch, **headers: str) -> Any:
    """Issue an authenticated ``GET /api/v1/health`` with optional extra headers."""
    key = _make_rsa_keypair("kid-wrh")
    token = _mint_token(key, sub="op-wrh", name="Op", email="op@example.com")
    _install_fake_vault(monkeypatch)
    client = TestClient(app)
    request_headers = {"Authorization": f"Bearer {token}", **headers}
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        return client.get("/api/v1/health", headers=request_headers)


@pytest.mark.usefixtures("_http_settings_env")
async def test_meho_work_ref_header_stamped_on_chassis_row(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Meho-Work-Ref`` request header lands on the chassis ``audit_log`` row."""
    response = _hit_health(monkeypatch, **{"Meho-Work-Ref": _WORK_REF})
    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    assert rows[0].work_ref == _WORK_REF  # type: ignore[attr-defined]


@pytest.mark.usefixtures("_http_settings_env")
async def test_absent_header_leaves_work_ref_null(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``Meho-Work-Ref`` header → chassis ``audit_log.work_ref`` is NULL."""
    response = _hit_health(monkeypatch)
    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    assert rows[0].work_ref is None  # type: ignore[attr-defined]


@pytest.mark.usefixtures("_http_settings_env")
async def test_header_value_is_case_insensitive(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The header name match is case-insensitive (RFC 9110 §5.1)."""
    response = _hit_health(monkeypatch, **{"meho-WORK-ref": _WORK_REF})
    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert rows[0].work_ref == _WORK_REF  # type: ignore[attr-defined]


async def _drive_audit_middleware(scope_headers: list[tuple[bytes, bytes]]) -> None:
    """Run :class:`AuditMiddleware` once, in this task, with bound identity.

    Binds ``operator_sub`` / ``tenant_id`` structlog contextvars (the
    shape :func:`verify_jwt_and_bind` produces) so the middleware reaches
    the real audit-write path -- and thus the ``Meho-Work-Ref`` bind --
    rather than short-circuiting on the unauthenticated skip. A trivial
    inner ASGI app returns a 200 so the buffered-forward path completes.
    """

    async def _inner_app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(_message: dict[str, Any]) -> None:
        return None

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/operations/call",
        "headers": scope_headers,
    }
    structlog.contextvars.bind_contextvars(
        operator_sub="op-direct",
        tenant_id=str(_TENANT_A),
    )
    try:
        await AuditMiddleware(_inner_app)(scope, _receive, _send)
    finally:
        structlog.contextvars.clear_contextvars()


@pytest.mark.usefixtures("_http_settings_env")
async def test_header_bind_stamps_row_and_resets_in_task(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving the middleware in-task: row carries the ref, then var resets.

    The rigorous no-leak proof for the header entry point. Runs
    :class:`AuditMiddleware` directly (same async task, real engine) with
    a ``Meho-Work-Ref`` header: the chassis ``audit_log`` row must carry
    the ref AND ``work_ref_var`` must be back to ``None`` afterward --
    the ``finally`` ``reset(token)`` ran, so nothing leaks onto the next
    request sharing this task. (The end-to-end app path is covered by
    :func:`test_meho_work_ref_header_stamped_on_chassis_row` above;
    asserting the reset bracketing directly avoids the awkward
    two-``TestClient``-requests-in-one-test shape -- the same approach
    :mod:`tests.test_broadcast_detail_header` takes for its
    contextvar-unbind test.)
    """
    import meho_backplane.audit as audit_module

    async def _noop_publish(_event: Any) -> None:
        return None

    monkeypatch.setattr(audit_module, "publish_event", _noop_publish)

    assert work_ref_var.get() is None
    await _drive_audit_middleware([(b"meho-work-ref", _WORK_REF.encode("latin-1"))])
    # The bind was reset on the way out -- no leak onto the task.
    assert work_ref_var.get() is None

    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    assert rows[0].work_ref == _WORK_REF  # type: ignore[attr-defined]

    # A second middleware run carrying NO header lands a NULL row -- the
    # prior bind did not survive into this call.
    await _drive_audit_middleware([(b"x-request-id", b"req-2")])
    assert work_ref_var.get() is None
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 2
    assert rows[1].work_ref is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Pure unit: the boundary header extractor
# ---------------------------------------------------------------------------


def test_extract_work_ref_header_parsing() -> None:
    """``_extract_work_ref`` returns the value, strips it, and NULLs the empties."""
    from meho_backplane.audit import _extract_work_ref

    present = {"headers": [(b"meho-work-ref", b"  gh:evoila/meho#7  ")]}
    assert _extract_work_ref(present) == "gh:evoila/meho#7"  # type: ignore[arg-type]

    upper = {"headers": [(b"Meho-Work-Ref", b"gh:evoila/meho#7")]}
    assert _extract_work_ref(upper) == "gh:evoila/meho#7"  # type: ignore[arg-type]

    empty = {"headers": [(b"meho-work-ref", b"   ")]}
    assert _extract_work_ref(empty) is None  # type: ignore[arg-type]

    absent = {"headers": [(b"x-request-id", b"abc")]}
    assert _extract_work_ref(absent) is None  # type: ignore[arg-type]
