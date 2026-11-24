# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval card of ``targets.register`` and ``targets.update`` shows the realm warning.

A call made with a token waits for a person's approval. The card holds the
realm warnings of the target as it will be after the write, so the person
sees them before approving. A call with no warning has a card without them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.targets.ops import (
    TARGETS_REGISTER_OP_ID,
    TARGETS_REGISTRY_CONNECTOR_ID,
    register_targets_registry_operations,
)
from meho_backplane.connectors.targets.update import (
    TARGETS_UPDATE_OP_ID,
    register_targets_update_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import DEFAULT_TENANT_ID
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._targets_helpers import _insert_target

_TENANT_ID = uuid.UUID(DEFAULT_TENANT_ID)
_KC_HOST = "93.184.216.34"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The test settings sign MEHO in with the realm "meho".
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()


@pytest_asyncio.fixture(autouse=True)
async def _registered_ops(_settings_env: None) -> AsyncIterator[None]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_targets_registry_operations(embedding_service=service)
    await register_targets_update_operation(embedding_service=service)
    yield


def _token_admin() -> Operator:
    """A tenant admin who calls with a token: the call waits for approval."""
    return Operator(
        sub="p-token",
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


async def _call(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    call = await call_operation(
        _token_admin(),
        {"connector_id": TARGETS_REGISTRY_CONNECTOR_ID, "op_id": op_id, "params": params},
    )
    assert call["status"] == "awaiting_approval", call
    return call


async def _card() -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        request = (await session.execute(select(ApprovalRequest))).scalars().one()
    assert request.proposed_effect is not None
    return dict(request.proposed_effect)


async def _seed_keycloak(realm: str, **extras: Any) -> uuid.UUID:
    row = await _insert_target(
        tenant_id=_TENANT_ID,
        name="kc-01",
        product="keycloak",
        host=_KC_HOST,
        secret_ref=None,
        extras={"managed_realm": realm, **extras},
    )
    return row.id


def _register_params(**extras: Any) -> dict[str, Any]:
    return {"name": "kc-new", "product": "keycloak", "host": _KC_HOST, "extras": extras}


async def test_register_with_the_admin_realm_shows_the_warning() -> None:
    await _call(TARGETS_REGISTER_OP_ID, _register_params(managed_realm="master"))

    [warning] = (await _card())["target_warnings"]
    assert "'master'" in warning
    assert "admin realm of this Keycloak" in warning


async def test_register_with_the_sign_in_realm_shows_the_warning() -> None:
    await _call(TARGETS_REGISTER_OP_ID, _register_params(managed_realm="meho"))

    [warning] = (await _card())["target_warnings"]
    assert "realm MEHO itself signs in with" in warning


async def test_register_names_a_custom_admin_realm_from_the_new_extras() -> None:
    await _call(
        TARGETS_REGISTER_OP_ID,
        _register_params(managed_realm="ops", admin_realm="ops"),
    )

    [warning] = (await _card())["target_warnings"]
    assert "admin realm of this Keycloak" in warning


@pytest.mark.parametrize(
    "params",
    [
        _register_params(managed_realm="example"),
        {"name": "ssh-new", "product": "ssh", "host": _KC_HOST},
        _register_params(managed_realm="../master"),
        _register_params(),
    ],
    ids=["plain-realm", "other-product", "bad-realm-name", "no-realm"],
)
async def test_register_with_no_warning_has_a_card_without_the_part(
    params: dict[str, Any],
) -> None:
    await _call(TARGETS_REGISTER_OP_ID, params)

    card = await _card()
    assert "target_warnings" not in card
    assert card["preview"]["action"] == TARGETS_REGISTER_OP_ID


async def test_update_that_changes_only_the_managed_realm_shows_the_warning() -> None:
    target_id = await _seed_keycloak("example")

    await _call(
        TARGETS_UPDATE_OP_ID,
        {"target_id": str(target_id), "extras": {"managed_realm": "meho"}},
    )

    [warning] = (await _card())["target_warnings"]
    assert "realm MEHO itself signs in with" in warning


async def test_update_of_another_field_warns_when_the_stored_realm_is_risky() -> None:
    target_id = await _seed_keycloak("master")

    await _call(TARGETS_UPDATE_OP_ID, {"target_id": str(target_id), "notes": "x"})

    [warning] = (await _card())["target_warnings"]
    assert "admin realm of this Keycloak" in warning


async def test_update_to_a_plain_realm_has_a_card_without_the_part() -> None:
    target_id = await _seed_keycloak("master")

    await _call(
        TARGETS_UPDATE_OP_ID,
        {"target_id": str(target_id), "extras": {"managed_realm": "example"}},
    )

    assert "target_warnings" not in await _card()


async def test_update_of_a_missing_target_has_a_card_without_the_part() -> None:
    await _call(
        TARGETS_UPDATE_OP_ID,
        {"target_id": str(uuid.uuid4()), "extras": {"managed_realm": "master"}},
    )

    assert "target_warnings" not in await _card()


async def test_the_card_holds_no_secret_value() -> None:
    target_id = await _seed_keycloak("example")

    await _call(
        TARGETS_UPDATE_OP_ID,
        {
            "target_id": str(target_id),
            "extras": {"managed_realm": "master", "password": "made-up-pass-1234"},
        },
    )

    card = await _card()
    assert card["target_warnings"]
    assert "made-up-pass-1234" not in str(card)
