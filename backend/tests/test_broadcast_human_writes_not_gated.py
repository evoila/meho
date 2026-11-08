# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A person's write never waits for an announcement (#3762).

The opt-in announce gate is retired. People read Broadcast; only trusted
agents publish on it. So no write can depend on an announcement that a
person cannot make:

* a person's ``caution`` write runs without any claim, with and without
  an MCP session, also in a tenant whose old gate column is still ``true``;
* its audit row says ``ok``, never ``denied``;
* the tenant model and the settings file no longer offer the gate.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select, text

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.instance_settings.schema import TenantPolicies
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._audit import agent_session_id_var
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-000000003762")
_SESSION = UUID("37623762-3762-3762-3762-376237623762")
_OP_ID = "vault.kv.delete"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    # The advisories are tested on their own; keep them off the dispatch here.
    monkeypatch.setenv("REFLEX_ADVISORY_WINDOW_MINUTES", "0")
    monkeypatch.setenv("DISPATCH_ACTIVITY_ADVISORY_WINDOW_MINUTES", "0")
    monkeypatch.setenv("CHECKS_ALERT_ADVISORY_WINDOW_MINUTES", "0")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


def _person() -> Operator:
    # A person's ``caution`` call runs at once only in the web interface; with
    # a token it waits for approval first (#3980).
    return Operator(
        sub="person-1",
        name="person-1",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        web_session=True,
    )


@contextmanager
def _bound_session(session_id: UUID | None) -> Iterator[None]:
    token = agent_session_id_var.set(session_id)
    try:
        yield
    finally:
        agent_session_id_var.reset(token)


class _NoOpVaultConnector(Connector):
    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"deleted": True}


class _FakeFingerprint:
    version: str | None = None


class _FakeTarget:
    def __init__(self) -> None:
        self.product = "vault"
        self.fingerprint = _FakeFingerprint()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT
        self.name = "vault-1"
        self.host = "test.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


async def _register_write_op() -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_OP_ID,
        handler=_handler,
        summary="Test op.",
        description="Test op.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="caution",
    )


async def _seed_tenant_with_old_gate_on() -> None:
    """A tenant whose retired gate column is still ``true`` in the database."""
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT, slug="t-3762", name="Tenant"))
        await session.commit()
        updated = await session.execute(
            text("UPDATE tenant SET announce_gate_enabled = :on WHERE id = :id"),
            {"on": True, "id": _TENANT.hex},
        )
        assert getattr(updated, "rowcount", 0) == 1
        await session.commit()


@pytest.mark.parametrize("session", [_SESSION, None], ids=["mcp-session", "cli-no-session"])
async def test_person_write_without_announcement_runs(session: UUID | None) -> None:
    await _seed_tenant_with_old_gate_on()
    await _register_write_op()
    with _bound_session(session):
        result = await dispatch(
            operator=_person(),
            connector_id="vault-1.x",
            op_id=_OP_ID,
            target=_FakeTarget(),
            params={},
        )
    assert result.status == "ok", result.error
    assert result.result == {"deleted": True}
    assert "error_code" not in result.extras
    async with get_sessionmaker()() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == _OP_ID))).scalars().all()
        )
    assert [row.payload["result_status"] for row in rows] == ["ok"]


def test_tenant_model_has_no_gate_flag() -> None:
    assert "announce_gate_enabled" not in Tenant.__table__.columns


def test_settings_file_offers_no_gate_policy() -> None:
    assert "announce_gate_enabled" not in TenantPolicies.model_fields
