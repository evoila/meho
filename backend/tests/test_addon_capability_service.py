# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`AddonCapabilityService` (#3026).

Coverage:

* declare persists the surface set, stamped with the pairing's negotiated
  contract version (``declared_contract_version``).
* re-declare is replace-all — a dropped capability leaves no residue.
* declare on an unpaired add-on raises ``AddonNotPairedError``.
* ``active_capabilities`` returns only capabilities of contract-healthy
  pairings, and can scope to a single kind.
* activation flips with pairing health: a pairing driven contract-incompatible
  reads ``active=False`` and drops out of the tenant-wide activation view,
  without any capability row being deleted.
* unpair cascade-deletes the pairing's capability rows (no dead surfaces),
  proven under ``PRAGMA foreign_keys=ON``.
* the capability vocabulary is a single coordinated definition — the wire enum
  matches the model's CHECK-constraint tuple.

The Keycloak admin client is monkey-patched so tests need no live Keycloak.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select, update

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import ADDON_CAPABILITY_KINDS, AddonCapability, AddonPairing, Tenant
from meho_backplane.operations.addon_capability import AddonCapabilityService
from meho_backplane.operations.addon_capability_schemas import (
    CapabilityDeclaration,
    CapabilityKind,
    DeclareCapabilitiesRequest,
)
from meho_backplane.operations.addon_pairing import AddonNotPairedError, AddonPairingService
from meho_backplane.operations.addon_pairing_contract import BACKPLANE_CONTRACT_VERSION
from meho_backplane.operations.addon_pairing_schemas import PairAddonRequest
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_KC_INTERNAL_ID = "cc000000-0000-0000-0000-0000000cap00"
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


@pytest.fixture
def _fk_enforced(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Enforce SQLite FKs so ``ON DELETE CASCADE`` fires in this test.

    Mirrors the engine's opt-in gate: the per-test SQLite driver leaves FK
    enforcement off by default (production PG always enforces it), so a
    cascade-semantics test flips it on and rebuilds the engine.
    """
    monkeypatch.setenv("MEHO_SQLITE_FOREIGN_KEYS", "1")
    get_settings.cache_clear()
    reset_engine_for_testing()
    yield
    reset_engine_for_testing()
    get_settings.cache_clear()


def _mock_kc_ok(internal_id: str = _KC_INTERNAL_ID) -> MagicMock:
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


async def _pair(name: str = "automation") -> None:
    request = PairAddonRequest(
        name=name,
        addon_contract_version=BACKPLANE_CONTRACT_VERSION,
        addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION,
    )
    with patch(_PATCH_TARGET, _mock_kc_ok(f"{_KC_INTERNAL_ID[:-2]}{len(name):02d}")):
        await AddonPairingService().pair(_TENANT, "op-admin", request)


async def _capability_row_count(name: str) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        pairing_id = (
            await session.execute(
                select(AddonPairing.id).where(
                    AddonPairing.tenant_id == _TENANT, AddonPairing.name == name
                )
            )
        ).scalar_one()
        return (
            await session.execute(
                select(func.count())
                .select_from(AddonCapability)
                .where(AddonCapability.pairing_id == pairing_id)
            )
        ).scalar_one()


def _decl(*caps: tuple[CapabilityKind, str]) -> DeclareCapabilitiesRequest:
    return DeclareCapabilitiesRequest(
        capabilities=[CapabilityDeclaration(kind=k, name=n) for k, n in caps]
    )


def test_capability_vocabulary_is_a_single_coordinated_definition() -> None:
    """The wire enum and the model's CHECK-constraint tuple cannot drift."""
    assert tuple(kind.value for kind in CapabilityKind) == ADDON_CAPABILITY_KINDS


@pytest.mark.asyncio
async def test_declare_persists_and_stamps_contract_version() -> None:
    await _seed_tenant()
    await _pair()
    result = await AddonCapabilityService().declare(
        _TENANT,
        "automation",
        _decl(
            (CapabilityKind.META_TOOL_FAMILY, "inventory"),
            (CapabilityKind.EVENT_KIND, "run.step.completed"),
        ),
    )
    assert result.addon == "automation"
    assert result.active is True
    assert result.declared_contract_version == BACKPLANE_CONTRACT_VERSION
    assert {(c.kind, c.name) for c in result.capabilities} == {
        (CapabilityKind.META_TOOL_FAMILY, "inventory"),
        (CapabilityKind.EVENT_KIND, "run.step.completed"),
    }
    assert all(
        c.declared_contract_version == BACKPLANE_CONTRACT_VERSION for c in result.capabilities
    )
    assert await _capability_row_count("automation") == 2


@pytest.mark.asyncio
async def test_redeclare_is_replace_all() -> None:
    await _seed_tenant()
    await _pair()
    service = AddonCapabilityService()
    await service.declare(
        _TENANT,
        "automation",
        _decl(
            (CapabilityKind.META_TOOL_FAMILY, "inventory"),
            (CapabilityKind.CLI_VERB_FAMILY, "vm"),
        ),
    )
    # A smaller re-declaration drops "vm" — no dead surface left behind.
    result = await service.declare(
        _TENANT, "automation", _decl((CapabilityKind.META_TOOL_FAMILY, "inventory"))
    )
    assert [(c.kind, c.name) for c in result.capabilities] == [
        (CapabilityKind.META_TOOL_FAMILY, "inventory")
    ]
    assert await _capability_row_count("automation") == 1


@pytest.mark.asyncio
async def test_declare_on_unpaired_addon_raises() -> None:
    await _seed_tenant()
    with pytest.raises(AddonNotPairedError):
        await AddonCapabilityService().declare(
            _TENANT, "ghost", _decl((CapabilityKind.CONSOLE_PANEL, "pairing"))
        )


@pytest.mark.asyncio
async def test_list_declared_absent_is_none() -> None:
    await _seed_tenant()
    assert await AddonCapabilityService().list_declared(_TENANT, "ghost") is None


@pytest.mark.asyncio
async def test_active_capabilities_filters_by_health_and_kind() -> None:
    await _seed_tenant()
    await _pair("automation")
    await _pair("ssp")
    service = AddonCapabilityService()
    await service.declare(
        _TENANT,
        "automation",
        _decl(
            (CapabilityKind.META_TOOL_FAMILY, "inventory"),
            (CapabilityKind.EVENT_KIND, "run.step.completed"),
        ),
    )
    await service.declare(
        _TENANT, "ssp", _decl((CapabilityKind.EVENT_KIND, "portal.request.raised"))
    )

    everything = await service.active_capabilities(_TENANT)
    assert {(c.addon, c.kind, c.name) for c in everything} == {
        ("automation", CapabilityKind.META_TOOL_FAMILY, "inventory"),
        ("automation", CapabilityKind.EVENT_KIND, "run.step.completed"),
        ("ssp", CapabilityKind.EVENT_KIND, "portal.request.raised"),
    }

    event_kinds = await service.active_capabilities(_TENANT, kind=CapabilityKind.EVENT_KIND)
    assert {(c.addon, c.name) for c in event_kinds} == {
        ("automation", "run.step.completed"),
        ("ssp", "portal.request.raised"),
    }


@pytest.mark.asyncio
async def test_activation_flips_when_pairing_goes_contract_incompatible() -> None:
    await _seed_tenant()
    await _pair("automation")
    service = AddonCapabilityService()
    await service.declare(
        _TENANT, "automation", _decl((CapabilityKind.META_TOOL_FAMILY, "inventory"))
    )
    assert (await service.list_declared(_TENANT, "automation")).active is True
    assert len(await service.active_capabilities(_TENANT)) == 1

    # Simulate the add-on now requiring a newer backplane than we run (the
    # is_contract_compatible "backplane too old" drift direction).
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            update(AddonPairing)
            .where(AddonPairing.tenant_id == _TENANT, AddonPairing.name == "automation")
            .values(addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION + 1)
        )
        await session.commit()

    declaration = await service.list_declared(_TENANT, "automation")
    assert declaration.active is False
    # The declaration persists (health can recover) but contributes nothing
    # to the tenant-wide activation view while unhealthy.
    assert len(declaration.capabilities) == 1
    assert await service.active_capabilities(_TENANT) == []


@pytest.mark.asyncio
async def test_unpair_cascade_deletes_capabilities(_fk_enforced: None) -> None:
    await _seed_tenant()
    await _pair("automation")
    await AddonCapabilityService().declare(
        _TENANT,
        "automation",
        _decl(
            (CapabilityKind.META_TOOL_FAMILY, "inventory"),
            (CapabilityKind.CLI_VERB_FAMILY, "vm"),
        ),
    )
    assert await _capability_row_count("automation") == 2

    with patch(_PATCH_TARGET, _mock_kc_ok()):
        assert await AddonPairingService().unpair(_TENANT, "automation") is True

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        remaining = (
            await session.execute(select(func.count()).select_from(AddonCapability))
        ).scalar_one()
    assert remaining == 0
