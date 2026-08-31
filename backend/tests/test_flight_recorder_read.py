# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the flight-recorder tenant-scoped trace read (#3215).

Covers :func:`meho_backplane.flight_recorder.read.load_trace` -- the single
tenant-scoped read the operator REST route and the console pane both share:

* a seeded trace loads its header + ordered spans (``seq`` ascending);
* a missing trace returns ``None`` (distinct from a missing audit row -- the
  caller owns the surface semantics);
* tenant isolation: a trace owned by another tenant is never returned, even for
  the same ``audit_id`` -- the load-bearing defence-in-depth guard.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.flight_recorder import SpanInput, load_trace, record_trace
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.settings import get_settings

_BASE = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)


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


async def _seed_tenant(slug: str) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()
    return tenant_id


def _spans() -> list[SpanInput]:
    return [
        SpanInput(
            span_kind="vendor_call",
            name="GET /rest/vm",
            started_at=_BASE,
            duration_ms=Decimal("12.50"),
            status="200",
            attributes={"method": "GET", "url": "https://vendor.example/rest/vm"},
        ),
        SpanInput(
            span_kind="jsonflux_reduction",
            name="jsonflux.reduce",
            started_at=_BASE + timedelta(milliseconds=13),
            status="ok",
            attributes={"input_rows": 4000, "kept_fields": ["name"], "handle": "h-1"},
        ),
    ]


async def test_load_trace_returns_ordered_spans() -> None:
    tenant_id = await _seed_tenant("fr-read-basic")
    audit_id = uuid.uuid4()
    await record_trace(audit_id=audit_id, tenant_id=tenant_id, spans=_spans(), now=_BASE)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        view = await load_trace(session, audit_id=audit_id, tenant_id=tenant_id)

    assert view is not None
    assert view.audit_id == audit_id
    assert view.tenant_id == tenant_id
    assert view.redaction_uncertain is False
    assert [s.seq for s in view.spans] == [0, 1]
    assert view.spans[0].span_kind == "vendor_call"
    assert view.spans[0].attributes["url"] == "https://vendor.example/rest/vm"
    assert view.spans[1].attributes["handle"] == "h-1"


async def test_load_trace_returns_none_when_absent() -> None:
    tenant_id = await _seed_tenant("fr-read-absent")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        view = await load_trace(session, audit_id=uuid.uuid4(), tenant_id=tenant_id)
    assert view is None


async def test_load_trace_is_tenant_scoped() -> None:
    """A trace owned by tenant B is never returned for tenant A (defence in depth).

    The unique index is on ``audit_id`` alone, so only one trace exists per
    dispatch; this seeds the trace under tenant B and proves that reading with
    tenant A's id -- even for that exact ``audit_id`` -- yields ``None`` rather
    than another tenant's trace.
    """
    tenant_a = await _seed_tenant("fr-read-a")
    tenant_b = await _seed_tenant("fr-read-b")
    audit_id = uuid.uuid4()
    await record_trace(audit_id=audit_id, tenant_id=tenant_b, spans=_spans(), now=_BASE)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        as_a = await load_trace(session, audit_id=audit_id, tenant_id=tenant_a)
        as_b = await load_trace(session, audit_id=audit_id, tenant_id=tenant_b)

    assert as_a is None  # tenant A cannot read tenant B's trace
    assert as_b is not None  # the owning tenant still can
