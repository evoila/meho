# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for privacy-bounded request usage events (#3734).

Coverage, by acceptance criterion:

* **One event per boundary** -- a REST request and an MCP call each write
  exactly one ``request_finished`` event; a nested boundary (a second
  :class:`AuditMiddleware` layer, or a second ``open_request_usage``)
  writes nothing extra.
* **Schema rejects prohibited content and unknown routes** -- bodies,
  tokens, queries, arguments, prompts, target names and adherence claims
  are refused; routes outside the catalog are refused; a concrete path
  is never stored.
* **Context selection is optional, never an adherence claim** -- the
  ``context_selected`` event carries only a correlation id.
* **Retention** -- unset retention writes nothing and never blocks; the
  prune job deletes only expired events; aggregate reads enforce the
  tenant boundary.
* **Dual write** -- the wrapper ``audit_log`` row is still written.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app as production_app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations.ingest.boot_stamp import BOOT_STAMP_OPERATOR_SUB
from meho_backplane.request_usage import collector as collector_module
from meho_backplane.request_usage.collector import (
    MAX_CONTEXT_SELECTIONS,
    note_context_selected,
    open_request_usage,
)
from meho_backplane.request_usage.reader import summarize_request_usage
from meho_backplane.request_usage.retention import prune_request_usage_once
from meho_backplane.request_usage.schema import (
    CATALOG_CONTEXT_KEY,
    EVENT_KINDS,
    OUTCOMES,
    TRANSPORTS,
    RequestUsageEventData,
    RouteCatalog,
    outcome_for_status,
)
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

from ._request_usage_helpers import read_usage_events, seed_usage_event

_TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000b0b0")
_CORRELATION = uuid.UUID("11111111-2222-3333-4444-555555555555")
_THING_ROUTE = "/api/v1/usage-probe/things/{thing_id}"
_CATALOG = RouteCatalog(
    rest=frozenset({_THING_ROUTE}),
    mcp=frozenset({"tool:search_knowledge", "method:tools/call"}),
)


@pytest.fixture
def usage_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turn request usage collection on (30-day retention)."""
    monkeypatch.setenv("REQUEST_USAGE_RETENTION_DAYS", "30")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _event(**overrides: Any) -> dict[str, Any]:
    """A valid ``request_finished`` event payload, with *overrides*."""
    data: dict[str, Any] = {
        "id": uuid.uuid4(),
        "kind": "request_finished",
        "occurred_at": datetime.now(UTC),
        "tenant_id": _TENANT_A,
        "principal_sub": "op-1",
        "transport": "rest",
        "route": _THING_ROUTE,
        "outcome": "ok",
        "duration_ms": 1.5,
    }
    data.update(overrides)
    return data


def _validate(data: dict[str, Any]) -> RequestUsageEventData:
    return RequestUsageEventData.model_validate(data, context={CATALOG_CONTEXT_KEY: _CATALOG})


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_accepts_a_bounded_event() -> None:
    event = _validate(_event())
    assert event.route == _THING_ROUTE
    assert event.outcome == "ok"


@pytest.mark.parametrize(
    "field",
    [
        "body",
        "request_body",
        "query",
        "query_string",
        "params",
        "arguments",
        "prompt",
        "target_name",
        "token",
        "authorization",
        "jwt",
        "detail",
        "adhered",
        "followed",
    ],
)
def test_schema_rejects_prohibited_content(field: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _validate(_event(**{field: "anything"}))
    assert excinfo.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    ("transport", "route"),
    [
        ("rest", "/api/v1/usage-probe/things/prod-db"),
        ("rest", "/api/v1/usage-probe/things/{thing_id}?q=secret"),
        ("mcp", "tool:not_registered"),
        ("mcp", _THING_ROUTE),
        ("rest", "tool:search_knowledge"),
    ],
)
def test_schema_rejects_unknown_route(transport: str, route: str) -> None:
    with pytest.raises(ValidationError, match="not a known route or tool"):
        _validate(_event(transport=transport, route=route))


def test_schema_needs_a_route_catalog() -> None:
    with pytest.raises(ValidationError, match="no route catalog"):
        RequestUsageEventData.model_validate(_event())


@pytest.mark.parametrize(
    "principal",
    [
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl",
        "Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.sig",
        "op 1",
    ],
)
def test_schema_rejects_non_opaque_principal(principal: str) -> None:
    with pytest.raises(ValidationError, match="principal_sub"):
        _validate(_event(principal_sub=principal))


def test_schema_rejects_unknown_outcome_code() -> None:
    with pytest.raises(ValidationError):
        _validate(_event(outcome="forbidden: role X is missing"))


def test_context_selected_carries_only_a_correlation_id() -> None:
    event = _validate(
        _event(
            kind="context_selected", correlation_id=_CORRELATION, outcome=None, duration_ms=None
        ),
    )
    assert event.correlation_id == _CORRELATION
    assert event.outcome is None
    with pytest.raises(ValidationError, match="needs a correlation_id"):
        _validate(_event(kind="context_selected", outcome=None, duration_ms=None))
    with pytest.raises(ValidationError, match="no outcome or duration"):
        _validate(_event(kind="context_selected", correlation_id=_CORRELATION))
    with pytest.raises(ValidationError, match="no correlation_id"):
        _validate(_event(correlation_id=_CORRELATION))


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, "ok"),
        (204, "ok"),
        (401, "denied"),
        (403, "denied"),
        (404, "not_found"),
        (422, "client_error"),
        (429, "rate_limited"),
        (500, "server_error"),
        (0, "server_error"),
    ],
)
def test_outcome_codes_are_bounded(status: int, outcome: str) -> None:
    assert outcome_for_status(status) == outcome


def test_migration_value_sets_match_schema() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0106_create_request_usage_event.py"
    )
    spec = importlib.util.spec_from_file_location("_migration_0106", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._KINDS == EVENT_KINDS
    assert module._TRANSPORTS == TRANSPORTS
    assert module._OUTCOMES == OUTCOMES


# ---------------------------------------------------------------------------
# Collector boundary
# ---------------------------------------------------------------------------


def test_collection_is_off_without_retention() -> None:
    assert get_settings().request_usage_retention_days is None
    assert open_request_usage() is None
    assert note_context_selected() is None


@pytest.mark.usefixtures("usage_on")
async def test_nested_boundary_cannot_open() -> None:
    outer = open_request_usage()
    assert outer is not None
    assert open_request_usage() is None
    await outer.close({"type": "http", "path": "/x"})
    reopened = open_request_usage()
    assert reopened is not None
    await reopened.close({"type": "http", "path": "/x"})


@pytest.mark.usefixtures("usage_on")
async def test_context_selections_are_capped() -> None:
    boundary = open_request_usage()
    assert boundary is not None
    picked = [note_context_selected() for _ in range(MAX_CONTEXT_SELECTIONS + 2)]
    assert all(item is not None for item in picked[:MAX_CONTEXT_SELECTIONS])
    assert picked[MAX_CONTEXT_SELECTIONS:] == [None, None]
    await boundary.close({"type": "http", "path": "/x"})


# ---------------------------------------------------------------------------
# REST boundary (AuditMiddleware)
# ---------------------------------------------------------------------------


async def _bind_principal() -> None:
    """Stand-in for ``verify_jwt_and_bind``: bind the principal contextvars."""
    structlog.contextvars.bind_contextvars(operator_sub="op-usage", tenant_id=str(_TENANT_A))


_probe = APIRouter(prefix="/api/v1/usage-probe")


@_probe.get("/things/{thing_id}", dependencies=[Depends(_bind_principal)])
async def _get_thing(thing_id: str) -> dict[str, str]:
    return {"thing": thing_id}


@_probe.get("/forbidden", dependencies=[Depends(_bind_principal)])
async def _forbidden() -> dict[str, str]:
    raise HTTPException(status_code=403, detail="needs role tenant_admin on prod-db")


@_probe.get("/context", dependencies=[Depends(_bind_principal)])
async def _context() -> dict[str, str]:
    note_context_selected(_CORRELATION)
    return {}


@_probe.get("/public")
async def _public() -> dict[str, str]:
    return {}


def _probe_client(*, nested: bool = False) -> TestClient:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    if nested:
        app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(_probe)
    return TestClient(app)


async def _audit_rows_for(path: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == path))
        return list(result.scalars().all())


@pytest.mark.usefixtures("usage_on")
async def test_rest_request_writes_one_bounded_event_and_keeps_audit_row() -> None:
    response = _probe_client().get("/api/v1/usage-probe/things/prod-db?q=secret-value")
    assert response.status_code == 200

    events = await read_usage_events()
    assert len(events) == 1
    event = events[0]
    assert event.kind == "request_finished"
    assert event.transport == "rest"
    assert event.route == _THING_ROUTE
    assert event.outcome == "ok"
    assert event.tenant_id == _TENANT_A
    assert event.principal_sub == "op-usage"
    assert event.duration_ms is not None
    stored = " ".join(str(getattr(event, column.key)) for column in event.__table__.columns)
    assert "prod-db" not in stored
    assert "secret-value" not in stored

    # Dual write: the wrapper audit row is still there.
    assert len(await _audit_rows_for("/api/v1/usage-probe/things/prod-db")) == 1


@pytest.mark.usefixtures("usage_on")
async def test_nested_middleware_writes_one_event() -> None:
    response = _probe_client(nested=True).get("/api/v1/usage-probe/things/a")
    assert response.status_code == 200
    assert len(await read_usage_events()) == 1


@pytest.mark.usefixtures("usage_on")
async def test_authorization_failure_is_only_an_outcome_code() -> None:
    response = _probe_client().get("/api/v1/usage-probe/forbidden")
    assert response.status_code == 403
    [event] = await read_usage_events()
    assert event.outcome == "denied"
    assert event.route == "/api/v1/usage-probe/forbidden"


@pytest.mark.usefixtures("usage_on")
async def test_context_selected_event_is_optional_and_correlated() -> None:
    _probe_client().get("/api/v1/usage-probe/things/a")
    _probe_client().get("/api/v1/usage-probe/context")
    events = await read_usage_events()
    assert sorted(event.kind for event in events) == [
        "context_selected",
        "request_finished",
        "request_finished",
    ]
    [selected] = [event for event in events if event.kind == "context_selected"]
    assert selected.correlation_id == _CORRELATION
    assert selected.outcome is None
    assert selected.duration_ms is None


@pytest.mark.usefixtures("usage_on")
async def test_unauthenticated_request_writes_no_event() -> None:
    assert _probe_client().get("/api/v1/usage-probe/public").status_code == 200
    assert await read_usage_events() == []


async def test_missing_retention_writes_nothing_and_does_not_block() -> None:
    response = _probe_client().get("/api/v1/usage-probe/things/a")
    assert response.status_code == 200
    assert await read_usage_events() == []
    assert len(await _audit_rows_for("/api/v1/usage-probe/things/a")) == 1


@pytest.mark.usefixtures("usage_on")
async def test_usage_write_failure_never_blocks_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken_sessionmaker() -> Any:
        raise RuntimeError("usage store down")

    monkeypatch.setattr(collector_module, "get_sessionmaker", _broken_sessionmaker)
    response = _probe_client().get("/api/v1/usage-probe/things/a")
    assert response.status_code == 200
    assert len(await _audit_rows_for("/api/v1/usage-probe/things/a")) == 1


# ---------------------------------------------------------------------------
# MCP boundary
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_client(usage_on: None) -> Iterator[tuple[TestClient, Operator]]:
    """Production app with an MCP operator that binds the principal contextvars."""
    op = build_operator(TenantRole.OPERATOR)

    async def _fake_verify() -> Operator:
        structlog.contextvars.bind_contextvars(
            operator_sub=op.sub,
            tenant_id=str(op.tenant_id),
        )
        return op

    production_app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(production_app) as client:
            yield client, op
    finally:
        production_app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


async def test_mcp_tool_call_writes_one_event_labelled_with_the_tool(
    mcp_client: tuple[TestClient, Operator],
) -> None:
    client, op = mcp_client
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho_status", "arguments": {}},
        },
    )
    assert response.status_code == 200

    events = [e for e in await read_usage_events() if e.transport == "mcp"]
    assert len(events) == 1
    assert events[0].route == "tool:meho_status"
    assert events[0].outcome == "ok"
    assert events[0].tenant_id == OPERATOR_TENANT_ID
    assert events[0].principal_sub == op.sub

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.method == "MCP")
                    .where(AuditLog.operator_sub != BOOT_STAMP_OPERATOR_SUB),
                )
            )
            .scalars()
            .all()
        )
    assert [row.path for row in rows] == ["/mcp/tools/call/meho_status"]


async def test_mcp_unknown_tool_keeps_only_the_method_label(
    mcp_client: tuple[TestClient, Operator],
) -> None:
    client, _op = mcp_client
    post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "prod-db.secret.tool", "arguments": {}},
        },
    )
    [event] = [e for e in await read_usage_events() if e.transport == "mcp"]
    assert event.route == "method:tools/call"
    assert event.outcome == "not_found"


# ---------------------------------------------------------------------------
# Retention + tenant-authorized reads
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("usage_on")
async def test_prune_deletes_only_expired_events() -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    await seed_usage_event(
        tenant_id=_TENANT_A, route="tool:a", occurred_at=now - timedelta(days=31)
    )
    await seed_usage_event(tenant_id=_TENANT_A, route="tool:b", occurred_at=now - timedelta(days=1))

    assert await prune_request_usage_once(now=now) == 1
    assert [event.route for event in await read_usage_events()] == ["tool:b"]


async def test_prune_does_nothing_without_retention() -> None:
    await seed_usage_event(
        tenant_id=_TENANT_A,
        route="tool:a",
        occurred_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    assert await prune_request_usage_once() is None
    assert len(await read_usage_events()) == 1


async def _seed_two_tenants() -> tuple[datetime, datetime]:
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    await seed_usage_event(tenant_id=_TENANT_A, route="tool:x", occurred_at=now, principal_sub="a1")
    await seed_usage_event(tenant_id=_TENANT_A, route="tool:x", occurred_at=now, principal_sub="a2")
    await seed_usage_event(
        tenant_id=_TENANT_A,
        route="tool:x",
        occurred_at=now,
        principal_sub="a1",
        outcome="denied",
    )
    await seed_usage_event(tenant_id=_TENANT_B, route="tool:y", occurred_at=now)
    return now - timedelta(hours=1), now + timedelta(hours=1)


async def test_summary_reads_only_the_callers_tenant() -> None:
    since, until = await _seed_two_tenants()
    async with get_sessionmaker()() as session:
        summary = await summarize_request_usage(
            session,
            operator=build_operator(TenantRole.OPERATOR),
            tenant_filter=None,
            since=since,
            until=until,
        )
    assert summary.tenant_id == _TENANT_A
    assert summary.total_requests == 3
    by_outcome = {item.outcome: item for item in summary.routes}
    assert by_outcome["ok"].requests == 2
    assert by_outcome["ok"].distinct_principals == 2
    assert by_outcome["denied"].requests == 1
    assert {item.route for item in summary.routes} == {"tool:x"}


async def test_summary_cross_tenant_needs_platform_admin() -> None:
    since, until = await _seed_two_tenants()
    async with get_sessionmaker()() as session:
        with pytest.raises(HTTPException) as excinfo:
            await summarize_request_usage(
                session,
                operator=build_operator(TenantRole.TENANT_ADMIN),
                tenant_filter=_TENANT_B,
                since=since,
                until=until,
            )
        assert excinfo.value.status_code == 403

        summary = await summarize_request_usage(
            session,
            operator=build_operator(TenantRole.TENANT_ADMIN, platform_admin=True),
            tenant_filter=_TENANT_B,
            since=since,
            until=until,
        )
    assert summary.tenant_id == _TENANT_B
    assert [item.route for item in summary.routes] == ["tool:y"]
