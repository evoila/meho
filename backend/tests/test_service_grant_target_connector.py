# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A standing grant of a service login covers only targets of its own connector.

A grant names one connector. When the target of a call resolves to another
connector, the grant does not clear the call: it waits for a person's
approval. A target of the grant's own connector, and a target that resolves
no connector, keep their answers.

The connectors, the products and the targets here are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, ServicePrincipalGrant
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-0000000013a6")
_PRINCIPAL = "svc:grant-target-bot"
_OP = "alpha.restart"

#: The targets the handler ran on, so a test can prove what ran.
_RUNS: list[str] = []


class _Base(Connector):
    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError


class _AlphaConnector(_Base):
    product = "alpha"
    version = "1.0"
    impl_id = "alpha-api"


class _BetaConnector(_Base):
    product = "beta"
    version = "1.0"
    impl_id = "beta-api"


async def _restart(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    del params
    _RUNS.append(target.name)
    return {"restarted": True}


class _Target:
    def __init__(self, product: str) -> None:
        self.id = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = f"app-{product}-01"
        self.host = f"{product}.example.invalid"
        self.port = 443
        self.auth_model = "shared_service_account"
        self.product = product
        self.preferred_impl_id = None
        self.fingerprint = None
        self.version = None


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    _RUNS.clear()
    register_connector_v2(product="alpha", version="1.0", impl_id="alpha-api", cls=_AlphaConnector)
    register_connector_v2(product="beta", version="1.0", impl_id="beta-api", cls=_BetaConnector)
    yield
    reset_dispatcher_caches()
    clear_registry()
    _RUNS.clear()


def _service() -> Operator:
    return Operator(
        sub=_PRINCIPAL,
        name="svc",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


async def _register_op() -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_typed_operation(
        product="alpha",
        version="1.0",
        impl_id="alpha-api",
        op_id=_OP,
        handler=_restart,
        summary="Restart.",
        description="Restarts the service.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="caution",
        requires_approval=True,
        embedding_service=embedding,
    )


async def _seed_name_grant() -> uuid.UUID:
    """A grant of connector alpha-api-1.0 for every target whose name starts with ``app-``."""
    grant_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            ServicePrincipalGrant(
                id=grant_id,
                tenant_id=_TENANT,
                principal_sub=_PRINCIPAL,
                op_id=_OP,
                connector_id="alpha-api-1.0",
                target_id=None,
                target_product=None,
                target_name_pattern="app-*",
                reason="nightly restarts",
                created_by_sub="op-admin",
            )
        )
        await s.commit()
    return grant_id


async def _grant_use_rows(grant_id: uuid.UUID) -> int:
    async with get_sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.method == "APPROVAL", AuditLog.path == "approval.decision"
                    )
                )
            )
            .scalars()
            .all()
        )
    return sum(1 for r in rows if r.payload.get("grant_id") == str(grant_id))


async def _call(target: Any) -> Any:
    return await dispatch(
        operator=_service(),
        connector_id="alpha-api-1.0",
        op_id=_OP,
        target=target,
        params={},
    )


@pytest.mark.asyncio
async def test_grant_of_one_connector_does_not_clear_a_target_of_another() -> None:
    await _register_op()
    grant_id = await _seed_name_grant()
    result = await _call(_Target("beta"))
    assert result.status == "awaiting_approval", result.error
    assert _RUNS == []
    assert await _grant_use_rows(grant_id) == 0


@pytest.mark.asyncio
async def test_grant_clears_a_target_of_its_own_connector() -> None:
    await _register_op()
    grant_id = await _seed_name_grant()
    result = await _call(_Target("alpha"))
    assert result.status == "ok", result.error
    assert _RUNS == ["app-alpha-01"]
    assert await _grant_use_rows(grant_id) == 1


@pytest.mark.asyncio
async def test_target_with_no_connector_keeps_its_answer() -> None:
    await _register_op()
    await _seed_name_grant()
    result = await _call(_Target("gamma"))
    assert result.extras["error_code"] == "no_connector"
    assert _RUNS == []
