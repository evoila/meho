# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Conformance: a client-credential run is replayable as one tree (#3446).

A service principal that authenticates with client credentials and dispatches
over the REST chassis has no MCP session and no in-process agent loop. Before
#3446 its DISPATCH rows only got a session anchor when its clientId was a
paired add-on. Any other service principal landed ``agent_session_id = NULL``,
so ``GET /api/v1/audit/sessions/{id}/replay`` could not rebuild its run.

These tests drive the real ``call_operation`` seam (shared by the REST
``POST /api/v1/operations/call`` route and the MCP tool) as an *unpaired*
service principal, then replay the run through the real REST replay route.
One test goes through the REST route with a real bearer token that carries
only the ``azp`` claim (no ``service-account-<clientId>`` username marker),
so the clientId recovery in ``auth/jwt.py`` is exercised, not bypassed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.audit import router as audit_router
from meho_backplane.api.v1.operations import router as operations_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.addon_orchestration import ORCHESTRATION_METHOD
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT = uuid.UUID("55555555-5555-5555-5555-555555555555")
_WORK_REF = "gh:evoila/meho#3446"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    clear_registry()
    reset_dispatcher_caches()
    yield
    clear_registry()
    reset_dispatcher_caches()
    get_settings.cache_clear()
    clear_jwks_cache()


class _StubConnector(Connector):
    product = "stub"
    version = "1.x"
    impl_id = "stub"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _echo_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    return {"ok": True}


async def _seed() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(Tenant(id=_TENANT, slug="tenant-cc", name="Tenant CC"))
        await session.commit()
    embedder = AsyncMock()
    embedder.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    embedder.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    embedder.dimension = EMBEDDING_DIMENSION
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    for op_id in ("stub.step_one", "stub.step_two"):
        await register_typed_operation(
            product="stub",
            version="1.x",
            impl_id="stub",
            op_id=op_id,
            handler=_echo_handler,
            summary="Stub op for client-credential replay tests.",
            description="Echo a result.",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=embedder,
        )


def _service_principal() -> Operator:
    """An unpaired client-credential principal (no ``addon_pairing`` row)."""
    return Operator(
        sub="svc-build-bot-sub",
        raw_jwt="",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
        client_id="svc-build-bot",
    )


async def _dispatch(op_id: str, work_ref: str | None) -> None:
    arguments: dict[str, Any] = {
        "connector_id": "stub-1.x",
        "op_id": op_id,
        "target": None,
        "params": {},
    }
    if work_ref is not None:
        arguments["work_ref"] = work_ref
    await call_operation(_service_principal(), arguments)


def _rest_dispatch(op_id: str, token: str, work_ref: str) -> None:
    """Dispatch over the real REST route with a bearer token."""
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(operations_router)
    resp = TestClient(app).post(
        "/api/v1/operations/call",
        json={
            "connector_id": "stub-1.x",
            "op_id": op_id,
            "target": None,
            "params": {},
            "work_ref": work_ref,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text


async def _dispatch_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.scalars(
            select(AuditLog).where(AuditLog.method == "DISPATCH").order_by(AuditLog.occurred_at)
        )
        return list(result.all())


def _replay(session_id: uuid.UUID) -> dict[str, Any]:
    app = FastAPI()
    app.include_router(audit_router)
    key = make_rsa_keypair("kid-cc")
    token = mint_token(
        key,
        sub="admin-1",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(_TENANT),
    )
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        resp = TestClient(app).get(
            f"/api/v1/audit/sessions/{session_id}/replay",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_client_credential_run_replays_as_one_tree() -> None:
    await _seed()

    await _dispatch("stub.step_one", _WORK_REF)
    await _dispatch("stub.step_two", _WORK_REF)

    rows = await _dispatch_rows()
    assert len(rows) == 2
    session_ids = {row.agent_session_id for row in rows}
    # Not silently NULL-anchored: both steps share one non-NULL session.
    assert None not in session_ids
    assert len(session_ids) == 1
    session_id = session_ids.pop()
    assert session_id is not None

    body = _replay(session_id)
    # Anchor row + the two dispatches, all carrying the session id.
    assert body["row_count"] == 3
    assert len(body["root"]) == 1
    root = body["root"][0]
    assert root["method"] == ORCHESTRATION_METHOD
    assert root["work_ref"] == _WORK_REF
    assert [child["op_id"] for child in root["children"]] == ["stub.step_one", "stub.step_two"]


async def test_rest_client_credential_token_without_username_marker_replays() -> None:
    """A token with ``azp`` but no username marker still groups into one tree.

    Keycloak stamps ``service-account-<clientId>`` on ``preferred_username``
    only when the client has the ``profile`` scope. A client-credentials
    token without it is still classified ``service`` (by token shape), but
    before #3446 its clientId was not recovered, so ``call_operation`` had
    no key to open an orchestration run and the rows stayed NULL-anchored.
    """
    await _seed()
    key = make_rsa_keypair("kid-cc-rest")
    token = mint_token(
        key,
        sub="svc-build-bot-sub",
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_TENANT),
        extra_claims={"azp": "svc-build-bot"},
    )

    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        _rest_dispatch("stub.step_one", token, _WORK_REF)
        _rest_dispatch("stub.step_two", token, _WORK_REF)

    rows = await _dispatch_rows()
    assert len(rows) == 2
    session_ids = {row.agent_session_id for row in rows}
    assert None not in session_ids
    assert len(session_ids) == 1
    session_id = session_ids.pop()
    assert session_id is not None

    body = _replay(session_id)
    assert body["row_count"] == 3
    assert len(body["root"]) == 1
    root = body["root"][0]
    assert root["method"] == ORCHESTRATION_METHOD
    assert root["work_ref"] == _WORK_REF
    assert [child["op_id"] for child in root["children"]] == ["stub.step_one", "stub.step_two"]


async def test_client_credential_dispatch_without_work_ref_has_no_session() -> None:
    # The work_ref names the run. Without one there is nothing to group by, so
    # the row stays NULL-anchored (an operator finds it by principal instead).
    await _seed()

    await _dispatch("stub.step_one", None)

    rows = await _dispatch_rows()
    assert len(rows) == 1
    assert rows[0].agent_session_id is None
