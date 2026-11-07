# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A handler's ``audit_*`` binds land on its own audit row only (#5211).

A connector handler enriches its DISPATCH row by binding ``audit_*``
structlog contextvars (bind9's ``audit_state_before`` / ``audit_state_after``,
Azure's subscription block). The dispatcher puts the context back as it was
when the dispatch ends, so:

* the next dispatch in the same context (an agent loop, a runbook) does not
  carry the earlier call's values;
* a parent row written after a nested dispatch does not carry the child's
  values;
* a key the caller bound before the dispatch (the approved run's
  ``audit_approval_request_id``) still reaches the row and stays bound;
* a handler that raises still has its binds removed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._audit import restore_audit_extras, snapshot_audit_extras
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-00000000b5b5")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    structlog.contextvars.clear_contextvars()
    register_connector_v2(product="demo", version="", impl_id="", cls=_NoOpConnector)
    yield
    reset_dispatcher_caches()
    clear_registry()
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


class _NoOpConnector(Connector):
    product = "demo"
    version = "1.x"
    impl_id = "demo"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> Any:
        raise NotImplementedError


class _FakeFingerprint:
    version: str | None = None


class _FakeTarget:
    def __init__(self) -> None:
        self.product = "demo"
        self.fingerprint = _FakeFingerprint()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT
        self.name = "demo-target"
        self.host = "demo.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


_OPERATOR = Operator(
    sub="user-scope",
    name="Scope Tester",
    email=None,
    raw_jwt="<test-raw-jwt>",
    tenant_id=_TENANT,
    tenant_role=TenantRole.OPERATOR,
    principal_kind=PrincipalKind.USER,
)


async def _binding_handler(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    structlog.contextvars.bind_contextvars(
        audit_state_before="zone v1", audit_state_after="zone v2"
    )
    return {"done": True}


async def _plain_handler(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    return {"done": True}


async def _raising_handler(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    structlog.contextvars.bind_contextvars(audit_state_before="half written")
    raise RuntimeError("boom")


async def _parent_handler(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    child = await _dispatch("demo.child.write")
    assert child.status == "ok"
    return {"done": True}


async def _register(op_id: str, handler: Any, embedding: AsyncMock) -> None:
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id=op_id,
        handler=handler,
        summary="Demo op.",
        description="Demo op used by the audit extras scope tests.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="safe",
    )


async def _dispatch(op_id: str) -> Any:
    return await dispatch(
        operator=_OPERATOR,
        connector_id="demo-1.x",
        op_id=op_id,
        target=_FakeTarget(),
        params={},
    )


async def _row(op_id: str) -> AuditLog:
    async with get_sessionmaker()() as session:
        query = select(AuditLog).where(AuditLog.path == op_id, AuditLog.method == "DISPATCH")
        rows = (await session.execute(query)).scalars().all()
    assert len(rows) == 1
    return rows[0]


@pytest.mark.asyncio
async def test_next_dispatch_does_not_carry_earlier_values(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register("demo.zone.write", _binding_handler, stub_embedding_service)
    await _register("demo.zone.get", _plain_handler, stub_embedding_service)

    assert (await _dispatch("demo.zone.write")).status == "ok"
    assert (await _dispatch("demo.zone.get")).status == "ok"

    first = await _row("demo.zone.write")
    assert first.payload["state_before"] == "zone v1"
    assert first.payload["state_after"] == "zone v2"
    second = await _row("demo.zone.get")
    assert "state_before" not in second.payload
    assert "state_after" not in second.payload
    assert snapshot_audit_extras() == {}


@pytest.mark.asyncio
async def test_parent_row_does_not_carry_child_values(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register("demo.child.write", _binding_handler, stub_embedding_service)
    await _register("demo.parent.run", _parent_handler, stub_embedding_service)

    assert (await _dispatch("demo.parent.run")).status == "ok"

    assert (await _row("demo.child.write")).payload["state_before"] == "zone v1"
    assert "state_before" not in (await _row("demo.parent.run")).payload


@pytest.mark.asyncio
async def test_caller_bound_key_reaches_row_and_stays(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register("demo.zone.write", _binding_handler, stub_embedding_service)
    structlog.contextvars.bind_contextvars(
        audit_approval_request_id="req-1", audit_state_before="caller value"
    )

    assert (await _dispatch("demo.zone.write")).status == "ok"

    row = await _row("demo.zone.write")
    assert row.payload["approval_request_id"] == "req-1"
    assert row.payload["state_before"] == "zone v1"
    assert snapshot_audit_extras() == {
        "audit_approval_request_id": "req-1",
        "audit_state_before": "caller value",
    }


@pytest.mark.asyncio
async def test_raising_handler_binds_are_removed(stub_embedding_service: AsyncMock) -> None:
    await _register("demo.zone.break", _raising_handler, stub_embedding_service)

    assert (await _dispatch("demo.zone.break")).status == "error"

    assert (await _row("demo.zone.break")).payload["state_before"] == "half written"
    assert snapshot_audit_extras() == {}


def test_restore_puts_back_unbound_and_changed_keys() -> None:
    structlog.contextvars.bind_contextvars(audit_kind="route", audit_skip=None)
    snapshot = snapshot_audit_extras()

    structlog.contextvars.unbind_contextvars("audit_kind")
    structlog.contextvars.bind_contextvars(audit_skip="set", audit_new="x")
    restore_audit_extras(snapshot)

    assert snapshot_audit_extras() == {"audit_kind": "route", "audit_skip": None}
