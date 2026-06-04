# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the G11.3-T5 scheduler admin surface (#826).

Coverage matrix:

* **Service layer (CRUD)** -- create / list / get / cancel against the
  :class:`SchedulerAdminService` for each kind (cron / one_off /
  event). The kind-specific fields land on the right column.
* **Discriminated-union validation** -- a body that violates the
  exactly-one-of-cron_expr/fire_at/event_filter invariant surfaces as
  a Pydantic ``ValidationError`` (422 at the REST boundary).
* **Tenant boundary** -- tenant A operator cannot see tenant B's
  triggers via list; tenant A admin cannot cancel tenant B's trigger
  by id (returns 404, not 403, to avoid existence-leak).
* **RBAC at REST** -- operator can list; create / cancel require
  tenant_admin; cross-tenant ``tenant_filter`` for an operator
  returns 403.
* **Cancel idempotency + terminal-fired guard** -- cancelling an
  already-cancelled trigger is a no-op (success); cancelling a
  terminal-fired one-off returns 409 (idiomatically, ``False`` at
  service layer).
* **MCP tools** -- the three ``meho.scheduler.*`` tools are
  registered, dispatch correctly, and inherit the audit contextvars.

The tests run on the SQLite-backed engine from
:mod:`tests.conftest`. The full REST middleware chain
(RequestContext -> Audit -> router) is exercised via
:class:`fastapi.testclient.TestClient`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from meho_backplane.agents.schemas import AgentDefinitionCreate, AgentModelTier
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPrincipal,
    AuditLog,
    ScheduledTrigger,
    ScheduledTriggerKind,
    ScheduledTriggerStatus,
    Tenant,
)
from meho_backplane.main import app
from meho_backplane.mcp.registry import get_tool
from meho_backplane.scheduler.schemas import ScheduledTriggerCreate
from meho_backplane.scheduler.service import (
    AgentDefinitionMissingError,
    SchedulerAdminService,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._vault_fakes import install_fake_vault

_TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    # Turn off the lifespan scheduler so the test stays deterministic
    # (no background ticks against the test DB).
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _token(
    key: Any,
    *,
    sub: str = "op-admin",
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant_id: UUID = _TENANT_A,
) -> str:
    return mint_token(key, sub=sub, tenant_role=role.value, tenant_id=str(tenant_id))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


async def _seed_tenant(tenant_id: UUID, slug: str) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        existing = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        if existing.scalar_one_or_none() is None:
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
            await session.commit()


async def _seed_agent_definition(
    *,
    tenant_id: UUID,
    name: str,
) -> UUID:
    """Insert a tenant + AgentPrincipal + enabled AgentDefinition; return def id.

    Mirrors :func:`tests.test_scheduler._seed_tenant_and_agent` -- the
    principal seed is what makes
    :meth:`AgentDefinitionService._validate_identity_ref` accept the
    create.
    """
    await _seed_tenant(tenant_id, slug=str(tenant_id)[:8])
    sessionmaker = get_sessionmaker()
    client_id = f"agent:{tenant_id}-{name}"
    async with sessionmaker() as session:
        existing = await session.execute(
            select(AgentPrincipal).where(
                AgentPrincipal.tenant_id == tenant_id,
                AgentPrincipal.keycloak_client_id == client_id,
            )
        )
        if existing.scalar_one_or_none() is None:
            session.add(
                AgentPrincipal(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    name=f"{tenant_id}-{name}",
                    keycloak_client_id=client_id,
                    keycloak_internal_id=f"kc-{tenant_id}-{name}",
                    owner_sub="seed-admin",
                    revoked=False,
                    created_by_sub="seed-admin",
                )
            )
            await session.commit()
    service = AgentDefinitionService()
    entry = await service.create(
        tenant_id=tenant_id,
        created_by_sub="seed-admin",
        payload=AgentDefinitionCreate(
            name=name,
            identity_ref=client_id,
            model_tier=AgentModelTier.STANDARD,
            system_prompt="seed",
            toolset={},
            turn_budget=2,
            enabled=True,
        ),
    )
    return entry.id


async def _fetch_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_create_schema_rejects_cron_without_expr() -> None:
    """A cron body without cron_expr fails the discriminated-union validator."""
    with pytest.raises(ValueError, match="cron_expr"):
        ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.CRON,
            agent_definition_id=uuid4(),
        )


def test_create_schema_rejects_one_off_without_fire_at() -> None:
    with pytest.raises(ValueError, match="fire_at"):
        ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.ONE_OFF,
            agent_definition_id=uuid4(),
        )


def test_create_schema_rejects_event_without_filter() -> None:
    with pytest.raises(ValueError, match="event_filter"):
        ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.EVENT,
            agent_definition_id=uuid4(),
        )


def test_create_schema_rejects_multiple_discriminators() -> None:
    """A body with both cron_expr and fire_at fails the validator."""
    with pytest.raises(ValueError, match="fire_at"):
        ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.CRON,
            agent_definition_id=uuid4(),
            cron_expr="*/5 * * * *",
            fire_at=datetime.now(UTC),
        )


def test_create_schema_rejects_invalid_cron_expression() -> None:
    with pytest.raises(ValueError, match="invalid cron expression"):
        ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.CRON,
            agent_definition_id=uuid4(),
            cron_expr="not a cron",
        )


def test_create_schema_rejects_unknown_timezone() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.CRON,
            agent_definition_id=uuid4(),
            cron_expr="*/5 * * * *",
            timezone="Mars/Phobos",
        )


def test_create_schema_accepts_valid_cron() -> None:
    body = ScheduledTriggerCreate(
        kind=ScheduledTriggerKind.CRON,
        agent_definition_id=uuid4(),
        cron_expr="0 9 * * *",
        timezone="Europe/Sarajevo",
    )
    assert body.kind == ScheduledTriggerKind.CRON
    assert body.timezone == "Europe/Sarajevo"


def test_create_schema_accepts_valid_event() -> None:
    body = ScheduledTriggerCreate(
        kind=ScheduledTriggerKind.EVENT,
        agent_definition_id=uuid4(),
        event_filter={"connector_id": "vmware-rest-9.0", "op_class": "alert"},
    )
    assert body.event_filter == {"connector_id": "vmware-rest-9.0", "op_class": "alert"}


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_create_cron_trigger() -> None:
    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="reporter")
    service = SchedulerAdminService()
    entry = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.CRON,
            agent_definition_id=def_id,
            cron_expr="*/5 * * * *",
            timezone="UTC",
        ),
    )
    assert entry.kind == ScheduledTriggerKind.CRON
    assert entry.cron_expr == "*/5 * * * *"
    assert entry.next_fire_at is not None
    assert entry.status == ScheduledTriggerStatus.ACTIVE
    assert entry.created_by_sub == "op-admin"


@pytest.mark.asyncio
async def test_service_create_one_off_trigger() -> None:
    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="one-shot")
    when = datetime.now(UTC) + timedelta(hours=1)
    service = SchedulerAdminService()
    entry = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.ONE_OFF,
            agent_definition_id=def_id,
            fire_at=when,
        ),
    )
    assert entry.kind == ScheduledTriggerKind.ONE_OFF
    assert entry.fire_at is not None
    assert entry.cron_expr is None


@pytest.mark.asyncio
async def test_service_create_event_trigger() -> None:
    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="event-bot")
    service = SchedulerAdminService()
    entry = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.EVENT,
            agent_definition_id=def_id,
            event_filter={"connector_id": "bind9", "op_class": "write"},
        ),
    )
    assert entry.kind == ScheduledTriggerKind.EVENT
    assert entry.event_filter == {"connector_id": "bind9", "op_class": "write"}
    assert entry.cron_expr is None
    assert entry.fire_at is None
    # Event triggers don't carry a wall-clock next_fire_at -- the outbox
    # dispatcher (T3) wakes them.
    assert entry.next_fire_at is None


@pytest.mark.asyncio
async def test_service_create_rejects_unknown_agent_definition() -> None:
    """A non-existent agent_definition_id raises AgentDefinitionMissingError."""
    await _seed_tenant(_TENANT_A, slug="tenant-a")
    service = SchedulerAdminService()
    bogus = uuid4()
    with pytest.raises(AgentDefinitionMissingError):
        await service.create(
            tenant_id=_TENANT_A,
            created_by_sub="op-admin",
            payload=ScheduledTriggerCreate(
                kind=ScheduledTriggerKind.CRON,
                agent_definition_id=bogus,
                cron_expr="*/5 * * * *",
            ),
        )


@pytest.mark.asyncio
async def test_service_list_filters_kind_and_status() -> None:
    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="filtered")
    service = SchedulerAdminService()
    cron = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.CRON,
            agent_definition_id=def_id,
            cron_expr="*/10 * * * *",
        ),
    )
    one_off = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.ONE_OFF,
            agent_definition_id=def_id,
            fire_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    cron_only = await service.list_(_TENANT_A, kind="cron")
    assert [t.id for t in cron_only] == [cron.id]
    one_off_only = await service.list_(_TENANT_A, kind="one_off")
    assert [t.id for t in one_off_only] == [one_off.id]
    all_active = await service.list_(_TENANT_A, status="active")
    assert {t.id for t in all_active} == {cron.id, one_off.id}


@pytest.mark.asyncio
async def test_service_cancel_active_trigger() -> None:
    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="cancellable")
    service = SchedulerAdminService()
    entry = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.CRON,
            agent_definition_id=def_id,
            cron_expr="*/5 * * * *",
        ),
    )
    cancelled = await service.cancel(_TENANT_A, entry.id)
    assert cancelled is True
    # Idempotent: a second cancel returns True (already cancelled).
    cancelled_again = await service.cancel(_TENANT_A, entry.id)
    assert cancelled_again is True
    fetched = await service.get(_TENANT_A, entry.id)
    assert fetched is not None
    assert fetched.status == ScheduledTriggerStatus.CANCELLED


@pytest.mark.asyncio
async def test_service_cancel_rejects_terminal_fired_one_off() -> None:
    """A one-off already in status='fired' is not cancellable."""
    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="fired-bot")
    service = SchedulerAdminService()
    entry = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.ONE_OFF,
            agent_definition_id=def_id,
            fire_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    # Force terminal-fired state out of band.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(ScheduledTrigger, entry.id)
        assert row is not None
        row.status = ScheduledTriggerStatus.FIRED.value
        await session.commit()
    cancelled = await service.cancel(_TENANT_A, entry.id)
    assert cancelled is False


@pytest.mark.asyncio
async def test_service_cancel_idempotent_under_concurrent_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for review B1 on PR #1128: cancel returns True, not phantom 409.

    The race shape: caller B's pre-flight SELECT sees ``status='active'``.
    Between B's SELECT and B's conditional UPDATE, caller A commits a
    cancel (the row is now ``cancelled``). B's UPDATE matches zero
    rows (the WHERE clause restricts to ``status IN (active, paused)``)
    and the naive code path then incorrectly raised 409
    ``trigger_already_fired``. The fix is a read-after-update: when
    rowcount==0, re-read the row's current status and treat
    ``CANCELLED`` as success (the other caller won the race, and the
    cancel is idempotent by contract).

    The test injects the race deterministically by wrapping
    :meth:`AsyncSession.execute` to commit an out-of-band cancel
    after the service's pre-flight SELECT returns but before the
    conditional UPDATE fires. Without the fix the assertion would
    fail with ``cancelled is False`` (mapping to 409 at the REST
    boundary).
    """
    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="race-bot")
    service = SchedulerAdminService()
    entry = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.CRON,
            agent_definition_id=def_id,
            cron_expr="*/5 * * * *",
        ),
    )

    # Inject the race: between the service's pre-flight SELECT and its
    # conditional UPDATE, commit a parallel cancel out of band so the
    # UPDATE's WHERE clause matches zero rows.
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.dml import Update as _UpdateStmt

    orig_execute = AsyncSession.execute
    raced = {"done": False}

    async def _racing_execute(self: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
        # Only race the very first UPDATE the service emits. The
        # service's pre-flight SELECT runs first and is left
        # untouched.
        if isinstance(statement, _UpdateStmt) and not raced["done"]:
            raced["done"] = True
            # Caller A wins the race: commit a CANCELLED transition
            # out of band in a fresh session so the test's call is
            # caller B (the one that sees stale ACTIVE).
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as side_session:
                row = await side_session.get(ScheduledTrigger, entry.id)
                assert row is not None
                row.status = ScheduledTriggerStatus.CANCELLED.value
                await side_session.commit()
        return await orig_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", _racing_execute)

    # Caller B's cancel: pre-flight sees ACTIVE, then the side write
    # commits CANCELLED, then the UPDATE matches zero rows. Without
    # the fix this returned False (phantom 409); with the fix the
    # read-after-update sees CANCELLED and returns True.
    cancelled = await service.cancel(_TENANT_A, entry.id)
    assert cancelled is True, "cancel must be idempotent under a lost race, not phantom-409"
    assert raced["done"], "the test fixture must actually have injected the race"

    # The row is still CANCELLED -- the lost-race cancel must not
    # mutate the already-cancelled row a second time.
    fetched = await service.get(_TENANT_A, entry.id)
    assert fetched is not None
    assert fetched.status == ScheduledTriggerStatus.CANCELLED


@pytest.mark.asyncio
async def test_service_cancel_rowcount_zero_with_fired_status_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Variant of the TOCTOU regression: terminal-FIRED won the race -> False.

    Same race shape as the idempotency test, but the side write
    transitions the row to ``FIRED`` (the scheduler dispatcher won the
    race instead of another cancel caller). The read-after-update sees
    FIRED and returns ``False`` so the boundary surfaces 409
    ``trigger_already_fired`` -- the *real* 409, not the phantom one.
    """
    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="race-fire-bot")
    service = SchedulerAdminService()
    entry = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.ONE_OFF,
            agent_definition_id=def_id,
            fire_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.dml import Update as _UpdateStmt

    orig_execute = AsyncSession.execute
    raced = {"done": False}

    async def _racing_execute(self: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(statement, _UpdateStmt) and not raced["done"]:
            raced["done"] = True
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as side_session:
                row = await side_session.get(ScheduledTrigger, entry.id)
                assert row is not None
                row.status = ScheduledTriggerStatus.FIRED.value
                await side_session.commit()
        return await orig_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", _racing_execute)

    cancelled = await service.cancel(_TENANT_A, entry.id)
    assert cancelled is False, "FIRED-after-pre-flight must surface as False (real 409)"
    assert raced["done"], "the test fixture must actually have injected the race"


@pytest.mark.asyncio
async def test_service_returns_none_across_tenant_boundary() -> None:
    """Tenant A cannot see / cancel tenant B's trigger by id."""
    def_id_b = await _seed_agent_definition(tenant_id=_TENANT_B, name="b-bot")
    service = SchedulerAdminService()
    entry_b = await service.create(
        tenant_id=_TENANT_B,
        created_by_sub="b-admin",
        payload=ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.CRON,
            agent_definition_id=def_id_b,
            cron_expr="*/5 * * * *",
        ),
    )
    # Tenant A probes tenant B's trigger id.
    assert await service.get(_TENANT_A, entry_b.id) is None
    # Tenant A's cancel against tenant B's id is a no-op.
    assert await service.cancel(_TENANT_A, entry_b.id) is False
    # The row is still active under tenant B.
    fetched = await service.get(_TENANT_B, entry_b.id)
    assert fetched is not None
    assert fetched.status == ScheduledTriggerStatus.ACTIVE


# ---------------------------------------------------------------------------
# REST surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_create_list_cancel_round_trip(client: TestClient) -> None:
    """POST creates -> GET lists -> DELETE cancels -> GET shows status=cancelled."""
    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="rest-bot")
    key = make_rsa_keypair("kid-rest")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {token}"}

        created = client.post(
            "/api/v1/scheduler/triggers",
            json={
                "kind": "cron",
                "agent_definition_id": str(def_id),
                "cron_expr": "*/5 * * * *",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        trigger_id = body["id"]
        assert body["kind"] == "cron"
        assert body["status"] == "active"
        assert body["created_by_sub"] == "op-admin"

        listed = client.get("/api/v1/scheduler/triggers", headers=headers)
        assert listed.status_code == 200
        assert [t["id"] for t in listed.json()["triggers"]] == [trigger_id]

        cancelled = client.delete(f"/api/v1/scheduler/triggers/{trigger_id}", headers=headers)
        assert cancelled.status_code == 204

        after = client.get(
            "/api/v1/scheduler/triggers", headers=headers, params={"status": "cancelled"}
        )
        assert after.status_code == 200
        rows = after.json()["triggers"]
        assert len(rows) == 1
        assert rows[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_rest_operator_can_list_but_not_create(client: TestClient) -> None:
    """An ``operator`` lists but is 403 on create/cancel."""
    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="op-test")
    admin_key = make_rsa_keypair("kid-adm")
    op_key = make_rsa_keypair("kid-op")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(admin_key))
        admin_headers = {"Authorization": f"Bearer {_token(admin_key)}"}
        op_headers = {
            "Authorization": f"Bearer {_token(op_key, sub='op-user', role=TenantRole.OPERATOR)}",
        }
        # Add the operator's key to the JWKS by mocking discovery again.
        r.reset()
        # Both keys need to be in JWKS to verify both tokens
        from authlib.jose import JsonWebKey

        admin_jwk = public_jwks(admin_key)
        op_jwk = public_jwks(op_key)
        jwks_combined: dict[str, Any] = {"keys": admin_jwk["keys"] + op_jwk["keys"]}
        mock_discovery_and_jwks(r, jwks_combined)

        # admin creates one
        created = client.post(
            "/api/v1/scheduler/triggers",
            json={
                "kind": "cron",
                "agent_definition_id": str(def_id),
                "cron_expr": "0 9 * * *",
            },
            headers=admin_headers,
        )
        assert created.status_code == 201, created.text

        # operator can list
        listed = client.get("/api/v1/scheduler/triggers", headers=op_headers)
        assert listed.status_code == 200
        assert len(listed.json()["triggers"]) == 1

        # operator cannot create
        rejected = client.post(
            "/api/v1/scheduler/triggers",
            json={
                "kind": "cron",
                "agent_definition_id": str(def_id),
                "cron_expr": "0 10 * * *",
            },
            headers=op_headers,
        )
        assert rejected.status_code == 403

        # operator cannot cancel
        trig_id = created.json()["id"]
        rejected_cancel = client.delete(f"/api/v1/scheduler/triggers/{trig_id}", headers=op_headers)
        assert rejected_cancel.status_code == 403

        # Use unused JsonWebKey import for clarity (keeps lint quiet).
        _ = JsonWebKey


@pytest.mark.asyncio
async def test_rest_operator_blocked_from_tenant_filter(client: TestClient) -> None:
    """An ``operator`` passing tenant_filter to list returns 403."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    op_key = make_rsa_keypair("kid-op-filter")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(op_key))
        op_headers = {
            "Authorization": f"Bearer {_token(op_key, sub='op-user', role=TenantRole.OPERATOR)}",
        }
        rejected = client.get(
            "/api/v1/scheduler/triggers",
            headers=op_headers,
            params={"tenant_filter": str(_TENANT_B)},
        )
        assert rejected.status_code == 403
        assert rejected.json()["detail"] == "tenant_filter_requires_tenant_admin"


@pytest.mark.asyncio
async def test_rest_cross_tenant_cancel_returns_404(client: TestClient) -> None:
    """Tenant A admin cannot cancel tenant B's trigger; 404 (not 403)."""
    def_id_b = await _seed_agent_definition(tenant_id=_TENANT_B, name="b-bot")
    a_admin_key = make_rsa_keypair("kid-a-adm")
    service = SchedulerAdminService()
    entry_b = await service.create(
        tenant_id=_TENANT_B,
        created_by_sub="b-admin",
        payload=ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.CRON,
            agent_definition_id=def_id_b,
            cron_expr="*/5 * * * *",
        ),
    )
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(a_admin_key))
        headers = {"Authorization": f"Bearer {_token(a_admin_key, tenant_id=_TENANT_A)}"}
        result = client.delete(f"/api/v1/scheduler/triggers/{entry_b.id}", headers=headers)
        assert result.status_code == 404
        assert result.json()["detail"] == "trigger_not_found"


@pytest.mark.asyncio
async def test_rest_unknown_agent_definition_returns_422(client: TestClient) -> None:
    """A POST with an unknown agent_definition_id returns 422."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    key = make_rsa_keypair("kid-422")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        bogus = uuid4()
        result = client.post(
            "/api/v1/scheduler/triggers",
            json={
                "kind": "cron",
                "agent_definition_id": str(bogus),
                "cron_expr": "*/5 * * * *",
            },
            headers=headers,
        )
        assert result.status_code == 422
        assert result.json()["detail"] == "agent_definition_not_found"


@pytest.mark.asyncio
async def test_rest_invalid_cron_expr_returns_422(client: TestClient) -> None:
    """A POST with an invalid cron expression is rejected at schema time."""
    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="invalid-cron")
    key = make_rsa_keypair("kid-invalid-cron")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        result = client.post(
            "/api/v1/scheduler/triggers",
            json={
                "kind": "cron",
                "agent_definition_id": str(def_id),
                "cron_expr": "this is not cron",
            },
            headers=headers,
        )
        assert result.status_code == 422


@pytest.mark.asyncio
async def test_rest_audit_row_is_written_on_create(client: TestClient) -> None:
    """A successful create produces an audit row with op_id='scheduler.create'."""
    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="audit-bot")
    key = make_rsa_keypair("kid-audit")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        created = client.post(
            "/api/v1/scheduler/triggers",
            json={
                "kind": "cron",
                "agent_definition_id": str(def_id),
                "cron_expr": "*/5 * * * *",
            },
            headers=headers,
        )
        assert created.status_code == 201
    rows = await _fetch_audit_rows()
    # op_id and op_class live in the JSON payload (the AuditMiddleware
    # binds them via structlog contextvars and renders the payload).
    scheduler_rows = [r for r in rows if r.payload.get("op_id") == "scheduler.create"]
    assert len(scheduler_rows) == 1
    assert scheduler_rows[0].payload.get("op_class") == "write"
    assert scheduler_rows[0].payload.get("trigger_kind") == "cron"


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def test_mcp_tools_registered() -> None:
    """The three meho.scheduler.* tools are registered."""
    # Importing the module triggers register_mcp_tool at module load.
    from meho_backplane.mcp.tools import scheduler as _scheduler_tools

    assert _scheduler_tools is not None
    assert get_tool("meho.scheduler.list") is not None
    assert get_tool("meho.scheduler.create") is not None
    assert get_tool("meho.scheduler.cancel") is not None


@pytest.mark.asyncio
async def test_mcp_create_dispatches_to_service() -> None:
    """meho.scheduler.create calls the service and returns trigger_id."""
    from meho_backplane.mcp.tools.scheduler import _create_handler  # type: ignore[attr-defined]

    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="mcp-bot")
    operator = Operator(
        sub="mcp-admin",
        raw_jwt="dummy",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.TENANT_ADMIN,
    )
    result = await _create_handler(
        operator,
        {
            "kind": "cron",
            "agent_definition_id": str(def_id),
            "cron_expr": "*/5 * * * *",
        },
    )
    assert "trigger_id" in result
    assert result["trigger"]["kind"] == "cron"


@pytest.mark.asyncio
async def test_mcp_cancel_returns_not_found_for_cross_tenant() -> None:
    """meho.scheduler.cancel against a cross-tenant id surfaces as not found."""
    from meho_backplane.mcp.server import McpInvalidParamsError
    from meho_backplane.mcp.tools.scheduler import _cancel_handler  # type: ignore[attr-defined]

    def_id_b = await _seed_agent_definition(tenant_id=_TENANT_B, name="x-mcp-bot")
    service = SchedulerAdminService()
    entry_b = await service.create(
        tenant_id=_TENANT_B,
        created_by_sub="b-admin",
        payload=ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.CRON,
            agent_definition_id=def_id_b,
            cron_expr="*/5 * * * *",
        ),
    )
    operator_a = Operator(
        sub="a-mcp-admin",
        raw_jwt="dummy",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.TENANT_ADMIN,
    )
    with pytest.raises(McpInvalidParamsError, match="trigger_not_found"):
        await _cancel_handler(operator_a, {"trigger_id": str(entry_b.id)})


# ---------------------------------------------------------------------------
# Durability test (the load-bearing AC)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_durability_trigger_survives_scheduler_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create a trigger via the admin surface, restart the scheduler loop,
    verify the trigger fires at its ``next_fire_at``.

    This is the floor-of-24/7-operation AC: a trigger created via the
    admin surface (the T5 path) must survive a process kill + restart
    and fire correctly on the next tick. The test exercises the full
    chain: admin surface insert -> persisted row -> loop restart ->
    claim_due_triggers selects the row -> fire path runs.

    Stubs:

    * ``AgentInvoker.run_scheduled`` is replaced with an AsyncMock so
      no real LLM / Keycloak round-trip happens. The fire path is
      proven by the mock's call_count.
    * ``_prepare_invocation`` is stubbed to short-circuit the
      definition + credentials lookup so the test does not need a
      real agent-secret env var or an enabled-definition state
      machine. The trigger's wall-clock state transitions are what
      this test asserts, not the credential resolution (which has
      its own tests in :mod:`test_scheduler_credentials`).
    """
    # Use the existing scheduler test helpers' tenant + agent seed.
    def_id = await _seed_agent_definition(tenant_id=_TENANT_A, name="durable-bot")
    service = SchedulerAdminService()

    # Create via the admin surface -- a one-off trigger in the past so
    # the next tick is guaranteed to fire it.
    past = datetime.now(UTC) - timedelta(minutes=1)
    entry = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=ScheduledTriggerCreate(
            kind=ScheduledTriggerKind.ONE_OFF,
            agent_definition_id=def_id,
            fire_at=past,
        ),
    )
    assert entry.next_fire_at is not None

    # Stub the invocation path so the test stays hermetic.
    from meho_backplane.scheduler import loop as scheduler_loop

    prepared = scheduler_loop._PreparedInvocation(  # type: ignore[attr-defined]
        name="durable-bot",
        identity_ref=f"agent:{_TENANT_A}-durable-bot",
        agent_client_id="dummy",
        agent_client_secret=SecretStr("dummy"),
        inputs_str="",
    )

    async def _stub_prepare(row: ScheduledTrigger) -> Any:
        return prepared

    monkeypatch.setattr(scheduler_loop, "_prepare_invocation", _stub_prepare)

    # Simulate a process kill + restart: start the scheduler task,
    # cancel it, then start a fresh one. The DB row must survive both
    # transitions and fire on the next tick.
    from meho_backplane.scheduler import start_scheduler, stop_scheduler

    monkeypatch.setenv("SCHEDULER_TICK_INTERVAL_SECONDS", "1")
    get_settings.cache_clear()

    # Stub the actual invoker so the fire path doesn't dispatch over
    # the network.
    fire_calls: list[uuid.UUID] = []

    async def _stub_dispatch(
        row: ScheduledTrigger,
        prep: Any,
        invoker: Any,
    ) -> bool:
        fire_calls.append(row.id)
        return True

    monkeypatch.setattr(scheduler_loop, "_dispatch_invocation", _stub_dispatch)

    # First start.
    task = start_scheduler()
    # Stop immediately to simulate the kill -- the row must still be
    # in the DB.
    await stop_scheduler(task)

    fetched = await service.get(_TENANT_A, entry.id)
    assert fetched is not None
    assert fetched.status == ScheduledTriggerStatus.ACTIVE
    assert fetched.next_fire_at is not None

    # Now restart the scheduler and run one tick deterministically.
    # The trigger's next_fire_at is in the past so it must fire.
    from meho_backplane.scheduler.loop import run_one_tick

    fires = await run_one_tick()
    # Exactly-once: the no-double-fire half of the Initiative #804 DoD
    # is meaningful only when this assertion is strict. `>= 1` would
    # silently pass a regression that double-dispatched the trigger.
    # Review m1 on PR #1128.
    assert fires == 1
    assert fire_calls.count(entry.id) == 1

    # After fire, the one-off row transitions to 'fired'.
    after = await service.get(_TENANT_A, entry.id)
    assert after is not None
    assert after.status == ScheduledTriggerStatus.FIRED

    # A second tick must not re-fire the now-FIRED trigger.
    second_tick_fires = await run_one_tick()
    assert second_tick_fires == 0
    assert fire_calls.count(entry.id) == 1
