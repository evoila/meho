# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end wiring of the effect-audit ingest into ``POST /gateway/{runner}/result``.

Proves mechanism 4's forward surface (#3193): a runner forwards its hash-chained
effect records alongside the command result over the real runner-plane endpoint,
under the real middleware + runner-scope guard.

* A **clean** report accepts the result AND ingests the effect chain in one
  transaction — the effect rows link to the mint audit row.
* A **tampered** report is refused ``409``: the whole submission rolls back (the
  command is *not* consumed, so the un-reported-mint alarm can still fire) and a
  durable quarantine security audit row is written.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select

from meho_backplane.api.v1.gateway import router as gateway_router
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    GatewayCommand,
    GatewayCommandStatus,
    RunnerEffectChain,
    RunnerPrincipal,
    Tenant,
)
from meho_backplane.gateway.effect_ingest import EFFECT_AUDIT_PATH, EFFECT_QUARANTINE_PATH
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.runner.effect_audit import EffectAuditChain, EffectAuditRecord
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_RUNNER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_RUNNER_NAME = "runner-eff-ep"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(gateway_router)
    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="https://testserver",
    ) as ac:
        yield ac


def _runner_token(key: object) -> str:
    return mint_token(
        key,
        sub="runner-sub",
        tenant_id=str(_TENANT),
        tenant_role="read_only",
        principal_kind="runner",
        runner_id=str(_RUNNER_ID),
    )


async def _seed() -> tuple[uuid.UUID, uuid.UUID]:
    """Seed tenant + runner principal + a delivered remote-write command.

    Returns ``(command_id, mint_audit_id)``.
    """
    command_id = uuid.uuid4()
    mint_audit_id = uuid.uuid4()
    now = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(Tenant(id=_TENANT, slug="tenant-eff", name="tenant-eff"))
        session.add(
            RunnerPrincipal(
                id=_RUNNER_ID,
                tenant_id=_TENANT,
                name=_RUNNER_NAME,
                keycloak_client_id=f"runner:{_RUNNER_NAME}:{_RUNNER_ID}",
                keycloak_internal_id=f"kc-{_RUNNER_ID}",
                owner_sub="op-admin",
                created_by_sub="op-admin",
                last_seen_at=now,
            )
        )
        session.add(
            AuditLog(
                id=mint_audit_id,
                operator_sub="op-admin",
                tenant_id=_TENANT,
                method="GATEWAY",
                path="gateway.command.mint",
                status_code=200,
                payload={},
            )
        )
        session.add(
            GatewayCommand(
                id=command_id,
                tenant_id=_TENANT,
                runner_id=_RUNNER_NAME,
                op_id="vmware.vm.tag.set",
                params={"tag": "prod"},
                enqueued_by_sub="op-admin",
                params_hash="ph-1",
                safety_level="caution",
                signature="sig-1",
                status=GatewayCommandStatus.DELIVERED.value,
                delivered_at=now,
                expires_at=now + timedelta(minutes=5),
                mint_audit_id=mint_audit_id,
            )
        )
        await session.commit()
    return command_id, mint_audit_id


def _records(tmp_path, command_id: uuid.UUID) -> list[EffectAuditRecord]:
    chain = EffectAuditChain(tmp_path / "chain", runner_id=_RUNNER_NAME)
    chain.record_intent(
        command_id=command_id.hex,
        op_id="vmware.vm.tag.set",
        params_hash="ph-1",
        signature="sig-1",
        target_scope="scope-1",
    )
    chain.record_outcome(
        command_id=command_id.hex,
        op_id="vmware.vm.tag.set",
        params_hash="ph-1",
        signature="sig-1",
        target_scope="scope-1",
        outcome="ok",
    )
    return chain.unforwarded()


def _as_json(records: list[EffectAuditRecord]) -> list[dict]:
    return [r.model_dump(mode="json") for r in records]


@pytest.mark.asyncio
async def test_clean_report_ingests_and_links_effect_chain(
    client: httpx.AsyncClient, tmp_path
) -> None:
    """A clean forward accepts the result and links the effect rows to the mint row."""
    command_id, mint_audit_id = await _seed()
    records = _records(tmp_path, command_id)

    key = make_rsa_keypair("kid-eff")
    headers = {"Authorization": f"Bearer {_runner_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = await client.post(
            f"/api/v1/gateway/{_RUNNER_NAME}/result",
            headers=headers,
            json={
                "command_id": str(command_id),
                "outcome": "succeeded",
                "result": {},
                "effect_records": _as_json(records),
            },
        )
    assert resp.status_code == 200, resp.text

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        effect_rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == EFFECT_AUDIT_PATH)))
            .scalars()
            .all()
        )
        command = await session.get(GatewayCommand, command_id)
        head = (
            await session.execute(
                select(RunnerEffectChain).where(RunnerEffectChain.runner_id == _RUNNER_NAME)
            )
        ).scalar_one()
    assert len(effect_rows) == 2
    assert all(r.parent_audit_id == mint_audit_id for r in effect_rows)
    assert command is not None and command.consumed_at is not None  # result accepted
    assert head.last_seq == 1


@pytest.mark.asyncio
async def test_tampered_report_is_refused_and_quarantined(
    client: httpx.AsyncClient, tmp_path
) -> None:
    """A tampered effect chain 409s, rolls back the result, and quarantines."""
    command_id, _ = await _seed()
    records = _records(tmp_path, command_id)
    # Tamper the outcome record's op_id without recomputing its record_hash.
    tampered = [records[0], records[1].model_copy(update={"op_id": "vmware.vm.destroy"})]

    key = make_rsa_keypair("kid-eff")
    headers = {"Authorization": f"Bearer {_runner_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = await client.post(
            f"/api/v1/gateway/{_RUNNER_NAME}/result",
            headers=headers,
            json={
                "command_id": str(command_id),
                "outcome": "succeeded",
                "result": {},
                "effect_records": _as_json(tampered),
            },
        )
    assert resp.status_code == 409, resp.text
    assert "effect_chain_tamper" in resp.text

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        command = await session.get(GatewayCommand, command_id)
        effect_rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == EFFECT_AUDIT_PATH)))
            .scalars()
            .all()
        )
        quarantine_rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == EFFECT_QUARANTINE_PATH)))
            .scalars()
            .all()
        )
    # The whole submission rolled back: the command is NOT consumed (so the
    # un-reported-mint alarm can still fire), and no effect rows were written.
    assert command is not None and command.consumed_at is None
    assert effect_rows == []
    # But the tamper is durably recorded as a security event.
    assert len(quarantine_rows) == 1
    assert quarantine_rows[0].payload["event_class"] == "security"
    assert quarantine_rows[0].payload["runner"] == _RUNNER_NAME
