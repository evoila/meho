# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The lock scope of the Keycloak writes that keep MEHO's access.

A run path that asks for the lock outside :func:`meho_access_scope` fails
before it counts or writes. The check before approval takes no lock. The
lock itself, on a real PostgreSQL, is tested in
``tests/integration/test_keycloak_meho_access_lock_pg.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from meho_backplane.connectors.keycloak.ops_meho_access_lock import (
    MehoAccessScopeError,
    confirm_meho_access_lock,
    hold_meho_access_lock,
    meho_access_check_only,
    meho_access_scope,
)
from meho_backplane.connectors.keycloak.ops_user_meho_access_guard import (
    refuse_last_admin_loss,
)
from meho_backplane.settings import get_settings

_REALM = "made-up-login-realm"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"https://keycloak.test/realms/{_REALM}")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_taking_the_lock_outside_a_scope_raises() -> None:
    with pytest.raises(MehoAccessScopeError):
        await hold_meho_access_lock(_REALM)
    with pytest.raises(MehoAccessScopeError):
        await confirm_meho_access_lock(_REALM)


async def test_a_guarded_write_outside_a_scope_fails_before_it_counts() -> None:
    counted: list[bool] = []

    async def _count() -> bool:
        counted.append(True)
        return False

    with pytest.raises(MehoAccessScopeError):
        await refuse_last_admin_loss("keycloak.user.disable", _REALM, "refused", _count)
    assert counted == []


async def test_the_check_before_approval_counts_without_a_lock() -> None:
    async def _count() -> bool:
        return False

    async with meho_access_check_only():
        await refuse_last_admin_loss("keycloak.user.disable", _REALM, "refused", _count)


async def test_a_write_in_its_scope_holds_and_confirms_the_lock() -> None:
    async def _count() -> bool:
        return False

    async with meho_access_scope():
        await refuse_last_admin_loss("keycloak.user.disable", _REALM, "refused", _count)
        await confirm_meho_access_lock(_REALM)


async def test_confirming_a_lock_the_scope_did_not_take_raises() -> None:
    async with meho_access_scope():
        with pytest.raises(MehoAccessScopeError):
            await confirm_meho_access_lock(_REALM)
