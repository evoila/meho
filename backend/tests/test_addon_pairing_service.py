# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`AddonPairingService` (#3025).

Coverage:

* pair provisions a **scoped** service principal — ``principal_kind=service``,
  ``kind_attribute=service``, ``tenant_role=read_only``, clientId
  ``addon:<name>`` — the "no blanket admin" proof, asserted against the
  recorded Keycloak admin transport.
* pair persists the negotiated contract + returns the one-time secret.
* pair -> unpair -> pair round-trip is reversible (unpair hard-deletes the
  row and the Keycloak client; the name frees up for re-pairing).
* a contract-version skew refuses the pairing **before** any Keycloak client
  is created.
* duplicate pair -> ``AddonAlreadyPairedError``; unpair of an absent add-on
  -> ``False``.
* heartbeat stamps ``last_seen_at``; heartbeat on an unpaired add-on raises.
* a failure after ``create_client`` rolls the just-created Keycloak client back.

The Keycloak admin client is monkey-patched so tests need no live Keycloak.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AddonPairing, Tenant
from meho_backplane.operations.addon_pairing import (
    AddonAlreadyPairedError,
    AddonNotPairedError,
    AddonPairingService,
)
from meho_backplane.operations.addon_pairing_contract import (
    BACKPLANE_CONTRACT_VERSION,
    ContractSkewError,
)
from meho_backplane.operations.addon_pairing_schemas import PairAddonRequest
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_KC_INTERNAL_ID = "cc000000-0000-0000-0000-00000000dead"
_PATCH_TARGET = "meho_backplane.operations.addon_pairing.KeycloakAdminClient.from_settings"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", "https://keycloak.test/admin/realms/meho")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "s3cr3t")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _mock_kc_ok(internal_id: str = _KC_INTERNAL_ID) -> MagicMock:
    """A mock KeycloakAdminClient factory that succeeds on create/secret/delete."""
    mock_client = AsyncMock()
    mock_client.create_client = AsyncMock(return_value=internal_id)
    mock_client.get_client_secret = AsyncMock(return_value="generated-secret")
    mock_client.delete_client = AsyncMock(return_value=None)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=mock_client)


async def _seed_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == _TENANT))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=_TENANT, slug="tenant-a", name="Tenant A"))
            await session.commit()


async def _fetch(tenant_id: uuid.UUID) -> list[AddonPairing]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = await session.execute(
            select(AddonPairing)
            .where(AddonPairing.tenant_id == tenant_id)
            .order_by(AddonPairing.name)
        )
        return list(rows.scalars().all())


def _request(name: str = "automation") -> PairAddonRequest:
    return PairAddonRequest(
        name=name,
        addon_contract_version=BACKPLANE_CONTRACT_VERSION,
        addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION,
    )


@pytest.mark.asyncio
async def test_pair_provisions_scoped_service_principal() -> None:
    """The paired principal is a read-only service client — no blanket admin."""
    await _seed_tenant()
    factory = _mock_kc_ok()
    mock_client = factory.return_value
    with patch(_PATCH_TARGET, factory):
        result = await AddonPairingService().pair(_TENANT, "op-admin", _request())

    mock_client.create_client.assert_awaited_once()
    kwargs = mock_client.create_client.await_args.kwargs
    assert kwargs["principal_kind"] == "service"
    assert kwargs["kind_attribute"] == "service"
    assert kwargs["tenant_role"] == "read_only"
    assert kwargs["client_id"] == "addon:automation"
    # One-time credentials + the negotiated contract come back to the caller.
    assert result.client_id == "addon:automation"
    assert result.client_secret == "generated-secret"
    assert result.negotiated_contract_version == BACKPLANE_CONTRACT_VERSION
    assert result.pairing.contract_version == BACKPLANE_CONTRACT_VERSION


@pytest.mark.asyncio
async def test_pair_persists_row() -> None:
    await _seed_tenant()
    with patch(_PATCH_TARGET, _mock_kc_ok()):
        await AddonPairingService().pair(_TENANT, "op-admin", _request())
    rows = await _fetch(_TENANT)
    assert len(rows) == 1
    assert rows[0].name == "automation"
    assert rows[0].keycloak_client_id == "addon:automation"
    assert rows[0].keycloak_internal_id == _KC_INTERNAL_ID
    assert rows[0].last_seen_at is None


@pytest.mark.asyncio
async def test_pair_unpair_pair_is_reversible() -> None:
    """Unpair hard-deletes; the name frees up so the add-on can pair again."""
    await _seed_tenant()
    factory = _mock_kc_ok()
    mock_client = factory.return_value
    service = AddonPairingService()
    with patch(_PATCH_TARGET, factory):
        await service.pair(_TENANT, "op-admin", _request())
        assert await service.unpair(_TENANT, "automation") is True
        mock_client.delete_client.assert_awaited_once_with(_KC_INTERNAL_ID)
        assert await _fetch(_TENANT) == []
        # Re-pair: the byte-identical unpaired state accepts the name again.
        await service.pair(_TENANT, "op-admin", _request())
    assert len(await _fetch(_TENANT)) == 1


@pytest.mark.asyncio
async def test_pair_contract_skew_never_provisions_keycloak() -> None:
    await _seed_tenant()
    factory = _mock_kc_ok()
    mock_client = factory.return_value
    payload = PairAddonRequest(
        name="automation",
        addon_contract_version=BACKPLANE_CONTRACT_VERSION,
        addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION + 1,
    )
    with patch(_PATCH_TARGET, factory), pytest.raises(ContractSkewError):
        await AddonPairingService().pair(_TENANT, "op-admin", payload)
    mock_client.create_client.assert_not_awaited()
    assert await _fetch(_TENANT) == []


@pytest.mark.asyncio
async def test_duplicate_pair_raises() -> None:
    await _seed_tenant()
    service = AddonPairingService()
    with patch(_PATCH_TARGET, _mock_kc_ok()):
        await service.pair(_TENANT, "op-admin", _request())
        with pytest.raises(AddonAlreadyPairedError):
            await service.pair(_TENANT, "op-admin", _request())


@pytest.mark.asyncio
async def test_unpair_absent_returns_false() -> None:
    await _seed_tenant()
    with patch(_PATCH_TARGET, _mock_kc_ok()):
        assert await AddonPairingService().unpair(_TENANT, "ghost") is False


@pytest.mark.asyncio
async def test_heartbeat_stamps_last_seen() -> None:
    await _seed_tenant()
    service = AddonPairingService()
    with patch(_PATCH_TARGET, _mock_kc_ok()):
        await service.pair(_TENANT, "op-admin", _request())
        entry = await service.heartbeat(_TENANT, "automation")
    assert entry.last_seen_at is not None
    rows = await _fetch(_TENANT)
    assert rows[0].last_seen_at is not None


@pytest.mark.asyncio
async def test_heartbeat_unpaired_raises() -> None:
    await _seed_tenant()
    with pytest.raises(AddonNotPairedError):
        await AddonPairingService().heartbeat(_TENANT, "ghost")


@pytest.mark.asyncio
async def test_secret_readback_failure_rolls_back_keycloak_client() -> None:
    """A failure after ``create_client`` deletes the client — no orphaned identity."""
    await _seed_tenant()
    factory = _mock_kc_ok()
    mock_client = factory.return_value
    mock_client.get_client_secret = AsyncMock(side_effect=RuntimeError("kc flap"))
    with patch(_PATCH_TARGET, factory), pytest.raises(RuntimeError):
        await AddonPairingService().pair(_TENANT, "op-admin", _request())
    mock_client.create_client.assert_awaited_once()
    mock_client.delete_client.assert_awaited_once_with(_KC_INTERNAL_ID)
    assert await _fetch(_TENANT) == []
