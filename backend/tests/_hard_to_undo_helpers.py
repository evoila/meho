# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared helpers for the tests of ``hard_to_undo`` as a delete sign.

A made-up connector, a typed operation of it, a target and a service login
that calls the operation over ``POST /api/v1/operations/call`` with a
signed token, so the real gate decides.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, RunApproval, Target
from meho_backplane.main import app
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-000000007056")
SERVICE = "svc:undo-bot"

#: Every run of a stand-in operation, so a test can prove what ran.
RUNS: list[dict[str, Any]] = []


class _StubConnector(Connector):
    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    del target
    RUNS.append(params)
    return {"done": True}


def start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sign-in settings, turn the switch off and clear every cache."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("CUSTOM_POLICY", raising=False)
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    finish()


def finish() -> None:
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    clear_registry()
    RUNS.clear()


def custom_policy(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", "true" if on else "false")
    get_settings.cache_clear()


def add_connector(product: str, version: str, impl_id: str) -> None:
    cls = type(
        f"_{product}Connector",
        (_StubConnector,),
        {"product": product, "version": version, "impl_id": impl_id},
    )
    register_connector_v2(product=product, version=version, impl_id=impl_id, cls=cls)


async def register_op(
    op_id: str,
    hard_to_undo: str | None,
    *,
    product: str = "undo",
    version: str = "1.0",
    impl_id: str = "undo-api",
    safety_level: str = "caution",
) -> None:
    """An operation that waits for approval, with no delete name, verb or tag."""
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_typed_operation(
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
        handler=_handler,
        summary="Change a widget.",
        description="Changes one widget of a made-up product.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level=safety_level,  # type: ignore[arg-type]
        requires_approval=True,
        hard_to_undo=hard_to_undo,  # type: ignore[arg-type]
        embedding_service=embedding,
    )


async def seed_target(name: str, product: str = "undo", version: str = "1.0") -> uuid.UUID:
    async with get_sessionmaker()() as session:
        target = Target(
            tenant_id=TENANT,
            name=name,
            aliases=[],
            product=product,
            host="widget.example.invalid",
            port=443,
            fqdn=None,
            secret_ref=None,
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": version} if version else {},
            notes="seeded by a hard_to_undo test",
        )
        session.add(target)
        await session.commit()
        return target.id


async def seed_run_approval(
    target_id: uuid.UUID, *, work_ref: str, connector_id: str, op_ids: tuple[str, ...]
) -> uuid.UUID:
    """A live run approval that lists the operations on the target, approved by a person."""
    approval_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            RunApproval(
                id=approval_id,
                tenant_id=TENANT,
                principal_sub=SERVICE,
                work_ref=work_ref,
                entries=[
                    {"connector_id": connector_id, "op_id": op_id, "target_id": str(target_id)}
                    for op_id in op_ids
                ],
                reason="test: change the widget in this run",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="person-approver",
            )
        )
    return approval_id


@asynccontextmanager
async def service_client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-hard-to-undo")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


async def call(
    client: tuple[httpx.AsyncClient, Any],
    *,
    connector_id: str,
    op_id: str,
    target: str,
    work_ref: str | None = None,
    sub: str = SERVICE,
) -> dict[str, Any]:
    """The service login calls the operation over REST, as a real caller does."""
    ac, key = client
    token = mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(TENANT),
        principal_kind="service",
        extra_claims={"client_id": "svc-undo-client"},
    )
    body: dict[str, Any] = {
        "connector_id": connector_id,
        "op_id": op_id,
        "target": target,
        "params": {},
    }
    if work_ref is not None:
        body["work_ref"] = work_ref
    response = await ac.post(
        "/api/v1/operations/call", json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def decision_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(AuditLog).where(AuditLog.path.in_(("approval.decision", "approval.request")))
        )
        return list(rows.scalars().all())


async def request_op_ids() -> list[str]:
    """The op ids of the waiting requests written so far."""
    return [r.payload.get("op_id") for r in await decision_rows() if r.path == "approval.request"]


async def run_approval_uses(approval_id: uuid.UUID) -> int:
    return len(
        [
            r
            for r in await decision_rows()
            if r.path == "approval.decision"
            and r.payload.get("run_approval_id") == str(approval_id)
        ]
    )
