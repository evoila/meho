# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Flight-recorder AGENT read-surface live trigger, through the full dispatch (#3216, F5).

Drives the real :func:`meho_backplane.operations.dispatch` to prove the live
trigger the recorder wires after ``record_trace``:

* a **clean** trace surfaces an agent handle on the dispatch response
  (`OperationResult.extras["flight_recorder_trace_handle"]`) that is pageable
  via the **unchanged** ``result_query`` core;
* a **redaction-uncertain** trace surfaces **no** handle (agent-invisible) while
  still persisting for the operator plane;
* a tenant with the **agent gate off** surfaces no handle (capture — and thus
  the operator plane — is unaffected);
* **F7**: a forced trigger (mint) failure leaves the dispatch result
  byte-identical and the audit row + trace intact.

Shares the typed-op registration harness the capture-dispatch test uses
(in-memory SQLite, recording broadcast publisher, autouse resets), plus a
capture-enabled seeded tenant so ``should_capture`` returns True. A shared
in-memory Valkey fake backs one ``ResultHandleStore`` wired into both the mint
(spill) and the ``result_query`` core (read).
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

import meho_backplane.flight_recorder.agent_read as agent_read_mod
import meho_backplane.operations._audit as audit_module
import meho_backplane.operations.result_query as result_query_core
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, DispatchTrace, EndpointDescriptor, Tenant
from meho_backplane.flight_recorder import capture
from meho_backplane.flight_recorder.agent_read import AGENT_TRACE_HANDLE_EXTRA_KEY
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.result_query import read_result_window
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
    async with get_sessionmaker()() as s:
        yield s


class _FakeRedis:
    """In-memory async Valkey stand-in (mirrors the store's own test double)."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        self.store[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.store.get(name)


@pytest.fixture
def shared_store(monkeypatch: pytest.MonkeyPatch) -> tuple[ResultHandleStore, _FakeRedis]:
    """One store wired into BOTH the mint (spill) and the result_query core (read)."""
    fake = _FakeRedis()
    store = ResultHandleStore(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(agent_read_mod, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    return store, fake


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


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(slug: str, *, agent_readable: bool | None = None) -> UUID:
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            Tenant(
                id=tenant_id,
                slug=slug,
                name=slug,
                flight_recorder_enabled=True,
                flight_recorder_agent_readable=agent_readable,
            )
        )
        await s.commit()
    return tenant_id


def _make_operator(tenant_id: UUID) -> Operator:
    return Operator(
        sub="op-agent",
        name="Agent",
        email=None,
        raw_jwt="<jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeTarget:
    def __init__(self) -> None:
        self.product = "vault"
        self.fingerprint = type("_F", (), {"version": None})()
        self.preferred_impl_id: str | None = None
        self.id = uuid.uuid4()
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


async def _register_composite(stub_embedding_service: AsyncMock) -> None:
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
    async with get_sessionmaker()() as s:
        s.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id="vault.composite.two",
                source_kind="composite",
                method=None,
                path=None,
                handler_ref="tests.test_flight_recorder_agent_read_dispatch._two_child_composite",
                summary="Composite two.",
                description="Composite two.",
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=stub_embedding_service.encode_one.return_value,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await s.commit()


async def _dispatch_composite(tenant_id: UUID) -> OperationResult:
    return await dispatch(
        operator=_make_operator(tenant_id),
        connector_id="vault-1.x",
        op_id="vault.composite.two",
        target=_FakeTarget(),
        params={},
    )


async def _traces() -> list[DispatchTrace]:
    async with get_sessionmaker()() as s:
        return list((await s.execute(select(DispatchTrace))).scalars().all())


# ---------------------------------------------------------------------------
# Clean trace -> pageable agent handle on the response envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_trace_surfaces_pageable_agent_handle(
    stub_embedding_service: AsyncMock,
    shared_store: tuple[ResultHandleStore, _FakeRedis],
) -> None:
    _store, _fake = shared_store
    tenant_id = await _seed_tenant("fr-ar-disp-clean")  # capture on, agent inherit => on
    await _register_composite(stub_embedding_service)

    result = await _dispatch_composite(tenant_id)
    assert result.status == "ok", result.error

    # The live trigger surfaced a trace handle on the response envelope.
    assert AGENT_TRACE_HANDLE_EXTRA_KEY in result.extras
    handle_json = result.extras[AGENT_TRACE_HANDLE_EXTRA_KEY]
    assert handle_json["fetch_more"]["drill_in"]["mcp_tool"] == "result_query"
    handle_id = UUID(handle_json["handle_id"])

    # The agent pages the ordered spans back via the UNCHANGED result_query core.
    operator = _make_operator(tenant_id)
    window = await read_result_window(operator, handle_id, offset=0, limit=50)
    assert window["total_rows"] == handle_json["total_rows"]
    kinds = [row["span_kind"] for row in window["rows"]]
    assert "composite_step" in kinds


# ---------------------------------------------------------------------------
# Redaction-uncertain trace -> NO agent handle (operator plane keeps it)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redaction_uncertain_trace_surfaces_no_agent_handle(
    stub_embedding_service: AsyncMock,
    shared_store: tuple[ResultHandleStore, _FakeRedis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, fake = shared_store
    tenant_id = await _seed_tenant("fr-ar-disp-uncertain")
    await _register_composite(stub_embedding_service)

    # Force the trace redaction-uncertain (what the #3213 engine sets on a body
    # it cannot prove clean) while keeping the real persistence path.
    real_record = capture.record_trace

    async def _force_uncertain(**kwargs: Any) -> Any:
        kwargs["redaction_uncertain"] = True
        return await real_record(**kwargs)

    monkeypatch.setattr(capture, "record_trace", _force_uncertain)

    result = await _dispatch_composite(tenant_id)
    assert result.status == "ok", result.error

    # Agent-INVISIBLE: no handle on the envelope, nothing spilled.
    assert AGENT_TRACE_HANDLE_EXTRA_KEY not in result.extras
    assert fake.store == {}

    # Operator-VISIBLE: the uncertain trace is still persisted.
    traces = await _traces()
    assert len(traces) == 1
    assert traces[0].redaction_uncertain is True


# ---------------------------------------------------------------------------
# Per-tenant gate off -> no agent handle (capture / operator plane unaffected)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_off_surfaces_no_agent_handle(
    stub_embedding_service: AsyncMock,
    shared_store: tuple[ResultHandleStore, _FakeRedis],
) -> None:
    _store, fake = shared_store
    # Capture ON (operator plane keeps data) but agent-read explicitly OFF.
    tenant_id = await _seed_tenant("fr-ar-disp-gateoff", agent_readable=False)
    await _register_composite(stub_embedding_service)

    result = await _dispatch_composite(tenant_id)
    assert result.status == "ok", result.error

    assert AGENT_TRACE_HANDLE_EXTRA_KEY not in result.extras
    assert fake.store == {}  # gate checked before any spill
    # Capture unaffected: the trace is still persisted for the operator plane.
    assert len(await _traces()) == 1


# ---------------------------------------------------------------------------
# F7 -- a forced trigger (mint) failure leaves the dispatch byte-identical
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f7_forced_trigger_failure_leaves_dispatch_byte_identical(
    stub_embedding_service: AsyncMock,
    shared_store: tuple[ResultHandleStore, _FakeRedis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _fake = shared_store
    tenant_id = await _seed_tenant("fr-ar-disp-f7")
    await _register_composite(stub_embedding_service)

    async def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("mint down")

    monkeypatch.setattr(agent_read_mod, "materialize_agent_trace_handle", _boom)

    result = await _dispatch_composite(tenant_id)

    # Byte-identical dispatch result — the trigger failure was swallowed (F7).
    assert result.status == "ok", result.error
    assert result.result == {"a": "ok", "b": "ok"}
    assert AGENT_TRACE_HANDLE_EXTRA_KEY not in result.extras

    # The audit row committed and the trace persisted — capture is untouched.
    async with get_sessionmaker()() as s:
        audit_count = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.path == "vault.composite.two")
            )
        ).scalar_one()
    assert audit_count == 1
    assert len(await _traces()) == 1
