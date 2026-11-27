# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared parts of the tests of a block of an agent by its name, through the real routes.

A made-up agent ``agent:sample-agent`` in tenant A, signed stand-in tokens
(the Keycloak stand-in answers discovery and keys), and a typed connector
stand-in with a safe read and a caution update. Each handler call is noted
in :data:`ran`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.kubernetes._wire_span import instrument_api_client
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal, EndpointDescriptor, Tenant
from meho_backplane.operations import DispatchChild, register_typed_operation
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION

from ._oidc_jwt_helpers import mint_token

TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000b6e1")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000b6e2")
AGENT = "agent:sample-agent"
AGENT_SUB = "7d2e4f10-0000-4000-8000-00000000b6e1"
AGENT_INTERNAL_ID = "sample-agent-client-internal-id"
CONNECTOR_ID = "vault-1.x"
READ_OP = "vault.sample.read"
UPDATE_OP = "vault.sample.update"
SEND_OP = "vault.sample.two_sends"
STEPS_OP = "vault.composite.sample_two_steps"
BLOCK_ROUTE = "/api/v1/principals/blocked"
READ_ROUTE = "/api/v1/connectors"
CALL_ROUTE = "/api/v1/operations/call"
KEYCLOAK_ADMIN = "https://keycloak.test/admin/realms/meho"
KEYCLOAK_TOKEN = "https://keycloak.test/realms/meho/protocol/openid-connect/token"

#: Each handler call, as ``(op_id, params)``.
ran: list[tuple[str, dict[str, Any]]] = []
#: Each request that reached the stand-in target of :data:`SEND_OP`.
sent: list[str] = []
#: What happens after the first send or step (set by each test).
after_first: list[Callable[[], Awaitable[None]]] = []


def clear() -> None:
    """Forget what the stand-ins noted."""
    ran.clear()
    sent.clear()
    after_first.clear()


class _Stub(Connector):
    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


async def _read(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    ran.append((READ_OP, dict(params)))
    return {"value": "read"}


async def _update(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    ran.append((UPDATE_OP, dict(params)))
    return {"value": "updated"}


class _Rest:
    async def request(self, method: str, url: str, **_kwargs: Any) -> str:
        sent.append(f"{method} {url}")
        return "ok"


async def _two_sends(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Two requests through the Kubernetes client wrapper, which checks the block list."""
    rest = instrument_api_client(SimpleNamespace(rest_client=_Rest())).rest_client  # type: ignore[arg-type]
    await rest.request("GET", "/one")
    for hook in after_first:
        await hook()
    await rest.request("POST", "/two")
    return {"value": "sent"}


async def sample_two_steps(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A multi-step operation: two reads through ``dispatch_child``."""
    first = await dispatch_child(connector_id=CONNECTOR_ID, op_id=READ_OP, params={"name": "1"})
    if first.status != "ok":
        return first
    for hook in after_first:
        await hook()
    return await dispatch_child(connector_id=CONNECTOR_ID, op_id=READ_OP, params={"name": "2"})


def _embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


async def register_ops() -> None:
    """Register the connector stand-in with :data:`READ_OP` (safe) and :data:`UPDATE_OP`."""
    register_connector_v2(product="vault", version="", impl_id="", cls=_Stub)
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    ops = ((READ_OP, _read, "safe"), (UPDATE_OP, _update, "caution"), (SEND_OP, _two_sends, "safe"))
    for op_id, handler, level in ops:
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=op_id,
            handler=handler,
            summary=f"Sample {op_id}.",
            description=f"Sample {op_id}.",
            parameter_schema=schema,
            when_to_use=None,
            safety_level=level,  # type: ignore[arg-type]
            embedding_service=_embedding(),
        )


async def register_steps_op() -> None:
    """Register :data:`STEPS_OP`, a multi-step operation that waits for approval."""
    async with get_sessionmaker()() as session:
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id=STEPS_OP,
                source_kind="composite",
                method=None,
                path=None,
                handler_ref="tests._agent_block_paths.sample_two_steps",
                summary="Two sample steps.",
                description="Two sample steps.",
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="caution",
                requires_approval=False,
                is_enabled=True,
                embedding=[0.0] * EMBEDDING_DIMENSION,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def seed(*, agent_tenant: uuid.UUID = TENANT_A, name: str = "sample-agent") -> None:
    """Both tenants and the agent ``agent:<name>`` in *agent_tenant*."""
    async with get_sessionmaker()() as session:
        for tenant, slug in ((TENANT_A, "sample-a"), (TENANT_B, "sample-b")):
            if await session.get(Tenant, tenant) is None:
                session.add(Tenant(id=tenant, slug=slug, name=slug))
        await session.flush()
        session.add(
            AgentPrincipal(
                tenant_id=agent_tenant,
                name=name,
                keycloak_client_id=f"agent:{name}",
                keycloak_internal_id=AGENT_INTERNAL_ID if name == "sample-agent" else name,
                owner_sub="owner-person",
                created_by_sub="owner-person",
            )
        )
        await session.commit()


def agent_token(key: Any, *, sub: str = AGENT_SUB, azp: str = AGENT) -> str:
    """A client-credentials token of the agent: the client id is in ``azp`` only."""
    return mint_token(
        key,
        sub=sub,
        name=None,
        email=None,
        tenant_id=str(TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
        principal_kind="agent",
        extra_claims={"azp": azp, "jti": str(uuid.uuid4())},
    )


def person_token(
    key: Any,
    sub: str,
    *,
    tenant: uuid.UUID = TENANT_A,
    platform_admin: bool | None = None,
    azp: str | None = None,
) -> str:
    """A person's token with ``tenant_admin`` and a fresh sign-in."""
    return mint_token(
        key,
        sub=sub,
        tenant_id=str(tenant),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        platform_admin=platform_admin,
        fresh_sign_in=True,
        extra_claims={"azp": azp} if azp else None,
    )


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def is_blocked(response: httpx.Response) -> bool:
    return response.status_code == 403 and "principal_blocked" in response.text


async def block(ac: httpx.AsyncClient, admin: str, sub: str = AGENT) -> httpx.Response:
    return await ac.post(BLOCK_ROUTE, json={"sub": sub, "reason": "stop it"}, headers=auth(admin))


async def call(ac: httpx.AsyncClient, token: str, op_id: str = READ_OP) -> httpx.Response:
    body = {"connector_id": CONNECTOR_ID, "op_id": op_id, "params": {"name": "one"}}
    return await ac.post(CALL_ROUTE, json=body, headers=auth(token))
