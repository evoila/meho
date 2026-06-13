# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G4.3-T5 usage telemetry surface.

Coverage matrix (issue #444 acceptance criteria):

* **``parse_since``** — relative (``30d`` / ``24h``), absolute
  ISO-8601, naive-UTC normalisation, rejection of malformed input.
* **Service-level aggregation** (``compute_usage``) — happy path,
  surface filter, tenant boundary, distinct-operator counting, empty
  audit_log → zero report.
* **Conversion window** — inclusive 5-minute boundary, exclusion at
  5min+1s, search-only (no follow-up) → 0% conversion.
* **HTTP route RBAC** — operator + ``tenant_filter`` → 403,
  ``tenant_admin`` + ``tenant_filter`` → 200, ``read_only`` → 403
  (gated below the operator floor).
* **HTTP route validation** — malformed ``since`` → 400 with the
  parser-friendly detail string.
* **Audit + broadcast contract** — the audit_log row's payload
  carries ``op_id="meho.retrieval.usage"`` +
  ``op_class="audit_query"`` + ``row_count=<total_searches>`` +
  the enrichment fields (``surfaces`` / ``since`` / ``tenant_scope``).

The service-level tests seed audit_log directly via
:class:`AsyncSession` so the aggregation logic is unit-testable
without a live HTTP request. The route tests use the same TestClient
+ production middleware stack the chassis ``test_api_v1_retrieve``
pattern established.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import respx
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.retrieve_usage import router as usage_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.retrieval.usage import (
    CONVERSION_WINDOW,
    COUNTED_SEARCH_SURFACES,
    MCP_TOOL_PATH_PREFIX,
    REST_RETRIEVE_EXCLUDED,
    SEARCH_OPS,
    SUPPORTED_SURFACES,
    DailyUsageBucket,
    SinceValueError,
    UsageReport,
    compute_usage,
    parse_since,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Settings + JWKS cache fixtures (mirrors test_api_v1_retrieve.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads, around every test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    """Empty the module-level JWKS cache around every test."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture
def log_buffer() -> Iterator[io.StringIO]:
    """Capture structlog output into a private buffer."""
    buf = io.StringIO()
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )
    yield buf
    structlog.reset_defaults()


def _read_log_lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Test app construction
# ---------------------------------------------------------------------------


def _build_app_with_usage_route() -> FastAPI:
    """Return a :class:`FastAPI` with the usage route + audit middleware.

    Mirrors :func:`tests.test_api_v1_retrieve._build_app_with_retrieve_route`:
    full production middleware stack so the audit-payload tests see
    the same contextvar flow production uses.
    """
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(usage_router)
    return app


@pytest.fixture
def usage_client(log_buffer: io.StringIO) -> Iterator[TestClient]:
    """``TestClient`` driving a fresh app with log capture pre-bound."""
    yield TestClient(_build_app_with_usage_route())


# ---------------------------------------------------------------------------
# Audit-row seeding helpers
# ---------------------------------------------------------------------------


_FIXED_TENANT = UUID("00000000-0000-0000-0000-00000000a0a0")
_OTHER_TENANT = UUID("00000000-0000-0000-0000-00000000b0b0")


async def _seed_audit_row(
    *,
    operator_sub: str,
    tenant_id: UUID | None,
    path: str,
    occurred_at: datetime,
    status_code: int = 200,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Insert one ``audit_log`` row directly and return its UUID."""
    row_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=row_id,
                occurred_at=occurred_at,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                method="MCP",
                path=path,
                status_code=status_code,
                request_id=None,
                duration_ms=Decimal("1.00"),
                payload=payload or {},
            ),
        )
        await session.commit()
    return row_id


async def _read_audit_rows_for_path(path: str) -> list[AuditLog]:
    """Return every audit_log row whose ``path`` matches *path*."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == path))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# parse_since
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)


def test_parse_since_relative_days() -> None:
    """``30d`` resolves to ``now - 30 days``."""
    assert parse_since("30d", now=_NOW) == _NOW - timedelta(days=30)


def test_parse_since_relative_hours() -> None:
    """``24h`` resolves to ``now - 24 hours``."""
    assert parse_since("24h", now=_NOW) == _NOW - timedelta(hours=24)


def test_parse_since_iso_date_attaches_utc() -> None:
    """ISO date without time is interpreted as UTC midnight."""
    result = parse_since("2026-04-01", now=_NOW)
    assert result == datetime(2026, 4, 1, 0, 0, tzinfo=UTC)


def test_parse_since_iso_datetime_with_offset_preserved() -> None:
    """ISO datetime with offset → timezone-aware, offset preserved."""
    result = parse_since("2026-04-01T12:00:00+00:00", now=_NOW)
    assert result == datetime(2026, 4, 1, 12, 0, tzinfo=UTC)


def test_parse_since_rejects_empty() -> None:
    """Empty string raises :class:`SinceValueError`."""
    with pytest.raises(SinceValueError):
        parse_since("", now=_NOW)


def test_parse_since_rejects_garbage() -> None:
    """Unknown grammar raises :class:`SinceValueError`."""
    with pytest.raises(SinceValueError):
        parse_since("tomorrow", now=_NOW)


def test_parse_since_rejects_oversized_relative() -> None:
    """The relative grammar caps at 4 digits."""
    with pytest.raises(SinceValueError):
        parse_since("99999d", now=_NOW)


# ---------------------------------------------------------------------------
# compute_usage — empty / shape baselines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_usage_empty_audit_log_returns_zero_report() -> None:
    """Empty audit_log → empty buckets + ``total_searches=0``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=30),
            until=_NOW,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=_FIXED_TENANT,
        )
    assert isinstance(report, UsageReport)
    assert report.buckets == []
    assert report.total_searches == 0
    assert report.tenant_id == _FIXED_TENANT
    assert list(report.surfaces) == list(SUPPORTED_SURFACES)


@pytest.mark.asyncio
async def test_compute_usage_no_matching_surface_returns_empty() -> None:
    """No surfaces in scope → empty report without issuing ``IN ()``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=30),
            until=_NOW,
            surfaces=[],
            tenant_id=_FIXED_TENANT,
        )
    assert report.buckets == []
    assert report.total_searches == 0


# ---------------------------------------------------------------------------
# Counted-surface de-silencing (#632) — the REST-excluded zero is no
# longer context-free.
# ---------------------------------------------------------------------------


def test_counted_search_surfaces_derived_from_search_ops() -> None:
    """``COUNTED_SEARCH_SURFACES`` mirrors ``SEARCH_OPS`` (single source).

    The documented contract cannot drift from the audit_log query
    filter: every counted surface label is exactly ``mcp:<tool>`` for a
    tool in ``SEARCH_OPS``.
    """
    assert tuple(f"mcp:{op}" for op in SEARCH_OPS) == COUNTED_SEARCH_SURFACES
    assert REST_RETRIEVE_EXCLUDED is True


@pytest.mark.asyncio
async def test_usage_report_carries_counted_surface_signal() -> None:
    """Every ``UsageReport`` carries ``counted_surfaces`` + ``rest_excluded``.

    Asserted on the empty (zero-row) report specifically: the field is
    present *alongside* ``total_searches=0`` so the zero is
    self-explaining.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=30),
            until=_NOW,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=_FIXED_TENANT,
        )
    assert report.total_searches == 0
    assert report.counted_surfaces == list(COUNTED_SEARCH_SURFACES)
    assert report.counted_surfaces == [
        "mcp:search_knowledge",
        "mcp:search_memory",
        "mcp:search_operations",
    ]
    assert report.rest_excluded is True


@pytest.mark.asyncio
async def test_rest_only_dogfood_zero_is_not_context_free(
    usage_client: TestClient,
) -> None:
    """REST-only dogfood scenario: the silent zero now self-explains.

    Reproduces the #632 trap end-to-end over the HTTP surface: an
    operator who has only ever called ``POST /api/v1/retrieve`` (never
    an audited MCP ``search_knowledge``) queries ``retrieve/usage``.
    Two ``/api/v1/retrieve``-pathed audit rows stand in for a month of
    REST-only dogfooding. They are NOT on a counted MCP path, so
    ``total_searches`` stays ``0`` — but the response now states which
    surfaces *do* count and that REST is excluded, so the zero reads as
    "REST is not counted", not "no usage".
    """
    await _seed_audit_row(
        operator_sub="op-rest",
        tenant_id=_FIXED_TENANT,
        path="/api/v1/retrieve",
        occurred_at=_NOW - timedelta(days=10),
    )
    await _seed_audit_row(
        operator_sub="op-rest",
        tenant_id=_FIXED_TENANT,
        path="/api/v1/retrieve",
        occurred_at=_NOW - timedelta(days=2),
    )

    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-rest", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = usage_client.get(
            "/api/v1/retrieve/usage?surface=kb",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    body = response.json()
    # The trap: REST /retrieve calls never tick the counter.
    assert body["total_searches"] == 0
    assert body["buckets"] == []
    # The de-silencing: the zero is no longer context-free.
    assert body["counted_surfaces"] == list(COUNTED_SEARCH_SURFACES)
    assert body["rest_excluded"] is True


# ---------------------------------------------------------------------------
# compute_usage — happy path + grouping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_usage_groups_by_day_and_surface() -> None:
    """Two searches on the same day → one bucket with ``search_count=2``."""
    base = _NOW - timedelta(hours=2)
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base,
    )
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base + timedelta(minutes=10),
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=1),
            until=_NOW,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=_FIXED_TENANT,
        )

    assert len(report.buckets) == 1
    bucket = report.buckets[0]
    assert bucket.surface == "kb"
    assert bucket.search_count == 2
    assert bucket.distinct_operators == 1
    assert report.total_searches == 2


@pytest.mark.asyncio
async def test_compute_usage_counts_distinct_operators() -> None:
    """Two operators each search once → ``distinct_operators=2``."""
    base = _NOW - timedelta(hours=2)
    for op in ("op-A", "op-B"):
        await _seed_audit_row(
            operator_sub=op,
            tenant_id=_FIXED_TENANT,
            path=f"{MCP_TOOL_PATH_PREFIX}search_operations",
            occurred_at=base,
        )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=1),
            until=_NOW,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=_FIXED_TENANT,
        )

    assert len(report.buckets) == 1
    assert report.buckets[0].surface == "operations"
    assert report.buckets[0].distinct_operators == 2
    assert report.buckets[0].search_count == 2


@pytest.mark.asyncio
async def test_compute_usage_surface_filter_narrows_results() -> None:
    """``surfaces=['kb']`` excludes operations + memory rows."""
    base = _NOW - timedelta(hours=2)
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base,
    )
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_operations",
        occurred_at=base,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=1),
            until=_NOW,
            surfaces=["kb"],
            tenant_id=_FIXED_TENANT,
        )

    assert {b.surface for b in report.buckets} == {"kb"}
    assert report.total_searches == 1


@pytest.mark.asyncio
async def test_compute_usage_skips_non_200_search_rows() -> None:
    """A 4xx/5xx search is not a 'daily use' signal."""
    base = _NOW - timedelta(hours=2)
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base,
        status_code=500,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=1),
            until=_NOW,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=_FIXED_TENANT,
        )

    assert report.buckets == []
    assert report.total_searches == 0


# ---------------------------------------------------------------------------
# compute_usage — tenant boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_usage_tenant_filter_excludes_other_tenants() -> None:
    """A search by another tenant does not appear in this tenant's report."""
    base = _NOW - timedelta(hours=2)
    await _seed_audit_row(
        operator_sub="op-other",
        tenant_id=_OTHER_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base,
    )
    await _seed_audit_row(
        operator_sub="op-own",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=1),
            until=_NOW,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=_FIXED_TENANT,
        )

    assert report.total_searches == 1
    assert report.buckets[0].distinct_operators == 1


@pytest.mark.asyncio
async def test_compute_usage_cross_tenant_view_sees_both() -> None:
    """``tenant_id=None`` aggregates across every tenant."""
    base = _NOW - timedelta(hours=2)
    await _seed_audit_row(
        operator_sub="op-A",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base,
    )
    await _seed_audit_row(
        operator_sub="op-B",
        tenant_id=_OTHER_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=1),
            until=_NOW,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=None,
        )

    assert report.total_searches == 2
    assert report.buckets[0].distinct_operators == 2


# ---------------------------------------------------------------------------
# compute_usage — search-to-action conversion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversion_inside_window_counts() -> None:
    """A follow-up action 4 minutes after the search → 100% conversion."""
    base = _NOW - timedelta(hours=2)
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base,
    )
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path="/mcp/resources/read/meho://kb/some-slug",
        occurred_at=base + timedelta(minutes=4),
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=1),
            until=_NOW,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=_FIXED_TENANT,
        )

    assert report.buckets[0].action_conversion_pct == 100.0


@pytest.mark.asyncio
async def test_conversion_at_window_boundary_inclusive() -> None:
    """An action at exactly ``CONVERSION_WINDOW`` after the search counts."""
    base = _NOW - timedelta(hours=2)
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_memory",
        occurred_at=base,
    )
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path="/api/v1/something-else",
        occurred_at=base + CONVERSION_WINDOW,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=1),
            until=_NOW,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=_FIXED_TENANT,
        )

    assert report.buckets[0].action_conversion_pct == 100.0


@pytest.mark.asyncio
async def test_conversion_outside_window_excluded() -> None:
    """An action ``CONVERSION_WINDOW + 1s`` after the search does not count."""
    base = _NOW - timedelta(hours=2)
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base,
    )
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path="/api/v1/something-else",
        occurred_at=base + CONVERSION_WINDOW + timedelta(seconds=1),
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=1),
            until=_NOW,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=_FIXED_TENANT,
        )

    assert report.buckets[0].action_conversion_pct == 0.0


@pytest.mark.asyncio
async def test_conversion_search_only_no_followup() -> None:
    """A search with no subsequent activity → 0% conversion."""
    base = _NOW - timedelta(hours=2)
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=1),
            until=_NOW,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=_FIXED_TENANT,
        )

    assert report.buckets[0].action_conversion_pct == 0.0


@pytest.mark.asyncio
async def test_conversion_partial_mixed_window() -> None:
    """Two searches: one converts, one doesn't → 50% conversion."""
    base = _NOW - timedelta(hours=2)
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base,
    )
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path="/api/v1/follow-up-1",
        occurred_at=base + timedelta(minutes=2),
    )
    # Second search 30 minutes later — well past the first follow-up's
    # window so the follow-up cannot bleed into this search.
    second_search = base + timedelta(minutes=30)
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=second_search,
    )
    # No follow-up for the second search.

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=1),
            until=_NOW,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=_FIXED_TENANT,
        )

    assert report.buckets[0].search_count == 2
    assert report.buckets[0].action_conversion_pct == 50.0


@pytest.mark.asyncio
async def test_conversion_cross_operator_does_not_count() -> None:
    """An action by a different operator never converts the search."""
    base = _NOW - timedelta(hours=2)
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base,
    )
    await _seed_audit_row(
        operator_sub="op-2",  # different operator
        tenant_id=_FIXED_TENANT,
        path="/api/v1/something",
        occurred_at=base + timedelta(minutes=1),
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        report = await compute_usage(
            session=session,
            since=_NOW - timedelta(days=1),
            until=_NOW,
            surfaces=SUPPORTED_SURFACES,
            tenant_id=_FIXED_TENANT,
        )

    assert report.buckets[0].action_conversion_pct == 0.0


# ---------------------------------------------------------------------------
# DailyUsageBucket / UsageReport — Pydantic shape regression
# ---------------------------------------------------------------------------


def test_daily_usage_bucket_is_frozen() -> None:
    """The Pydantic model rejects mutation post-construction."""
    from pydantic import ValidationError

    bucket = DailyUsageBucket(
        date=datetime(2026, 5, 1, tzinfo=UTC).date(),
        surface="kb",
        search_count=1,
        distinct_operators=1,
        action_conversion_pct=42.5,
    )
    with pytest.raises(ValidationError):
        bucket.search_count = 99  # type: ignore[misc]


def test_usage_report_serialises_to_stable_json() -> None:
    """``UsageReport.model_dump_json`` is stable across runs.

    Regression-locks the field set the CLI's ``--json`` consumer
    (T6 retire-checklist) deserialises against. Adding a field is
    a breaking change to consumers; this test surfaces it.
    """
    report = UsageReport(
        since=datetime(2026, 4, 1, tzinfo=UTC),
        until=datetime(2026, 5, 1, tzinfo=UTC),
        surfaces=["kb"],
        tenant_id=_FIXED_TENANT,
        buckets=[
            DailyUsageBucket(
                date=datetime(2026, 4, 1, tzinfo=UTC).date(),
                surface="kb",
                search_count=2,
                distinct_operators=1,
                action_conversion_pct=50.0,
            ),
        ],
        total_searches=2,
    )
    blob = json.loads(report.model_dump_json())
    assert set(blob.keys()) == {
        "since",
        "until",
        "surfaces",
        "tenant_id",
        "buckets",
        "total_searches",
        "counted_surfaces",
        "rest_excluded",
    }
    # #632: the counted-surface signal ships defaulted from the single
    # source of truth and is part of the CLI-consumer contract.
    assert blob["counted_surfaces"] == list(COUNTED_SEARCH_SURFACES)
    assert blob["rest_excluded"] is True
    assert set(blob["buckets"][0].keys()) == {
        "date",
        "surface",
        "search_count",
        "distinct_operators",
        "action_conversion_pct",
    }


# ---------------------------------------------------------------------------
# HTTP route — RBAC
# ---------------------------------------------------------------------------


def test_route_unauthenticated_returns_401(usage_client: TestClient) -> None:
    """No Authorization header → 401."""
    response = usage_client.get("/api/v1/retrieve/usage")
    assert response.status_code == 401


def test_route_read_only_returns_403(
    usage_client: TestClient,
    log_buffer: io.StringIO,
) -> None:
    """``read_only`` is below the operator floor → 403."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-ro", tenant_role=TenantRole.READ_ONLY.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = usage_client.get(
            "/api/v1/retrieve/usage",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}


def test_route_operator_with_tenant_filter_returns_403(
    usage_client: TestClient,
) -> None:
    """``operator`` role + non-null ``tenant_filter`` → 403.

    Cross-tenant inspection is reserved for ``tenant_admin``. The
    403 carries the route-specific detail token so operators can
    grep their CLI output for the exact contract that failed.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-cross", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = usage_client.get(
            f"/api/v1/retrieve/usage?tenant_filter={_OTHER_TENANT}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "cross_tenant_requires_platform_admin"}


def test_route_platform_admin_with_tenant_filter_returns_200(
    usage_client: TestClient,
) -> None:
    """A ``platform_admin`` + ``tenant_filter`` → 200 with the filtered scope.

    Cross-tenant access requires the platform-admin capability (#1638); a
    plain ``tenant_admin`` is now denied (see the operator-403 sibling test).
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-admin",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        platform_admin=True,
    )
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = usage_client.get(
            f"/api/v1/retrieve/usage?tenant_filter={_OTHER_TENANT}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == str(_OTHER_TENANT)


def test_route_operator_default_returns_200_with_empty_buckets(
    usage_client: TestClient,
) -> None:
    """Default request with no audit_log rows → 200, empty buckets."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = usage_client.get(
            "/api/v1/retrieve/usage",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["buckets"] == []
    assert body["total_searches"] == 0
    assert body["surfaces"] == list(SUPPORTED_SURFACES)


# ---------------------------------------------------------------------------
# HTTP route — Validation
# ---------------------------------------------------------------------------


def test_route_malformed_since_returns_400(usage_client: TestClient) -> None:
    """``since=tomorrow`` is unparseable → 400 with parser detail."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = usage_client.get(
            "/api/v1/retrieve/usage?since=tomorrow",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 400
    assert "tomorrow" in response.json()["detail"]


def test_route_unknown_surface_returns_422(usage_client: TestClient) -> None:
    """``surface=invalid`` fails Pydantic ``Literal`` validation."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = usage_client.get(
            "/api/v1/retrieve/usage?surface=bogus",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# HTTP route — Audit + broadcast contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_audit_payload_carries_canonical_op_id(
    usage_client: TestClient,
) -> None:
    """The audit_log row's payload pins the canonical ``op_id`` + ``op_class``.

    Load-bearing contract for the broadcast event's aggregate-only
    posture (decision #3). The route binds ``audit_op_id`` +
    ``audit_op_class`` contextvars; the chassis audit middleware lifts
    them into the audit_log row's payload (stripped ``audit_`` prefix);
    the broadcast publisher re-reads them as the override op_id and
    op_class for the BroadcastEvent. The test verifies the audit_log
    side of that contract.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-audit",
        tenant_role=TenantRole.OPERATOR.value,
    )
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = usage_client.get(
            "/api/v1/retrieve/usage?since=7d&surface=kb",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200

    rows = await _read_audit_rows_for_path("/api/v1/retrieve/usage")
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "meho.retrieval.usage"
    assert payload["op_class"] == "audit_query"
    assert payload["surfaces"] == "kb"
    assert payload["since"] == "7d"
    assert payload["tenant_scope"] == "self"
    # ``row_count`` is bound after compute_usage; empty audit_log →
    # ``total_searches=0`` → ``row_count=0``.
    assert payload["row_count"] == 0


@pytest.mark.asyncio
async def test_route_audit_tenant_scope_other_for_cross_tenant_admin(
    usage_client: TestClient,
) -> None:
    """``platform_admin`` + cross-tenant filter → ``tenant_scope="other"``.

    Lets G8 audit-trail queries distinguish operators inspecting
    their own tenant from platform-admins genuinely cross-cutting.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-admin",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        platform_admin=True,
    )
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = usage_client.get(
            f"/api/v1/retrieve/usage?tenant_filter={_OTHER_TENANT}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200

    rows = await _read_audit_rows_for_path("/api/v1/retrieve/usage")
    assert len(rows) == 1
    assert rows[0].payload["tenant_scope"] == "other"


@pytest.mark.asyncio
async def test_route_audit_row_count_matches_total_searches(
    usage_client: TestClient,
) -> None:
    """``audit_row_count`` reflects the aggregate cardinality, not the raw scan.

    Seed a known search-count, hit the route, read back the audit row,
    assert the row's ``payload['row_count']`` equals the response body's
    ``total_searches``.

    The seed timestamps are anchored to ``datetime.now(UTC)`` rather than
    the module-level fixed ``_NOW`` constant. This route test (unlike the
    service-level :func:`compute_usage` tests above, which pass an explicit
    ``since``/``until`` window pinned to ``_NOW``) drives the HTTP endpoint
    with its **default** ``since`` of ``30d``, which the route resolves
    relative to the real wall clock (``datetime.now(UTC)``). Seeding at a
    fixed past date made the rows fall out of that rolling window once the
    calendar advanced ~30 days past ``_NOW``, turning the assertion into a
    time-bomb (the rows were committed and visible at the raw-SQL level but
    excluded by the window filter, so ``total_searches`` dropped to 0).
    Anchoring the seed to the same clock the route reads keeps both rows
    inside the default window on every run.
    """
    base = datetime.now(UTC) - timedelta(hours=2)
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base,
    )
    await _seed_audit_row(
        operator_sub="op-1",
        tenant_id=_FIXED_TENANT,
        path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
        occurred_at=base + timedelta(minutes=15),
    )

    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-1",
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_FIXED_TENANT),
    )
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = usage_client.get(
            "/api/v1/retrieve/usage",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["total_searches"] == 2

    rows = await _read_audit_rows_for_path("/api/v1/retrieve/usage")
    assert len(rows) == 1
    assert rows[0].payload["row_count"] == 2
