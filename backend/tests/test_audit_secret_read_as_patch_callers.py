# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two operations that store a new secret with the patch handler name the caller.

``vault.token.create_and_store`` and ``harbor.robot.create`` with a
``vault_target`` write the new secret with the ``vault.kv.patch`` handler.
hvac's ``patch`` reads the current item before it writes. The Vault
stand-in here runs hvac's own ``patch`` code, so the read really happens.

* The item exists: the read returns it, and the audit row names the caller.
* The item is missing: the read finds nothing, the handler creates the item,
  and the audit row names no login.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import hvac.exceptions
import pytest
import respx
from hvac.api.secrets_engines.kv_v2 import KvV2
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.harbor import HarborConnector
from meho_backplane.connectors.harbor.ops import register_harbor_robot_operations
from meho_backplane.connectors.harbor.session import HarborTargetLike
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vault import (
    VaultConnector,
    register_vault_identity_token_operations,
    register_vault_typed_operations,
)
from meho_backplane.connectors.vault.ops_token_store import TOKEN_STORE_OP
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

from ._vault_fakes import _FakeClient, install_fake_client

_TENANT = UUID("00000000-0000-0000-0000-0000000000d8")
_CALLER = "patch-caller-sub"
_KV_PATH = f"tenants/{_TENANT}/ci/store"
_HARBOR_OP = "harbor.robot.create"
_HARBOR_TARGET = "harbor-patch-caller"
_HARBOR_HOST = "harbor.test.invalid"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    register_connector_v2(
        product=HarborConnector.product,
        version=HarborConnector.version,
        impl_id=HarborConnector.impl_id,
        cls=HarborConnector,
    )
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _operator() -> Operator:
    return Operator(
        sub=_CALLER,
        name=None,
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _install_reading_patch(monkeypatch: pytest.MonkeyPatch, *, item_exists: bool) -> _FakeClient:
    """Install the Vault fake with hvac's own ``patch``: read first, then write."""
    fake = install_fake_client(monkeypatch, secret={"username": "svc", "password": "svc-pass"})
    fake.sys.default_capabilities = ["create", "read", "update", "patch"]
    kv_v2 = fake.secrets.kv.v2
    if not item_exists:
        kv_v2.read_exc = hvac.exceptions.InvalidPath("missing")

    def hvac_patch(path: str, secret: dict[str, Any], mount_point: str = "secret") -> Any:
        kv_v2.patch_calls.append({"path": path, "secret": secret, "mount_point": mount_point})
        return KvV2.patch(kv_v2, path=path, secret=secret, mount_point=mount_point)

    kv_v2.patch = hvac_patch  # type: ignore[method-assign]
    return fake


async def _row(op_id: str) -> AuditLog:
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
        )
    assert len(rows) == 1
    return rows[0]


def _assert_one_write(fake: _FakeClient, *, item_exists: bool) -> None:
    kv_v2 = fake.secrets.kv.v2
    assert len(kv_v2.read_calls) == 1
    (write,) = kv_v2.put_calls
    assert write["path"] == _KV_PATH
    assert write["cas"] == (kv_v2.version if item_exists else 0)


@pytest.fixture
async def harbor(embedding: AsyncMock) -> AsyncIterator[HarborConnector]:
    await register_harbor_robot_operations(embedding_service=embedding)
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT,
                name=_HARBOR_TARGET,
                aliases=[],
                product="harbor",
                version="2.11.0",
                host=_HARBOR_HOST,
                port=443,
                secret_ref=f"harbor/{_HARBOR_TARGET}",
                auth_model="shared_service_account",
            )
        )
        await session.commit()

    async def credentials(_target: HarborTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "admin", "password": "test-password"}

    connector = HarborConnector(credentials_loader=credentials)
    _CONNECTOR_INSTANCE_CACHE[HarborConnector] = connector
    yield connector
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("item_exists", "read_as"), [(True, _CALLER), (False, None)])
async def test_token_create_and_store_names_the_caller_of_the_patch_read(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock, item_exists: bool, read_as: str | None
) -> None:
    fake = _install_reading_patch(monkeypatch, item_exists=item_exists)
    fake.auth.token.minted_accessor = "fake-accessor-patch-caller"
    await register_vault_typed_operations(embedding_service=embedding)
    await register_vault_identity_token_operations(embedding_service=embedding)

    result = await dispatch(
        operator=_operator(),
        connector_id="vault-1.x",
        op_id=TOKEN_STORE_OP,
        target=None,
        params={"role_name": "ci-runner", "store": {"vault_kv": {"path": _KV_PATH}}},
        _approved=True,
    )

    assert result.status == "ok", result.error
    _assert_one_write(fake, item_exists=item_exists)
    assert (await _row(TOKEN_STORE_OP)).secret_read_as == read_as


@pytest.mark.asyncio
@pytest.mark.parametrize(("item_exists", "read_as"), [(True, _CALLER), (False, None)])
async def test_harbor_robot_store_names_the_caller_of_the_patch_read(
    monkeypatch: pytest.MonkeyPatch,
    harbor: HarborConnector,
    item_exists: bool,
    read_as: str | None,
) -> None:
    fake = _install_reading_patch(monkeypatch, item_exists=item_exists)
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT, _HARBOR_TARGET)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"https://{_HARBOR_HOST}/api/v2.0/robots").respond(
            201, json={"id": 7, "name": "robot$proj+bot", "secret": "made-up-robot-secret-0zzz"}
        )
        result = await dispatch(
            operator=_operator(),
            connector_id="harbor-rest-2.x",
            op_id=_HARBOR_OP,
            target=target,
            params={
                "name": "bot",
                "project": "proj",
                "duration": -1,
                "vault_target": f"vault:{_KV_PATH}#secret",
            },
            _approved=True,
        )

    assert result.status == "ok", result.error
    _assert_one_write(fake, item_exists=item_exists)
    assert (await _row(_HARBOR_OP)).secret_read_as == read_as
