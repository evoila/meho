# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the flight-recorder internal persistence API (#3212, F6/F7).

Covers :func:`meho_backplane.flight_recorder.store.record_trace` with synthetic
spans (this task owns the store; the capture seam owns real span production):

* a header + ordered spans persist with every field, ``seq`` 0..n-1;
* ``expires_at`` is stamped as ``created_at + resolved_retention_days`` (and
  honours a per-tenant retention override);
* ``redaction_uncertain`` round-trips;
* an empty-span trace is a valid header-only trace;
* F7: a write error is swallowed and returns ``None`` (never raises).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DispatchTrace, DispatchTraceSpan, Tenant
from meho_backplane.flight_recorder import store as fr_store
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.flight_recorder.store import SpanInput, record_trace
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    yield
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()


async def _seed_tenant(
    session: AsyncSession,
    *,
    slug: str,
    retention_days: int | None = None,
) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    session.add(
        Tenant(
            id=tenant_id,
            slug=slug,
            name=f"Tenant {slug}",
            flight_recorder_retention_days=retention_days,
        )
    )
    await session.commit()
    return tenant_id


def _synthetic_spans() -> list[SpanInput]:
    started = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
    return [
        SpanInput(
            span_kind="vendor_call",
            name="GET /rest/vm",
            started_at=started,
            duration_ms=Decimal("12.50"),
            status="200",
            attributes={"method": "GET", "url": "https://vendor.example/rest/vm"},
        ),
        SpanInput(
            span_kind="jsonflux_reduction",
            name="jsonflux.reduce",
            started_at=started + timedelta(milliseconds=13),
            attributes={"input_rows": 4000, "kept_fields": ["name", "id"], "handle": "h-1"},
        ),
    ]


async def test_record_trace_persists_header_and_ordered_spans() -> None:
    audit_id = uuid.uuid4()
    created_at = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-store-basic")

    trace_id = await record_trace(
        audit_id=audit_id,
        tenant_id=tenant_id,
        spans=_synthetic_spans(),
        now=created_at,
    )
    assert trace_id is not None

    async with sessionmaker() as session:
        header = (
            await session.execute(select(DispatchTrace).where(DispatchTrace.id == trace_id))
        ).scalar_one()
        spans = list(
            (
                await session.execute(
                    select(DispatchTraceSpan)
                    .where(DispatchTraceSpan.trace_id == trace_id)
                    .order_by(DispatchTraceSpan.seq.asc())
                )
            )
            .scalars()
            .all()
        )

    assert header.audit_id == audit_id
    assert header.tenant_id == tenant_id
    assert header.redaction_uncertain is False
    # Default retention (7d) stamped from created_at (compare naive — SQLite
    # drops tzinfo on round-trip).
    assert header.expires_at.replace(tzinfo=None) == (created_at + timedelta(days=7)).replace(
        tzinfo=None
    )

    assert [s.seq for s in spans] == [0, 1]
    assert spans[0].span_kind == "vendor_call"
    assert spans[0].name == "GET /rest/vm"
    assert spans[0].status == "200"
    assert spans[0].duration_ms == Decimal("12.50")
    assert spans[0].attributes["method"] == "GET"
    assert spans[1].span_kind == "jsonflux_reduction"
    assert spans[1].attributes["handle"] == "h-1"


async def test_record_trace_honours_per_tenant_retention_override() -> None:
    created_at = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-store-lab", retention_days=14)

    trace_id = await record_trace(
        audit_id=uuid.uuid4(),
        tenant_id=tenant_id,
        spans=[],
        now=created_at,
    )
    assert trace_id is not None
    async with sessionmaker() as session:
        header = (
            await session.execute(select(DispatchTrace).where(DispatchTrace.id == trace_id))
        ).scalar_one()
    assert header.expires_at.replace(tzinfo=None) == (created_at + timedelta(days=14)).replace(
        tzinfo=None
    )


async def test_record_trace_empty_spans_is_valid_header_only() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-store-empty")
    trace_id = await record_trace(audit_id=uuid.uuid4(), tenant_id=tenant_id, spans=[])
    assert trace_id is not None
    async with sessionmaker() as session:
        count = len(
            (
                await session.execute(
                    select(DispatchTraceSpan).where(DispatchTraceSpan.trace_id == trace_id)
                )
            )
            .scalars()
            .all()
        )
    assert count == 0


async def test_record_trace_persists_redaction_uncertain_flag() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session, slug="fr-store-uncertain")
    trace_id = await record_trace(
        audit_id=uuid.uuid4(),
        tenant_id=tenant_id,
        spans=[],
        redaction_uncertain=True,
    )
    assert trace_id is not None
    async with sessionmaker() as session:
        header = (
            await session.execute(select(DispatchTrace).where(DispatchTrace.id == trace_id))
        ).scalar_one()
    assert header.redaction_uncertain is True


async def test_record_trace_swallows_errors_and_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F7: a persistence error is best-effort — swallowed, returns ``None``."""

    async def _boom(_tenant_id: uuid.UUID) -> int:
        raise RuntimeError("retention resolution down")

    monkeypatch.setattr(fr_store, "resolve_retention_days", _boom)
    result = await record_trace(
        audit_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        spans=_synthetic_spans(),
    )
    assert result is None
