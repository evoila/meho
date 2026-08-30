# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Service-layer tests for the service-principal standing-grant CRUD (#3151).

Exercises :class:`~meho_backplane.operations.service_grants.ServicePrincipalGrantService`
directly (no HTTP): create with the full create-time review (wildcard
refusal, delete-shaped refusal by pattern and by descriptor tag, past /
naive expiry, duplicate-active refusal), soft-delete revoke, and list
filtering (active-only vs full history).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000031a4")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000031b5")
_PRINCIPAL = "svc:deploy-bot"
_CONNECTOR = "vmware-rest-9.0"
_CREATOR = "op-admin"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _payload(
    *,
    op_id: str = "vmware.composite.vm.create",
    connector_id: str = _CONNECTOR,
    target_id: uuid.UUID | None = None,
    reason: str = "unattended substrate build",
    expires_at: datetime | None = None,
    principal_sub: str = _PRINCIPAL,
) -> ServiceGrantCreate:
    return ServiceGrantCreate(
        principal_sub=principal_sub,
        op_id=op_id,
        connector_id=connector_id,
        target_id=target_id,
        reason=reason,
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_create_returns_row_with_scopes() -> None:
    """A well-formed grant is created and echoes its exact scopes."""
    svc = ServicePrincipalGrantService()
    entry = await svc.create(_TENANT_ID, _CREATOR, _payload())
    assert entry.principal_sub == _PRINCIPAL
    assert entry.op_id == "vmware.composite.vm.create"
    assert entry.connector_id == _CONNECTOR
    assert entry.target_id is None
    assert entry.reason == "unattended substrate build"
    assert entry.created_by_sub == _CREATOR
    assert entry.revoked_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, payload_kwargs",
    [
        ("op_id", {"op_id": "*"}),
        ("op_id", {"op_id": "vmware.composite.vm.*"}),
        ("connector_id", {"connector_id": "vmware-*"}),
        ("principal_sub", {"principal_sub": "svc:*"}),
    ],
)
async def test_create_refuses_wildcards(field: str, payload_kwargs: dict[str, str]) -> None:
    """Any glob in op_id / connector_id / principal_sub is refused (#3151)."""
    svc = ServicePrincipalGrantService()
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, _CREATOR, _payload(**payload_kwargs))
    assert field in str(exc.value)
    assert "wildcard" in str(exc.value).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_id",
    [
        "DELETE:/vcenter/vm/{vm}",
        "vault.sys.policy.delete",
        "argocd.app.destroy",
        "keycloak.user.remove",
        "vault.token.purge",
    ],
)
async def test_create_refuses_delete_shaped_by_pattern(op_id: str) -> None:
    """Delete-shaped ops are never grantable — refused by configured pattern."""
    svc = ServicePrincipalGrantService()
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, _CREATOR, _payload(op_id=op_id))
    assert "delete-shaped" in str(exc.value)


@pytest.mark.asyncio
async def test_create_refuses_destructive_tagged_descriptor() -> None:
    """A non-delete-named op whose descriptor carries 'destructive' is refused.

    Belt-and-suspenders beyond the pattern set: the second, descriptor-level
    check catches a hand-tagged destructive typed op that the name globs miss.
    """
    # Seed a descriptor that does NOT match a delete-shaped name pattern but
    # is tagged destructive (vault-1.x → product/version/impl = vault//vault).
    async with get_sessionmaker()() as s:
        s.add(
            EndpointDescriptor(
                tenant_id=_TENANT_ID,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id="vault.sys.seal",
                source_kind="typed",
                method="POST",
                safety_level="dangerous",
                tags=["write", "destructive"],
                is_enabled=True,
            )
        )
        await s.commit()

    svc = ServicePrincipalGrantService()
    payload = _payload(op_id="vault.sys.seal", connector_id="vault-1.x")
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, _CREATOR, payload)
    assert "destructive" in str(exc.value)


@pytest.mark.asyncio
async def test_create_refuses_destructive_tier_descriptor() -> None:
    """An op whose descriptor is in the ``destructive`` safety tier (#3183) is
    refused at grant creation, even without a delete-shaped name or the
    ``destructive`` tag — the tier itself is the authoritative signal.
    """
    async with get_sessionmaker()() as s:
        s.add(
            EndpointDescriptor(
                tenant_id=_TENANT_ID,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id="vault.sys.rekey",
                source_kind="typed",
                method="POST",
                safety_level="destructive",
                tags=["write"],
                is_enabled=True,
            )
        )
        await s.commit()

    svc = ServicePrincipalGrantService()
    payload = _payload(op_id="vault.sys.rekey", connector_id="vault-1.x")
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, _CREATOR, payload)
    assert "destructive" in str(exc.value)


@pytest.mark.asyncio
async def test_create_refuses_past_and_naive_expiry() -> None:
    """A past or timezone-naive ``expires_at`` is refused."""
    svc = ServicePrincipalGrantService()
    with pytest.raises(GrantValidationError):
        await svc.create(
            _TENANT_ID, _CREATOR, _payload(expires_at=datetime.now(UTC) - timedelta(hours=1))
        )
    # A future but timezone-naive expiry is refused (must be tz-aware).
    naive_future = datetime(2999, 1, 1)
    with pytest.raises(GrantValidationError):
        await svc.create(_TENANT_ID, _CREATOR, _payload(expires_at=naive_future))


@pytest.mark.asyncio
async def test_create_refuses_duplicate_active_grant() -> None:
    """A second active grant for the same fully-scoped key is refused."""
    svc = ServicePrincipalGrantService()
    await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.vm.power"))
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.vm.power"))
    assert "already exists" in str(exc.value)


@pytest.mark.asyncio
async def test_revoke_is_soft_delete_and_allows_regrant() -> None:
    """Revoke stamps ``revoked_at`` (retains the row) and frees the key for re-grant."""
    svc = ServicePrincipalGrantService()
    op = "vmware.composite.vm.deploy"
    entry = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id=op))

    assert await svc.revoke(_TENANT_ID, entry.id, "op-revoker") is True
    revoked = await svc.get(_TENANT_ID, entry.id)
    assert revoked is not None
    assert revoked.revoked_at is not None
    assert revoked.revoked_by_sub == "op-revoker"

    # Re-revoking an already-revoked grant is a no-op → False (404 at the boundary).
    assert await svc.revoke(_TENANT_ID, entry.id, "op-revoker") is False

    # The scope is free again: a fresh grant for the same key is accepted.
    again = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id=op))
    assert again.id != entry.id


@pytest.mark.asyncio
async def test_revoke_absent_and_cross_tenant_returns_false() -> None:
    """Revoke of an absent / cross-tenant id returns False (no cross-tenant leak)."""
    svc = ServicePrincipalGrantService()
    entry = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.vm.clone"))
    assert await svc.revoke(_TENANT_ID, uuid.uuid4(), "x") is False
    assert await svc.revoke(_OTHER_TENANT, entry.id, "x") is False


@pytest.mark.asyncio
async def test_get_cross_tenant_returns_none() -> None:
    svc = ServicePrincipalGrantService()
    entry = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.host.list"))
    assert await svc.get(_OTHER_TENANT, entry.id) is None


@pytest.mark.asyncio
async def test_list_excludes_revoked_by_default() -> None:
    """``list_`` hides revoked grants unless ``include_revoked=True``."""
    svc = ServicePrincipalGrantService()
    live = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.snap.create"))
    gone = await svc.create(_TENANT_ID, _CREATOR, _payload(op_id="vmware.composite.snap.list"))
    await svc.revoke(_TENANT_ID, gone.id, "op-revoker")

    active = await svc.list_(_TENANT_ID, principal_sub=_PRINCIPAL)
    active_ids = {g.id for g in active}
    assert live.id in active_ids
    assert gone.id not in active_ids

    full = await svc.list_(_TENANT_ID, principal_sub=_PRINCIPAL, include_revoked=True)
    full_ids = {g.id for g in full}
    assert {live.id, gone.id} <= full_ids
