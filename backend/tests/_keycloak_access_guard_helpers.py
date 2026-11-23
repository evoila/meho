# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A fixture for Keycloak tests that write users in MEHO's own login realm.

In that realm, MEHO counts the platform admins before a lock-out write
and before a group delete or attribute change (``ops_user_meho_access_guard``,
``ops_group_meho_access_guard``). The count reads the realm's groups and
members. Tests about other checks do not mock those reads, so they use
:func:`platform_admins_not_counted`: the write is never the one that takes
the last platform admin away. The checks for a login MEHO itself uses and
for the caller's own account still run. The count has its own tests in
``test_connectors_keycloak_meho_access_guard.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from meho_backplane.connectors.keycloak import (
    ops_group_meho_access_guard,
    ops_user_meho_access_guard,
)

__all__ = ["platform_admins_not_counted"]


@pytest.fixture
def platform_admins_not_counted(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Treat every write as one that leaves another platform admin."""

    async def _not_last(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(ops_user_meho_access_guard, "_loses_last_platform_admin", _not_last)
    monkeypatch.setattr(ops_group_meho_access_guard, "_loses_last_platform_admin", _not_last)
    yield
