# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared set-up for the tests of the outer limit in the install's policy file.

Used by ``test_api_v1_policy_install_limits.py`` and
``test_api_v1_policy_install_limits_card.py``. Each test drives the
production ``meho_backplane.main:app``; the DB is the autouse per-test
SQLite + ``alembic upgrade head``.

:func:`console_approve` clicks Approve in the web console: a real console
sign-in (a signed token stored in a ``web_session`` row), the CSRF check,
``POST /ui/approvals/{request_id}/approve`` and the run it starts.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.policy.apply import (
    POLICY_APPLY_OP_ID,
    register_policy_apply_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, TenantPolicy
from meho_backplane.main import app
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.document import file_sha256
from meho_backplane.policy.limits import install_personal_agent_limit
from meho_backplane.policy.service import build_plan
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.session_store import create_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, mint_csrf_token
from meho_backplane.ui.routes.approvals import run_watch

from ._oidc_jwt_helpers import DEFAULT_TENANT_ID, mint_token
from ._vault_fakes import install_fake_vault

TENANT = uuid.UUID(DEFAULT_TENANT_ID)
CONNECTOR = "policy-registry-1.x"

FILE_P = "policy: v1\nclassifications:\n  - {match: {connector: inslim-1.x}, category: network}\n"
FILE_L = FILE_P + "limits: {personal_agent_up_to: dangerous}\n"

TENANT_SENTENCE = (
    "`limits` belongs to the install's policy file. Only a platform admin sets the "
    "outer limit; a tenant's file stays inside it."
)


@pytest.fixture(name="switch_on")
def switch_on_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CUSTOM_POLICY", "true")
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "observe")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(name="client")
def client_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    # The console answers when the run ends: the run is short, the wait is long.
    monkeypatch.setattr(run_watch, "ANSWER_WAIT_SECONDS", 30.0)
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


@pytest.fixture(name="embedding")
def embedding_fixture() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ran": True}


async def setup_ops(embedding: AsyncMock) -> None:
    await register_policy_apply_operation(embedding_service=embedding)
    for op_id, level in (("inslim.vm.list", "safe"), ("inslim.vm.restart", "caution")):
        await register_typed_operation(
            product="inslim",
            version="1.x",
            impl_id="inslim",
            op_id=op_id,
            handler=_handler,
            summary="Fixture operation.",
            description="Fixture operation.",
            parameter_schema={"type": "object", "properties": {}},
            group_key="compute",
            when_to_use="Fixture group.",
            safety_level=level,
            embedding_service=embedding,
        )


def token(key: Any, *, sub: str = "admin-a", platform_admin: bool = True, **claims: Any) -> str:
    claims.setdefault("tenant_role", TenantRole.TENANT_ADMIN.value)
    return mint_token(key, sub=sub, tenant_id=str(TENANT), platform_admin=platform_admin, **claims)


def post(client: TestClient, path: str, bearer: str, content: str, **params: str) -> Any:
    return client.post(
        f"/api/v1/policy/{path}",
        params=params,
        content=content.encode(),
        headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/yaml"},
    )


def export(client: TestClient, bearer: str) -> Any:
    return client.get(
        "/api/v1/policy/export",
        params={"scope": "install"},
        headers={"Authorization": f"Bearer {bearer}"},
    )


async def console_approve(
    client: TestClient, key: Any, request_id: uuid.UUID, *, sub: str, platform_admin: bool = True
) -> Any:
    """Click Approve in the web console as *sub*, after a fresh console sign-in."""
    signed_in = token(
        key,
        sub=sub,
        platform_admin=platform_admin,
        extra_claims={
            "auth_time": int(time.time()),
            "acr": "1",
            "sid": str(uuid.uuid4()),
            "azp": "meho-web",
        },
    )
    async with get_sessionmaker()() as session, session.begin():
        web = await create_session(
            session,
            operator_sub=sub,
            tenant_id=TENANT,
            access_token=signed_in,
            refresh_token="refresh-token",
            lifetime=timedelta(hours=1),
        )
    csrf = mint_csrf_token(str(web.id))
    client.cookies.set(SESSION_COOKIE_NAME, str(web.id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    try:
        return client.post(
            f"/ui/approvals/{request_id}/approve",
            data={"reason": "checked"},
            headers={CSRF_HEADER_NAME: csrf},
            follow_redirects=False,
        )
    finally:
        client.cookies.clear()


def person(
    sub: str,
    *,
    platform_admin: bool = True,
    web: bool = True,
    kind: PrincipalKind = PrincipalKind.USER,
) -> Operator:
    return Operator(
        sub=sub,
        tenant_id=TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=kind,
        platform_admin=platform_admin,
        raw_jwt="header.payload.signature",
        web_session=web,
        acr="1",
        auth_time=int(time.time()),
    )


async def stored_install() -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(TenantPolicy).where(TenantPolicy.tenant_id.is_(None)))
        ).scalar_one_or_none()
    return None if row is None else row.document


async def store_install(document: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(TenantPolicy(tenant_id=None, document=document, set_by_sub="fixture"))


async def requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


async def pending_requests() -> list[ApprovalRequest]:
    return [r for r in await requests() if r.status == "pending"]


async def audit_rows(method: str, path: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        return list(
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.method == method, AuditLog.path == path)
                    .order_by(AuditLog.occurred_at)
                )
            ).scalars()
        )


async def approve(request_id: uuid.UUID, approver: Operator) -> Any:
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=approver)
        await session.commit()
    return await resume_dispatch_after_approval(operator=approver, request=request)


async def limit() -> str:
    async with get_sessionmaker()() as session:
        return await install_personal_agent_limit(session, custom_policy=True)


async def apply_params(content: str) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session,
            scope="install",
            tenant_id=None,
            content=content.encode(),
            custom_policy=True,
        )
    return {
        "scope": "install",
        "file": content,
        "file_sha256": file_sha256(content.encode()),
        "based_on": plan.based_on,
    }


async def dispatch_apply(operator: Operator, content: str) -> Any:
    return await dispatch(
        operator=operator,
        connector_id=CONNECTOR,
        op_id=POLICY_APPLY_OP_ID,
        target=None,
        params=await apply_params(content),
    )
