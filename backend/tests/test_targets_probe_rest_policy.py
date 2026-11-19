# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/targets/{name}/probe`` follows the per-target rules of ``targets.probe``.

An agent or service login that MEHO's policy refuses on the target gets 403
``denied``. The connector is not called and the stored detected product and
version stay as they were. An allowed caller probes as before.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.connectors.targets.probe import (
    TARGETS_PROBE_OP_ID,
    register_targets_probe_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, PermissionVerdict
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._targets_helpers import _build_app, _insert_target

_TENANT_ID = uuid.UUID(DEFAULT_TENANT_ID)
_PRODUCT = "proberest"
_AGENT = "agent-probe-rest"
_OLD_FINGERPRINT: dict[str, Any] = {
    "vendor": "example-vendor",
    "product": _PRODUCT,
    "version": "1.0.0",
    "reachable": True,
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()


@pytest_asyncio.fixture(autouse=True)
async def _registered_op(_settings_env: None) -> AsyncIterator[None]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_targets_probe_operation(embedding_service=service)
    yield


def _use_connector() -> list[str]:
    """Register a fake connector for ``_PRODUCT``; return the names it was asked about."""
    asked: list[str] = []

    class _FakeConnector(Connector):
        product = _PRODUCT

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            asked.append(str(target.name))
            return FingerprintResult(
                vendor="example-vendor",
                product=_PRODUCT,
                version="2.0.0",
                reachable=True,
                probed_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
                probe_method="version-endpoint",
            )

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector_v2(
        product=_PRODUCT, version="2.0", impl_id=f"{_PRODUCT}-rest", cls=_FakeConnector
    )
    return asked


async def _seed(name: str) -> TargetORM:
    return await _insert_target(
        tenant_id=_TENANT_ID,
        name=name,
        product=_PRODUCT,
        host=f"{name}.example.test",
        fingerprint=dict(_OLD_FINGERPRINT),
    )


async def _deny_on_target(principal_sub: str, target_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AgentPermission(
                tenant_id=_TENANT_ID,
                principal_sub=principal_sub,
                op_pattern=TARGETS_PROBE_OP_ID,
                target_scope=str(target_id),
                verdict=PermissionVerdict.DENY,
                created_by_sub="op-admin",
            )
        )
        await session.commit()


async def _stored_fingerprint(target_id: uuid.UUID) -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        row = await session.get(TargetORM, target_id)
    assert row is not None
    return row.fingerprint


def _rest_probe(name: str, *, sub: str, kind: str | None) -> Any:
    key = make_rsa_keypair("kid-probe-rest")
    token = mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=DEFAULT_TENANT_ID,
        principal_kind=kind,
    )
    client = TestClient(_build_app(web_session=False))
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        return client.post(
            f"/api/v1/targets/{name}/probe",
            headers={"Authorization": f"Bearer {token}"},
        )


@pytest.mark.parametrize("by", ["name", "id"])
async def test_an_agent_denied_on_the_target_gets_403_and_nothing_changes(by: str) -> None:
    asked = _use_connector()
    denied = await _seed("denied-system")
    await _deny_on_target(_AGENT, denied.id)

    response = _rest_probe(
        "denied-system" if by == "name" else str(denied.id), sub=_AGENT, kind="agent"
    )

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "denied"
    assert detail["op_id"] == TARGETS_PROBE_OP_ID
    assert "policy_denied" in detail["message"]
    assert "denied-system" in detail["message"]
    assert asked == []
    assert await _stored_fingerprint(denied.id) == _OLD_FINGERPRINT


async def test_the_same_agent_still_probes_a_target_it_is_not_denied_on() -> None:
    asked = _use_connector()
    denied = await _seed("denied-system")
    allowed = await _seed("allowed-system")
    await _deny_on_target(_AGENT, denied.id)

    response = _rest_probe("allowed-system", sub=_AGENT, kind="agent")

    assert response.status_code == 200, response.text
    assert response.json()["version"] == "2.0.0"
    assert asked == ["allowed-system"]
    stored = await _stored_fingerprint(allowed.id)
    assert stored is not None
    assert stored["version"] == "2.0.0"


@pytest.mark.parametrize("kind", ["service", None], ids=["service-login", "person"])
async def test_an_allowed_caller_still_probes(kind: str | None) -> None:
    asked = _use_connector()
    row = await _seed("open-system")

    response = _rest_probe("open-system", sub="caller-one", kind=kind)

    assert response.status_code == 200, response.text
    assert asked == ["open-system"]
    stored = await _stored_fingerprint(row.id)
    assert stored is not None
    assert stored["version"] == "2.0.0"
