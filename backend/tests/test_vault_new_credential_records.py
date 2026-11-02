# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Where the new credential of a Vault mint op ends up.

* ``vault.token.create`` returns the accessor and the policies, never the
  token. The token is in none of these: the result (which is also the
  answer of the approve call), the audit row (``payload`` and
  ``raw_payload``), the stored approval result (``resume_result``) and the
  run record of an async approve (``operation_run.result``).
* ``vault.auth.approle.generate_secret_id`` returns the SecretID to the
  caller, but the audit row's raw copy has it replaced.

Vault is faked in-process (``tests/_vault_fakes.py``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vault import (
    register_vault_identity_token_operations,
    register_vault_typed_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, OperationRun
from meho_backplane.operations import dispatch
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.operation_run_service import OperationRunService
from meho_backplane.settings import get_settings

from ._vault_fakes import _FakeClient, install_fake_client

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000c7ed")
_TOKEN_OP = "vault.token.create"
_SECRET_ID_OP = "vault.auth.approle.generate_secret_id"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def _registered() -> AsyncIterator[None]:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_vault_typed_operations(embedding_service=embedding)
    await register_vault_identity_token_operations(embedding_service=embedding)
    yield


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    return install_fake_client(monkeypatch)


def _operator(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


async def _audit_blob() -> str:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return json.dumps([[row.payload, row.raw_payload] for row in rows], default=str)


async def _park_token_create() -> ApprovalRequest:
    parked = await dispatch(
        operator=_operator("requester"),
        connector_id="vault-1.x",
        op_id=_TOKEN_OP,
        target=None,
        params={"policies": ["default"], "ttl": "1h"},
    )
    assert parked.status == "awaiting_approval", parked
    request_id = uuid.UUID(parked.extras["approval_request_id"])
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=_operator("approver"))
        await session.commit()
    return request


async def _stored_resume_result(request_id: uuid.UUID) -> Any:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row.resume_result


async def test_token_create_returns_the_accessor_and_no_token(
    fake: _FakeClient, _registered: None
) -> None:
    fake.auth.token.minted_accessor = "fake-accessor-one"

    result = await dispatch(
        operator=_operator("requester"),
        connector_id="vault-1.x",
        op_id=_TOKEN_OP,
        target=None,
        params={"policies": ["default"]},
        _approved=True,
    )

    assert result.status == "ok", result.error
    assert result.result == {"accessor": "fake-accessor-one", "policies": ["default"]}
    token = fake.auth.token.minted_client_token
    assert token not in json.dumps(result.model_dump(mode="json"))
    blob = await _audit_blob()
    assert token not in blob
    assert "fake-accessor-one" in blob


async def test_an_approved_token_create_stores_no_token(
    fake: _FakeClient, _registered: None
) -> None:
    """The approve answer and the stored approval result hold no token."""
    request = await _park_token_create()

    answer: OperationResult = await resume_dispatch_after_approval(
        operator=_operator("approver"), request=request, params=None
    )

    assert answer.status == "ok", answer
    token = fake.auth.token.minted_client_token
    assert token not in json.dumps(answer.model_dump(mode="json"))
    assert token not in json.dumps(await _stored_resume_result(request.id), default=str)
    assert token not in await _audit_blob()


async def test_an_async_approved_token_create_keeps_no_token_in_the_run(
    fake: _FakeClient, _registered: None
) -> None:
    """With ``async: true`` on the approve call, the run record holds no token."""
    request = await _park_token_create()
    service = OperationRunService()

    run_id = await service.submit_approval_resume(_operator("approver"), request, params=None)
    await service._store[run_id].task

    async with get_sessionmaker()() as session:
        run = await session.get(OperationRun, run_id)
        assert run is not None
        run_result = run.result
    assert isinstance(run_result, dict)
    assert run_result["status"] == "ok", run_result
    assert run_result["result"]["accessor"] == fake.auth.token.minted_accessor
    token = fake.auth.token.minted_client_token
    assert token not in json.dumps(run_result)
    assert token not in json.dumps(await _stored_resume_result(request.id), default=str)
    assert token not in await _audit_blob()


async def test_a_new_secret_id_reaches_the_caller_but_not_the_audit_row(
    fake: _FakeClient, _registered: None
) -> None:
    fake.auth.approle.secret_id = "fake-secret-id-new"

    result = await dispatch(
        operator=_operator("requester"),
        connector_id="vault-1.x",
        op_id=_SECRET_ID_OP,
        target=None,
        params={"role_name": "ci"},
        _approved=True,
    )

    assert result.status == "ok", result.error
    assert result.result["secret_id"] == "fake-secret-id-new"
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    executed = [row for row in rows if row.payload.get("result_status") == "ok"]
    assert len(executed) == 1
    assert executed[0].raw_payload is not None
    assert "fake-secret-id-new" not in json.dumps(
        [executed[0].payload, executed[0].raw_payload], default=str
    )
