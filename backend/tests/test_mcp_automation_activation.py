# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Paired-surface activation gate for the automation family (Task #3029).

The first surface gated on live **add-on pairing state** rather than a static
tenant capability. This file pins the acceptance contract end to end:

* the ``meho_automation_list`` meta-tool is **absent** from ``tools/list`` and
  **rejected** at ``tools/call`` while no paired, contract-healthy add-on
  advertises the ``automation`` meta-tool family — an unpaired backplane's
  listing is byte-identical to a build that never carried the family;
* it **appears** the moment such a pairing exists and its capability is
  declared, and its ``tools/call`` succeeds;
* it **disappears cleanly** when the add-on unpairs or drifts
  contract-incompatible, without any tool being re-registered.

The gate is the ``required_addon_family`` axis on ``ToolDefinition``, resolved
per request from :meth:`AddonCapabilityService.active_meta_tool_families`. The
Keycloak admin client is monkey-patched so tests need no live Keycloak (the
capability-service test's pattern).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, update

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AddonPairing, Tenant
from meho_backplane.mcp.handlers import handle_tools_call, handle_tools_list
from meho_backplane.mcp.registry import (
    ToolSurface,
    addon_family_active,
    get_tool,
    has_addon_gated_tools,
)
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations.addon_capability import AddonCapabilityService
from meho_backplane.operations.addon_capability_schemas import (
    CapabilityDeclaration,
    CapabilityKind,
    DeclareCapabilitiesRequest,
)
from meho_backplane.operations.addon_pairing import AddonPairingService
from meho_backplane.operations.addon_pairing_contract import BACKPLANE_CONTRACT_VERSION
from meho_backplane.operations.addon_pairing_schemas import PairAddonRequest
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_AUTOMATION_TOOL = "meho_automation_list"
_AUTOMATION_FAMILY = "automation"
_PATCH_TARGET = "meho_backplane.operations.addon_pairing.KeycloakAdminClient.from_settings"


@pytest.fixture(autouse=True)
def _keycloak_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Keycloak admin knobs the pairing service reads at settings time."""
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", "https://keycloak.test/admin/realms/meho")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "s3cr3t")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _mock_kc_ok(internal_id: str) -> MagicMock:
    mock_client = AsyncMock()
    mock_client.create_client = AsyncMock(return_value=internal_id)
    mock_client.get_client_secret = AsyncMock(return_value="generated-secret")
    mock_client.delete_client = AsyncMock(return_value=None)
    mock_client.get_service_account_user_id = AsyncMock(return_value="svc-account-uuid")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=mock_client)


async def _seed_operator_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        exists = (
            await session.execute(select(Tenant).where(Tenant.id == OPERATOR_TENANT_ID))
        ).scalar_one_or_none()
        if exists is None:
            session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-tenant", name="Operator Tenant"))
            await session.commit()


async def _pair(name: str = _AUTOMATION_FAMILY) -> None:
    """Pair *name* under the operator tenant (Keycloak mocked)."""
    request = PairAddonRequest(
        name=name,
        addon_contract_version=BACKPLANE_CONTRACT_VERSION,
        addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION,
    )
    with patch(_PATCH_TARGET, _mock_kc_ok(f"kc-{name}")):
        await AddonPairingService().pair(OPERATOR_TENANT_ID, "op-admin", request)


async def _declare_meta_tool_family(addon: str = _AUTOMATION_FAMILY) -> None:
    """Declare a ``meta_tool_family`` capability named after the automation family."""
    request = DeclareCapabilitiesRequest(
        capabilities=[
            CapabilityDeclaration(kind=CapabilityKind.META_TOOL_FAMILY, name=_AUTOMATION_FAMILY),
        ],
    )
    await AddonCapabilityService().declare(OPERATOR_TENANT_ID, addon, request)


async def _drive_contract_incompatible(name: str = _AUTOMATION_FAMILY) -> None:
    """Skew the pairing so its add-on floor exceeds this backplane's contract."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            update(AddonPairing)
            .where(AddonPairing.tenant_id == OPERATOR_TENANT_ID, AddonPairing.name == name)
            .values(addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION + 1)
        )
        await session.commit()


async def _unpair(name: str = _AUTOMATION_FAMILY) -> None:
    with patch(_PATCH_TARGET, _mock_kc_ok(f"kc-{name}")):
        await AddonPairingService().unpair(OPERATOR_TENANT_ID, name)


async def _activate_automation() -> None:
    """Seed the happy path: tenant + paired, contract-healthy add-on + declaration."""
    await _seed_operator_tenant()
    await _pair()
    await _declare_meta_tool_family()


async def _listed_names(*, scopes: frozenset[str] = frozenset()) -> list[str]:
    op = build_operator(scopes=scopes)
    result = await handle_tools_list(op, None)
    return [tool["name"] for tool in result["tools"]]


# ---------------------------------------------------------------------------
# Unit: the gate predicate + registry plumbing
# ---------------------------------------------------------------------------


def test_addon_family_active_predicate() -> None:
    """``addon_family_active`` is a pure membership test, fail-closed on empty."""
    assert addon_family_active(frozenset(), None) is True  # ungated tool
    assert addon_family_active(frozenset({_AUTOMATION_FAMILY}), _AUTOMATION_FAMILY) is True
    assert addon_family_active(frozenset(), _AUTOMATION_FAMILY) is False
    assert addon_family_active(frozenset({"other"}), _AUTOMATION_FAMILY) is False


def test_automation_tool_declares_the_addon_family_gate() -> None:
    """The registered tool carries ``required_addon_family`` and stays working-surface."""
    entry = get_tool(_AUTOMATION_TOOL)
    assert entry is not None
    defn, _handler = entry
    assert defn.required_addon_family == _AUTOMATION_FAMILY
    assert defn.surface is ToolSurface.WORKING
    assert has_addon_gated_tools() is True


def test_addon_family_dropped_from_wire_shape() -> None:
    """``to_wire`` never leaks the MEHO-internal ``required_addon_family`` field."""
    entry = get_tool(_AUTOMATION_TOOL)
    assert entry is not None
    defn, _handler = entry
    assert "required_addon_family" not in defn.to_wire()


# ---------------------------------------------------------------------------
# tools/list — appear on pair, absent otherwise
# ---------------------------------------------------------------------------


async def test_automation_absent_when_unpaired() -> None:
    """No pairing → the family is absent (byte-identical unpaired listing)."""
    names = await _listed_names()
    assert _AUTOMATION_TOOL not in names


async def test_automation_present_when_paired_and_healthy() -> None:
    """A paired, contract-healthy add-on advertising the family lists the tool."""
    await _activate_automation()
    names = await _listed_names()
    assert _AUTOMATION_TOOL in names


async def test_automation_absent_when_paired_but_contract_incompatible() -> None:
    """A paired but contract-skewed add-on deactivates the surface."""
    await _activate_automation()
    await _drive_contract_incompatible()
    names = await _listed_names()
    assert _AUTOMATION_TOOL not in names


async def test_automation_absent_when_family_not_declared() -> None:
    """Paired + healthy but advertising no ``automation`` family → still absent.

    Pairing alone does not activate the surface; the add-on must declare the
    ``meta_tool_family`` capability. Proves the gate keys on the advertised
    family name, not merely on the pairing's existence.
    """
    await _seed_operator_tenant()
    await _pair()
    names = await _listed_names()
    assert _AUTOMATION_TOOL not in names


async def test_automation_disappears_on_unpair() -> None:
    """Activate, confirm present, unpair, confirm the surface is gone."""
    await _activate_automation()
    assert _AUTOMATION_TOOL in await _listed_names()
    await _unpair()
    assert _AUTOMATION_TOOL not in await _listed_names()


# ---------------------------------------------------------------------------
# tools/call — the gate is enforced at invocation too
# ---------------------------------------------------------------------------


async def test_call_rejected_when_inactive() -> None:
    """Naming the tool directly is 403-class while the family is inactive.

    The handler must never run — knowing the name out-of-band cannot bypass the
    pairing gate. The rejection names the add-on family.
    """
    op = build_operator()
    with pytest.raises(McpInvalidParamsError) as exc:
        await handle_tools_call(op, {"name": _AUTOMATION_TOOL, "arguments": {}})
    message = str(exc.value).lower()
    assert "automation" in message
    assert exc.value.data == {
        "reason": "addon_family_inactive",
        "required_addon_family": _AUTOMATION_FAMILY,
    }


async def test_call_succeeds_when_active() -> None:
    """With the family active the call passes the gate and returns the surface."""
    await _activate_automation()
    op = build_operator()
    result: dict[str, Any] = await handle_tools_call(
        op, {"name": _AUTOMATION_TOOL, "arguments": {}}
    )
    providers = result["structuredContent"]["providers"]
    assert [p["addon"] for p in providers] == [_AUTOMATION_FAMILY]
    kinds = {surface["kind"] for surface in providers[0]["surfaces"]}
    assert CapabilityKind.META_TOOL_FAMILY.value in kinds
