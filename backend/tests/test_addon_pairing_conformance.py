# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Conformance seed for the add-on pairing contract (#3025).

The invariant the rest of Initiative #2900 builds on: an **unpaired**
backplane is byte-identical to a never-paired one, and pairing grows only an
operator-plane surface — never the agent's meta-tool waist (postulate 5).
This is the *seed*: it pins the two properties #3025 must hold, so a later
task that lights up the paired agent surface extends these assertions rather
than silently regressing the unpaired baseline.

* **Agent surface unchanged** — pairing registers no MCP meta-tool. The
  working / operator waist is what it was; an add-on identifier is *data*
  (``op_id`` / a row), never a tool name.
* **Unpaired health carries no pairing content** — with zero active
  pairings the ``/status`` pairings facet is an empty list.
* **A paired add-on surfaces its health** — including the both-direction
  contract-skew re-evaluation (the paired side of the seed).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select

import meho_backplane.main  # noqa: F401 — importing registers the full MCP tool surface
from meho_backplane.api.v1.health import _pairing_health
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AddonPairing, Tenant
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.operations.addon_pairing_contract import BACKPLANE_CONTRACT_VERSION
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def test_pairing_registers_no_agent_surface_tool() -> None:
    """The add-on pairing feature grows no MCP meta-tool (byte-identical waist)."""
    offenders = sorted(
        name for name in mcp_registry._TOOLS if "addon" in name.lower() or "pairing" in name.lower()
    )
    assert not offenders, (
        f"pairing must not register agent-surface tools, found: {offenders}. "
        "Add-on identifiers are data (op_id / rows), never tool names (postulate 5)."
    )


async def _seed_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == _TENANT))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=_TENANT, slug="tenant-a", name="Tenant A"))
            await session.commit()


async def _insert_pairing(*, name: str, addon_min_backplane_version: int) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AddonPairing(
                tenant_id=_TENANT,
                name=name,
                keycloak_client_id=f"addon:{name}",
                keycloak_internal_id="kc-internal",
                owner_sub="op-admin",
                contract_version=BACKPLANE_CONTRACT_VERSION,
                addon_contract_version=BACKPLANE_CONTRACT_VERSION,
                addon_min_backplane_version=addon_min_backplane_version,
                created_by_sub="op-admin",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_unpaired_health_has_empty_pairing_facet() -> None:
    """Zero active pairings -> the /status pairings facet is empty."""
    await _seed_tenant()
    assert await _pairing_health(_TENANT) == []


@pytest.mark.asyncio
async def test_paired_addon_surfaces_compatible_health() -> None:
    await _seed_tenant()
    await _insert_pairing(name="automation", addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION)
    facet = await _pairing_health(_TENANT)
    assert len(facet) == 1
    assert facet[0].addon == "automation"
    assert facet[0].contract_version == BACKPLANE_CONTRACT_VERSION
    assert facet[0].contract_compatible is True
    assert facet[0].last_seen is None


@pytest.mark.asyncio
async def test_paired_addon_health_flags_contract_skew() -> None:
    """A pairing whose add-on now out-requires this backplane reads incompatible."""
    await _seed_tenant()
    await _insert_pairing(name="ssp", addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION + 1)
    facet = await _pairing_health(_TENANT)
    assert len(facet) == 1
    assert facet[0].contract_compatible is False
