# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the flight-recorder **agent** read surface (#3216, F5).

Covers :func:`meho_backplane.flight_recorder.agent_read.materialize_agent_trace_handle`
and the AC of #3216 (F5 of ``docs/decisions/dispatch-flight-recorder.md``):

* an agent pages a **clean** trace end to end -- mint spills the ordered spans,
  and the **unchanged** ``result_query`` core
  (:func:`~meho_backplane.operations.result_query.read_result_window`) returns
  them window by window;
* **no new tool** is registered on the agent surface by this feature (the mint
  module registers nothing; the working-surface pin lives in the untouched
  ``test_mcp_surface_conformance.py``);
* a **redaction-uncertain** trace is agent-invisible (mint returns ``None``,
  nothing spilled) yet operator-visible (the trace row is still present) -- the
  F5 degrade / discharge property: a secret-bearing / uncertain span never
  reaches the agent handle;
* the **per-tenant gate off** => no agent access (nothing loaded, nothing
  spilled) while capture (the operator plane's data source) stays on;
* handle isolation -- a handle minted for one operator/tenant is unreadable by
  another operator or another tenant.

Traces are seeded via :func:`meho_backplane.flight_recorder.store.record_trace`
(the capture seam #3214 is not imported), against the autouse
``sqlite+aiosqlite`` engine the conftest pre-migrates to head. A shared
in-memory Valkey fake backs one :class:`ResultHandleStore` injected into both
the mint (spill) and the ``result_query`` core (fetch), so the round trip
exercises the real spill + read-back logic without a container.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.flight_recorder.agent_read as agent_read_mod
import meho_backplane.operations.result_query as result_query_core
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DispatchTrace, DispatchTraceSpan, Tenant
from meho_backplane.flight_recorder.agent_read import materialize_agent_trace_handle
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.flight_recorder.store import SpanInput, record_trace
from meho_backplane.operations.result_query import ResultHandleNotFoundError, read_result_window
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


class _FakeRedis:
    """In-memory async Valkey stand-in (mirrors the store's own test double)."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.last_ex: int | None = None

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        self.last_ex = ex
        self.store[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.store.get(name)


@pytest.fixture
def shared_store(monkeypatch: pytest.MonkeyPatch) -> tuple[ResultHandleStore, _FakeRedis]:
    """One store backed by a fake, wired into BOTH the mint and the read core.

    The mint receives it via its ``store=`` param; the ``result_query`` core
    resolves it through its patched getter -- so a spill by the mint is
    readable by :func:`read_result_window`, exactly as in production where both
    share the process singleton.
    """
    fake = _FakeRedis()
    store = ResultHandleStore(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    return store, fake


def _operator(tenant_id: uuid.UUID, *, sub: str = "op-a") -> Operator:
    return Operator(
        sub=sub,
        name="Agent",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        scopes=frozenset(),
        platform_admin=False,
    )


async def _seed_tenant(
    session: AsyncSession,
    *,
    slug: str,
    enabled: bool = True,
    agent_readable: bool | None = None,
) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    session.add(
        Tenant(
            id=tenant_id,
            slug=slug,
            name=f"Tenant {slug}",
            flight_recorder_enabled=enabled,
            flight_recorder_agent_readable=agent_readable,
        )
    )
    await session.commit()
    return tenant_id


def _clean_spans(n: int = 3) -> list[SpanInput]:
    started = datetime(2026, 8, 31, 9, 0, 0, tzinfo=UTC)
    return [
        SpanInput(
            span_kind="vendor_call",
            name=f"GET /rest/vm/{i}",
            started_at=started,
            duration_ms=Decimal("10.25"),
            status="200",
            attributes={"method": "GET", "url": f"https://vendor.example/rest/vm/{i}"},
        )
        for i in range(n)
    ]


async def _new_tenant(
    *, slug: str, enabled: bool = True, agent_readable: bool | None = None
) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        return await _seed_tenant(
            session, slug=slug, enabled=enabled, agent_readable=agent_readable
        )


# --------------------------------------------------------------------------
# AC: agent pages a CLEAN trace via result_query (the reuse path)
# --------------------------------------------------------------------------


async def test_agent_pages_clean_trace_via_result_query(
    shared_store: tuple[ResultHandleStore, _FakeRedis],
) -> None:
    store, _fake = shared_store
    tenant_id = await _new_tenant(slug="fr-ar-clean", enabled=True)  # inherit => agent-readable
    audit_id = uuid.uuid4()
    await record_trace(audit_id=audit_id, tenant_id=tenant_id, spans=_clean_spans(3))

    operator = _operator(tenant_id)
    handle = await materialize_agent_trace_handle(operator=operator, audit_id=audit_id, store=store)

    assert handle is not None
    assert handle.total_rows == 3
    # The handle is self-documenting: it points the agent at result_query.
    assert handle.fetch_more is not None
    assert handle.fetch_more.drill_in.available is True
    assert handle.fetch_more.drill_in.mcp_tool == "result_query"

    # Page the FULL ordered set back through the UNCHANGED result_query core.
    window = await read_result_window(operator, handle.handle_id, offset=0, limit=50)
    assert window["total_rows"] == 3
    assert window["returned_rows"] == 3
    names = [row["name"] for row in window["rows"]]
    assert names == ["GET /rest/vm/0", "GET /rest/vm/1", "GET /rest/vm/2"]
    assert [row["seq"] for row in window["rows"]] == [0, 1, 2]
    assert window["rows"][0]["attributes"]["method"] == "GET"


async def test_agent_trace_handle_pages_across_windows(
    shared_store: tuple[ResultHandleStore, _FakeRedis],
) -> None:
    store, _fake = shared_store
    tenant_id = await _new_tenant(slug="fr-ar-page", enabled=True)
    audit_id = uuid.uuid4()
    await record_trace(audit_id=audit_id, tenant_id=tenant_id, spans=_clean_spans(5))

    operator = _operator(tenant_id)
    handle = await materialize_agent_trace_handle(operator=operator, audit_id=audit_id, store=store)
    assert handle is not None

    first = await read_result_window(operator, handle.handle_id, offset=0, limit=2)
    assert [r["seq"] for r in first["rows"]] == [0, 1]
    second = await read_result_window(operator, handle.handle_id, offset=2, limit=2)
    assert [r["seq"] for r in second["rows"]] == [2, 3]
    tail = await read_result_window(operator, handle.handle_id, offset=4, limit=2)
    assert [r["seq"] for r in tail["rows"]] == [4]
    end = await read_result_window(operator, handle.handle_id, offset=5, limit=2)
    assert end["rows"] == []


async def test_inline_sample_is_byte_bounded_not_a_raw_payload(
    shared_store: tuple[ResultHandleStore, _FakeRedis],
) -> None:
    """Postulate 6: heavy span bodies never ship a raw payload inline.

    A span body is capped at F3's 64 KB, so a count-only inline sample could
    dump hundreds of KB into agent context. The sample is byte-bounded (like
    the JSONFlux reducer), so a trace of large spans ships a *small* preview
    while the full ordered set stays reachable via ``result_query``.
    """
    store, _fake = shared_store
    tenant_id = await _new_tenant(slug="fr-ar-bigbodies", enabled=True)
    audit_id = uuid.uuid4()
    big_body = "x" * 2000  # ~2 KB redacted body per span; default budget is 4 KB
    spans = [
        SpanInput(
            span_kind="vendor_call",
            name=f"GET /rest/big/{i}",
            started_at=datetime(2026, 8, 31, 9, 0, 0, tzinfo=UTC),
            status="200",
            attributes={"redacted_body": big_body},
        )
        for i in range(5)
    ]
    await record_trace(audit_id=audit_id, tenant_id=tenant_id, spans=spans)

    operator = _operator(tenant_id)
    handle = await materialize_agent_trace_handle(operator=operator, audit_id=audit_id, store=store)
    assert handle is not None
    assert handle.total_rows == 5
    # The inline preview is bounded well below the full set...
    assert handle.sample_rows is not None
    assert 1 <= len(handle.sample_rows) < 5
    # ...yet the FULL ordered set is still retrievable via result_query.
    window = await read_result_window(operator, handle.handle_id, offset=0, limit=50)
    assert window["total_rows"] == 5
    assert window["returned_rows"] == 5


# --------------------------------------------------------------------------
# AC: redaction-uncertain trace is agent-INVISIBLE but operator-VISIBLE
#     (the F5 discharge: a secret-bearing / uncertain span never reaches the
#      agent handle)
# --------------------------------------------------------------------------


async def test_redaction_uncertain_trace_is_withheld_from_agent(
    shared_store: tuple[ResultHandleStore, _FakeRedis],
) -> None:
    store, fake = shared_store
    tenant_id = await _new_tenant(slug="fr-ar-uncertain", enabled=True)
    audit_id = uuid.uuid4()
    # A span that could carry a secret, on a trace redaction could not prove clean.
    secret_span = SpanInput(
        span_kind="vendor_call",
        name="POST /login",
        started_at=datetime(2026, 8, 31, 9, 0, 0, tzinfo=UTC),
        status="200",
        attributes={"body": "token=SUPER-SECRET-DO-NOT-LEAK"},
    )
    await record_trace(
        audit_id=audit_id,
        tenant_id=tenant_id,
        spans=[secret_span],
        redaction_uncertain=True,
    )

    operator = _operator(tenant_id)
    handle = await materialize_agent_trace_handle(operator=operator, audit_id=audit_id, store=store)

    # Agent-INVISIBLE: no handle, and NOTHING was spilled to the read-back store.
    assert handle is None
    assert fake.store == {}

    # Operator-VISIBLE: the trace (and its uncertainty flag) is retained on the
    # operator plane's data source, unaffected by the agent degrade.
    async with get_sessionmaker()() as session:
        header = (
            await session.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one()
        span_count = len(
            (
                await session.execute(
                    select(DispatchTraceSpan).where(DispatchTraceSpan.trace_id == header.id)
                )
            )
            .scalars()
            .all()
        )
    assert header.redaction_uncertain is True
    assert span_count == 1


# --------------------------------------------------------------------------
# AC: per-tenant gate OFF => no agent access (operator plane unaffected)
# --------------------------------------------------------------------------


async def test_gate_off_denies_agent_access(
    shared_store: tuple[ResultHandleStore, _FakeRedis],
) -> None:
    store, fake = shared_store
    # Capture ON (operator plane keeps data) but agent-read explicitly OFF.
    tenant_id = await _new_tenant(slug="fr-ar-gateoff", enabled=True, agent_readable=False)
    audit_id = uuid.uuid4()
    await record_trace(audit_id=audit_id, tenant_id=tenant_id, spans=_clean_spans(3))

    operator = _operator(tenant_id)
    handle = await materialize_agent_trace_handle(operator=operator, audit_id=audit_id, store=store)

    assert handle is None
    assert fake.store == {}  # gate checked BEFORE any load/spill

    # Operator plane still has the trace.
    async with get_sessionmaker()() as session:
        header = (
            await session.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one_or_none()
    assert header is not None


async def test_gate_inherit_off_denies_when_capture_off(
    shared_store: tuple[ResultHandleStore, _FakeRedis],
) -> None:
    store, fake = shared_store
    # NULL override + capture OFF => inherits OFF (a non-lab tenant).
    tenant_id = await _new_tenant(slug="fr-ar-noncapture", enabled=False)
    audit_id = uuid.uuid4()
    await record_trace(audit_id=audit_id, tenant_id=tenant_id, spans=_clean_spans(2))

    handle = await materialize_agent_trace_handle(
        operator=_operator(tenant_id), audit_id=audit_id, store=store
    )
    assert handle is None
    assert fake.store == {}


# --------------------------------------------------------------------------
# Handle isolation: cross-operator + cross-tenant
# --------------------------------------------------------------------------


async def test_handle_not_readable_by_another_operator(
    shared_store: tuple[ResultHandleStore, _FakeRedis],
) -> None:
    store, _fake = shared_store
    tenant_id = await _new_tenant(slug="fr-ar-xop", enabled=True)
    audit_id = uuid.uuid4()
    await record_trace(audit_id=audit_id, tenant_id=tenant_id, spans=_clean_spans(2))

    minter = _operator(tenant_id, sub="op-a")
    handle = await materialize_agent_trace_handle(operator=minter, audit_id=audit_id, store=store)
    assert handle is not None

    # A different operator in the SAME tenant gets a not-found miss, not rows.
    stranger = _operator(tenant_id, sub="op-b")
    with pytest.raises(ResultHandleNotFoundError):
        await read_result_window(stranger, handle.handle_id, offset=0, limit=50)


async def test_cross_tenant_audit_id_is_a_miss(
    shared_store: tuple[ResultHandleStore, _FakeRedis],
) -> None:
    store, fake = shared_store
    owner_tenant = await _new_tenant(slug="fr-ar-owner", enabled=True)
    other_tenant = await _new_tenant(slug="fr-ar-other", enabled=True)
    audit_id = uuid.uuid4()
    await record_trace(audit_id=audit_id, tenant_id=owner_tenant, spans=_clean_spans(2))

    # An operator in a DIFFERENT tenant asking for the same audit_id gets None:
    # the trace load is tenant-scoped, so it never matches.
    handle = await materialize_agent_trace_handle(
        operator=_operator(other_tenant), audit_id=audit_id, store=store
    )
    assert handle is None
    assert fake.store == {}


# --------------------------------------------------------------------------
# Edge cases + the no-new-tool guard
# --------------------------------------------------------------------------


async def test_missing_trace_returns_none(
    shared_store: tuple[ResultHandleStore, _FakeRedis],
) -> None:
    store, _fake = shared_store
    tenant_id = await _new_tenant(slug="fr-ar-missing", enabled=True)
    handle = await materialize_agent_trace_handle(
        operator=_operator(tenant_id), audit_id=uuid.uuid4(), store=store
    )
    assert handle is None


async def test_operator_without_tenant_gets_none(
    shared_store: tuple[ResultHandleStore, _FakeRedis],
) -> None:
    """The defensive no-tenant guard (parity with ``read_result_window``).

    ``Operator.tenant_id`` is a required ``UUID`` at the model boundary, so a
    tenant-less operator is only reachable via ``model_construct`` (validation
    bypassed) -- exactly the belt-and-suspenders state the shared read core
    also guards. A tenant-less identity can never own a spilled handle.
    """
    store, _fake = shared_store
    operator = Operator.model_construct(
        sub="op-notenant",
        name="Agent",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=None,
        tenant_role=TenantRole.OPERATOR,
        scopes=frozenset(),
        platform_admin=False,
    )
    handle = await materialize_agent_trace_handle(
        operator=operator, audit_id=uuid.uuid4(), store=store
    )
    assert handle is None


async def test_empty_trace_yields_no_handle(
    shared_store: tuple[ResultHandleStore, _FakeRedis],
) -> None:
    """A header-only (0-span) trace has nothing to page -> no handle."""
    store, fake = shared_store
    tenant_id = await _new_tenant(slug="fr-ar-empty", enabled=True)
    audit_id = uuid.uuid4()
    await record_trace(audit_id=audit_id, tenant_id=tenant_id, spans=[])
    handle = await materialize_agent_trace_handle(
        operator=_operator(tenant_id), audit_id=audit_id, store=store
    )
    assert handle is None
    assert fake.store == {}


def test_agent_read_registers_no_mcp_tool() -> None:
    """No new tool on the working surface: the mint module registers nothing.

    The agent reads a trace through the existing ``result_query`` meta-tool;
    this feature adds NO tool. The authoritative working-surface inventory is
    pinned separately by the untouched ``test_mcp_surface_conformance.py``.
    """
    source = inspect.getsource(agent_read_mod)
    assert "register_mcp_tool" not in source
    assert "ToolDefinition" not in source
    # The read path is the existing tool, referenced by name only.
    assert "result_query" in source
