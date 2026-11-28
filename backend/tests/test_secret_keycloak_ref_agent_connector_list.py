# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An agent whose connector list has no Keycloak gets no Keycloak detail from a ``keycloak:`` ref.

``secret.move`` and ``secret.handoff.export`` are sent as an agent with a
connector list that holds no Keycloak, and with a ``keycloak:`` ref that names
a realm other than the target's managed realm. Before approval, and in the
run after a person approves (the stored list is bound again there), no answer
and no stored card names the managed realm, the target, or its host, and
nothing is sent to Keycloak.

The Vault side is faked at the ``_build_client`` seam and Keycloak with respx.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from sqlalchemy import select

import meho_backplane.connectors.secret  # noqa: F401 -- registers the secret kinds
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret.handoff_ops import register_secret_handoff_operations
from meho_backplane.connectors.secret.ops import register_secret_broker_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._target_connector_limit import connector_limit
from meho_backplane.operations.approval_queue import (
    ApprovalRoleFloorError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c8")
_MANAGED_REALM = "managed-realm-x"
_WRONG_REALM = "another-realm-y"
_TARGET_NAME = "kc-agent-target"
_HOST = "kc-agent.test.invalid"
_ALIAS = "idp"
#: The connector list of the agent: no Keycloak in it.
_NO_KEYCLOAK = frozenset({"hostos-ssh-1.0"})
_VAULT_PLACE = f"vault:tenants/{_TENANT_ID}/app#password"
_WRONG_REF = f"keycloak:{_ALIAS}/{_WRONG_REALM}/operator-a#password"
_WRONG_CLIENT_REF = f"keycloak:{_ALIAS}/{_WRONG_REALM}/clients/launcher-app#secret"

_AGENT = Operator(
    sub="kc-agent",
    name=None,
    email=None,
    raw_jwt="fake.jwt.value",
    tenant_id=_TENANT_ID,
    tenant_role=TenantRole.OPERATOR,
    principal_kind=PrincipalKind.AGENT,
)
_ADMIN = Operator(
    sub="kc-admin",
    name=None,
    email=None,
    raw_jwt="fake.jwt.value",
    tenant_id=_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
    principal_kind=PrincipalKind.USER,
    web_session=True,
    platform_admin=True,
)

_CASES = pytest.mark.parametrize(
    ("connector_id", "op_id", "params"),
    [
        ("secret-broker-1.x", "secret.move", {"from": _VAULT_PLACE, "to": _WRONG_REF}),
        ("secret-broker-1.x", "secret.move", {"from": _WRONG_CLIENT_REF, "to": _VAULT_PLACE}),
        ("secret-broker-1.x", "secret.handoff.export", {"from": _WRONG_CLIENT_REF}),
    ],
    ids=["move-to-keycloak", "move-from-keycloak", "handoff-export"],
)


def _stub_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="made-up-admin")

    return _load()


@pytest.fixture(autouse=True)
async def _env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "operator-role")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    set_default_reducer(PassThroughReducer())
    await register_secret_broker_operations(embedding_service=embedding)
    await register_secret_handoff_operations(embedding_service=embedding)
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[_ALIAS],
                product="keycloak",
                host=_HOST,
                port=443,
                fqdn=None,
                secret_ref=f"example/{_TARGET_NAME}/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": _MANAGED_REALM},
                fingerprint={"version": "26.0.5"},
                notes="seeded by test_secret_keycloak_ref_agent_connector_list",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_stub_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield
    await connector.aclose()
    _CONNECTOR_INSTANCE_CACHE.pop(KeycloakConnector, None)
    get_settings.cache_clear()
    reset_dispatcher_caches()


@pytest.fixture
def keycloak() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        mock.route().respond(200, json={})
        yield mock


async def _everything(result: OperationResult) -> str:
    """All text of an answer, and of every stored approval row."""
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(ApprovalRequest))).scalars().all()
    stored = [{"effect": row.proposed_effect, "params": row.params} for row in rows]
    return json.dumps(
        {"result": result.model_dump(mode="json"), "stored": stored}, default=str
    ).lower()


def _names_nothing(text: str) -> None:
    for word in (_MANAGED_REALM, _TARGET_NAME, _HOST):
        assert word.lower() not in text, word


async def _send(connector_id: str, op_id: str, params: dict[str, Any]) -> OperationResult:
    with connector_limit(_NO_KEYCLOAK):
        return await dispatch(
            operator=_AGENT,
            connector_id=connector_id,
            op_id=op_id,
            target=None,
            params=params,
        )


@_CASES
async def test_before_approval_no_answer_names_the_realm_or_the_target(
    connector_id: str,
    op_id: str,
    params: dict[str, Any],
    keycloak: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch, secret={"password": "made-up-value-4711"})
    result = await _send(connector_id, op_id, params)
    _names_nothing(await _everything(result))
    assert keycloak.calls.call_count == 0


@_CASES
async def test_in_the_approved_run_no_answer_names_the_realm_or_the_target(
    connector_id: str,
    op_id: str,
    params: dict[str, Any],
    keycloak: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch, secret={"password": "made-up-value-4711"})
    first = await _send(connector_id, op_id, params)
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(ApprovalRequest))).scalars().all()
    assert first.status == "awaiting_approval", first
    assert len(rows) == 1
    async with get_sessionmaker()() as session:
        try:
            request = await approve_request(session, rows[0].id, operator=_ADMIN)
        except ApprovalRoleFloorError as refusal:
            # A request that cannot name its login is never approved: the
            # refusal names nothing of the realm or the target either.
            _names_nothing(str(refusal).lower())
            assert keycloak.calls.call_count == 0
            return
        await session.commit()
    # The run restores the stored connector list; no list is bound here.
    result = await resume_dispatch_after_approval(operator=_ADMIN, request=request, params=None)
    _names_nothing(await _everything(result))
    assert keycloak.calls.call_count == 0
