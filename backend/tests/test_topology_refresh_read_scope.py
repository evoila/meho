# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A topology refresh follows the read scope of a service login.

A person can limit the targets a service login reads (its read scope).
A topology refresh reads the target through its connector, so MEHO checks
the scope first:

* a target outside the scope: 403 ``read_scope_denied`` on the REST route
  and in the console, the connector is not called, and the graph is not
  changed;
* a target inside the scope, or a service login without a scope: the
  refresh runs;
* a person is never limited by a scope row.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.api.v1.topology import router as topology_router
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.schemas import NodeHint, TopologyHints
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphNode, Target
from meho_backplane.operations.service_read_scope_schemas import (
    ReadScopeEntry,
    ServiceReadScopeSet,
)
from meho_backplane.operations.service_read_scopes import ServiceReadScopeService
from meho_backplane.settings import get_settings
from meho_backplane.topology.refresh import refresh_target_topology
from meho_backplane.topology.refresh_read_scope import RefreshReadScopeDeniedError

from ._oidc_jwt_helpers import AUDIENCE, ISSUER, make_rsa_keypair, mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks
from .test_ui_agent_grants import (
    _authenticated_client,
    _bff_env,  # noqa: F401  (autouse fixture)
    _csrf_headers,
    _make_keypair_and_jwks,
    _seed_session_sync,
)

_TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_SERVICE = "svc:inventory-bot"
_PERSON = "op-alice"
_PRODUCT = "scopetopo"


class _CountingConnector(Connector):
    """A connector whose ``discover_topology`` counts its calls."""

    product = _PRODUCT
    calls: ClassVar[int] = 0

    async def fingerprint(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def execute(
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def discover_topology(self, target: Any) -> TopologyHints:
        type(self).calls += 1
        return TopologyHints(
            discovered_at=datetime.now(UTC),
            nodes=(NodeHint(kind="vm", name=f"vm-of-{target.name}"),),
        )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    _CountingConnector.calls = 0
    with patch(
        "meho_backplane.topology.refresh.resolve_connector", return_value=_CountingConnector
    ):
        yield
    get_settings.cache_clear()
    clear_jwks_cache()


async def _register_target(name: str) -> Target:
    async with get_sessionmaker()() as session:
        target = Target(
            id=uuid.uuid4(),
            tenant_id=_TENANT_ID,
            name=name,
            aliases=[],
            product=_PRODUCT,
            host=f"{name}.example.test",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
    return target


async def _scope_to(sub: str, target: Target) -> None:
    await ServiceReadScopeService().set(
        _TENANT_ID,
        sub,
        "op-admin",
        ServiceReadScopeSet(entries=[ReadScopeEntry(target_id=target.id)]),
    )


async def _node_count() -> int:
    async with get_sessionmaker()() as session:
        return int(
            (await session.execute(select(func.count()).select_from(GraphNode))).scalar_one()
        )


def _operator(sub: str, kind: PrincipalKind) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


# ---------------------------------------------------------------------------
# The refresh service
# ---------------------------------------------------------------------------


async def test_a_target_outside_the_scope_is_refused_before_the_connector() -> None:
    inside = await _register_target("inside")
    outside = await _register_target("outside")
    await _scope_to(_SERVICE, inside)

    with pytest.raises(RefreshReadScopeDeniedError) as caught:
        await refresh_target_topology(outside, _operator(_SERVICE, PrincipalKind.SERVICE))

    assert caught.value.reason.startswith("read_scope_denied:")
    assert "outside" in caught.value.reason
    assert _CountingConnector.calls == 0
    assert await _node_count() == 0


async def test_a_target_inside_the_scope_refreshes() -> None:
    inside = await _register_target("inside")
    await _scope_to(_SERVICE, inside)

    result = await refresh_target_topology(inside, _operator(_SERVICE, PrincipalKind.SERVICE))

    assert result.added_nodes == 1
    assert _CountingConnector.calls == 1


async def test_a_service_login_without_a_scope_refreshes() -> None:
    target = await _register_target("any")

    result = await refresh_target_topology(target, _operator(_SERVICE, PrincipalKind.SERVICE))

    assert result.added_nodes == 1
    assert _CountingConnector.calls == 1


async def test_a_scope_of_another_service_login_does_not_apply() -> None:
    inside = await _register_target("inside")
    outside = await _register_target("outside")
    await _scope_to("svc:other-bot", inside)

    result = await refresh_target_topology(outside, _operator(_SERVICE, PrincipalKind.SERVICE))

    assert result.added_nodes == 1


async def test_a_person_is_not_limited_by_a_scope_row() -> None:
    inside = await _register_target("inside")
    outside = await _register_target("outside")
    await _scope_to(_PERSON, inside)

    result = await refresh_target_topology(outside, _operator(_PERSON, PrincipalKind.USER))

    assert result.added_nodes == 1
    assert _CountingConnector.calls == 1


# ---------------------------------------------------------------------------
# The REST route
# ---------------------------------------------------------------------------


def _rest_refresh(target_name: str, *, sub: str, kind: str | None) -> Any:
    app = FastAPI()
    app.include_router(topology_router)
    key = make_rsa_keypair(f"kid-{sub}")
    token = mint_token(
        key,
        sub=sub,
        tenant_id=str(_TENANT_ID),
        tenant_role=TenantRole.OPERATOR.value,
        principal_kind=kind,
    )
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        return TestClient(app).post(
            f"/api/v1/topology/refresh/{target_name}",
            headers={"Authorization": f"Bearer {token}"},
        )


def test_rest_refresh_outside_the_scope_is_403() -> None:
    inside = asyncio.run(_register_target("inside"))
    asyncio.run(_register_target("outside"))
    asyncio.run(_scope_to(_SERVICE, inside))

    response = _rest_refresh("outside", sub=_SERVICE, kind="service")

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "read_scope_denied"
    assert detail["message"].startswith("read_scope_denied:")
    assert _CountingConnector.calls == 0
    assert asyncio.run(_node_count()) == 0


def test_rest_refresh_inside_the_scope_is_200() -> None:
    inside = asyncio.run(_register_target("inside"))
    asyncio.run(_scope_to(_SERVICE, inside))

    response = _rest_refresh("inside", sub=_SERVICE, kind="service")

    assert response.status_code == 200, response.text
    assert response.json()["added_nodes"] == 1
    assert _CountingConnector.calls == 1


def test_rest_refresh_by_a_person_is_200() -> None:
    inside = asyncio.run(_register_target("inside"))
    asyncio.run(_register_target("outside"))
    asyncio.run(_scope_to(_PERSON, inside))

    response = _rest_refresh("outside", sub=_PERSON, kind=None)

    assert response.status_code == 200, response.text
    assert _CountingConnector.calls == 1


# ---------------------------------------------------------------------------
# The console route
# ---------------------------------------------------------------------------


def _console_refresh(target_name: str, *, sub: str, kind: str | None) -> Any:
    keypair, jwks = _make_keypair_and_jwks()
    token = mint_token(
        keypair,
        sub=sub,
        tenant_id=str(_TENANT_ID),
        tenant_role=TenantRole.OPERATOR.value,
        principal_kind=kind,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_ID, access_token=token, operator_sub=sub)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        return client.post(f"/ui/topology/refresh/{target_name}", headers=_csrf_headers(csrf))
    finally:
        mock.stop()


def test_console_refresh_outside_the_scope_is_403() -> None:
    inside = asyncio.run(_register_target("inside"))
    asyncio.run(_register_target("outside"))
    asyncio.run(_scope_to(_SERVICE, inside))

    response = _console_refresh("outside", sub=_SERVICE, kind="service")

    assert response.status_code == 403, response.text
    assert response.json()["detail"].startswith("read_scope_denied:")
    assert _CountingConnector.calls == 0
    assert asyncio.run(_node_count()) == 0


def test_console_refresh_inside_the_scope_runs() -> None:
    inside = asyncio.run(_register_target("inside"))
    asyncio.run(_scope_to(_SERVICE, inside))

    response = _console_refresh("inside", sub=_SERVICE, kind="service")

    assert response.status_code == 200, response.text
    assert _CountingConnector.calls == 1
