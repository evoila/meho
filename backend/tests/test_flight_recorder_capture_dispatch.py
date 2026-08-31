# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Flight-recorder capture through the full dispatch path (#3214).

Drives the real :func:`meho_backplane.operations.dispatch` to prove:

* **composite sub-step spans** land under the **one** parent trace (no second
  trace is created for a child dispatch);
* the **JSONFlux reduction span** is emitted when the reducer mints a handle;
* the **F7 invariant** -- a forced ``record_trace`` failure leaves the dispatch
  result byte-identical and the audit row committed.

Shares the typed-op registration harness the dispatcher tests use (in-memory
SQLite, recording broadcast publisher, autouse reset), plus a capture-enabled
seeded tenant so ``should_capture`` returns True.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    DispatchTrace,
    DispatchTraceSpan,
    EndpointDescriptor,
    Tenant,
)
from meho_backplane.flight_recorder import capture
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.reducer import PassThroughReducer
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


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture(autouse=True)
def _capture_broadcast(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


@pytest.fixture
def jsonflux_reducer() -> Iterator[None]:
    """Swap the real :class:`JsonFluxReducer` in as the dispatcher default.

    The v0.2 dispatcher default is :class:`PassThroughReducer` (never reduces),
    so the reduction-span path only fires with the real reducer installed.
    """
    set_default_reducer(JsonFluxReducer())
    try:
        yield
    finally:
        set_default_reducer(PassThroughReducer())
        reset_dispatcher_caches()


# ---------------------------------------------------------------------------
# Handlers (module-level so composite ``handler_ref`` resolves via importlib)
# ---------------------------------------------------------------------------


async def _child_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"echo": params}


async def _two_child_composite(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: Any,
) -> dict[str, Any]:
    a = await dispatch_child(connector_id="vault-1.x", op_id="vault.kv.list", params={"p": "a"})
    b = await dispatch_child(connector_id="vault-1.x", op_id="vault.kv.list", params={"p": "b"})
    return {"a": a.status, "b": b.status}


async def _large_list_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    return {"items": [{"n": i} for i in range(60)]}


async def _simple_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    return {"ok": True, "value": 42}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_capture_tenant(slug: str) -> UUID:
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        s.add(Tenant(id=tenant_id, slug=slug, name=slug, flight_recorder_enabled=True))
        await s.commit()
    return tenant_id


def _make_operator(tenant_id: UUID) -> Operator:
    return Operator(
        sub="op-capture",
        name="Cap",
        email=None,
        raw_jwt="<jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeTarget:
    def __init__(self, target_id: UUID | None = None) -> None:
        self.product = "vault"
        self.fingerprint = type("_F", (), {"version": None})()
        self.preferred_impl_id: str | None = None
        self.id = target_id or uuid.uuid4()
        self.name = "cap-target"
        self.host = "test.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


class _NoOpVaultConnector(Connector):
    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


async def _insert_composite_descriptor(
    *, session: AsyncSession, op_id: str, handler_ref: str, embedding: list[float]
) -> None:
    session.add(
        EndpointDescriptor(
            id=uuid.uuid4(),
            tenant_id=None,
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=op_id,
            source_kind="composite",
            method=None,
            path=None,
            handler_ref=handler_ref,
            summary=f"Composite {op_id}.",
            description=f"Composite {op_id}.",
            tags=[],
            parameter_schema={"type": "object"},
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
    )
    await session.commit()


async def _traces() -> list[DispatchTrace]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        return list((await s.execute(select(DispatchTrace))).scalars().all())


async def _spans(trace_id: UUID) -> list[DispatchTraceSpan]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        return list(
            (
                await s.execute(
                    select(DispatchTraceSpan)
                    .where(DispatchTraceSpan.trace_id == trace_id)
                    .order_by(DispatchTraceSpan.seq.asc())
                )
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# Composite sub-step spans land under one parent trace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_substeps_under_one_parent_trace(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    tenant_id = await _seed_capture_tenant("fr-disp-composite")
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.list",
        handler=_child_handler,
        summary="List.",
        description="List.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )
    await _insert_composite_descriptor(
        session=session,
        op_id="vault.composite.two",
        handler_ref="tests.test_flight_recorder_capture_dispatch._two_child_composite",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(tenant_id),
        connector_id="vault-1.x",
        op_id="vault.composite.two",
        target=_FakeTarget(),
        params={},
    )
    assert result.status == "ok", result.error

    # Exactly one trace -- the children joined the parent, no second trace.
    traces = await _traces()
    assert len(traces) == 1
    trace = traces[0]

    # The trace hangs off the parent (root) audit row.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        parent = (
            await s.execute(select(AuditLog).where(AuditLog.path == "vault.composite.two"))
        ).scalar_one()
    assert trace.audit_id == parent.id
    assert parent.parent_audit_id is None

    spans = await _spans(trace.id)
    step_spans = [s for s in spans if s.span_kind == "composite_step"]
    assert len(step_spans) == 2
    assert {s.attributes["op_id"] for s in step_spans} == {"vault.kv.list"}


# ---------------------------------------------------------------------------
# JSONFlux reduction span
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jsonflux_reduction_span_recorded(
    stub_embedding_service: AsyncMock,
    jsonflux_reducer: None,
) -> None:
    tenant_id = await _seed_capture_tenant("fr-disp-flux")
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.big.list",
        handler=_large_list_handler,
        summary="Big list.",
        description="Big list.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    result = await dispatch(
        operator=_make_operator(tenant_id),
        connector_id="vault-1.x",
        op_id="vault.big.list",
        target=_FakeTarget(),
        params={},
    )
    assert result.status == "ok", result.error
    assert result.handle is not None  # the reducer minted a handle

    traces = await _traces()
    assert len(traces) == 1
    spans = await _spans(traces[0].id)
    flux = [s for s in spans if s.span_kind == "jsonflux_reduction"]
    assert len(flux) == 1
    assert flux[0].attributes["input_rows"] == 60
    assert "n" in flux[0].attributes["kept_fields"]
    assert flux[0].attributes["handle"]


# ---------------------------------------------------------------------------
# F7 -- a forced record_trace failure leaves the dispatch + audit intact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f7_forced_record_trace_failure_leaves_dispatch_and_audit_intact(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = await _seed_capture_tenant("fr-disp-f7")
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.simple.read",
        handler=_simple_handler,
        summary="Simple.",
        description="Simple.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    async def _boom(**_kwargs: Any) -> None:
        raise RuntimeError("trace store down")

    monkeypatch.setattr(capture, "record_trace", _boom)

    result = await dispatch(
        operator=_make_operator(tenant_id),
        connector_id="vault-1.x",
        op_id="vault.simple.read",
        target=_FakeTarget(),
        params={},
    )

    # The dispatch result is byte-identical to the un-instrumented shape ...
    assert result.status == "ok", result.error
    assert result.result == {"ok": True, "value": 42}

    # ... the audit row still committed ...
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        audit_count = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.path == "vault.simple.read")
            )
        ).scalar_one()
    assert audit_count == 1

    # ... and the forced failure left no trace (swallowed, not raised).
    assert await _traces() == []
