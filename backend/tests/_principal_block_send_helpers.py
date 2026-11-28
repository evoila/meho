# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared pieces for the tests that block a login while its Vault call runs.

:func:`block_after_login` wraps a function that opens a Vault client. On the
chosen open it lets the Vault login finish, then writes a real block row for
the caller before the client is handed out. So the next Vault request of
that client is the one the block check must stop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import block_principal, reset_block_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant


async def add_tenant(tenant_id: UUID) -> None:
    """Add the tenant row the block row of a test belongs to."""
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[-8:]}", name="Block Test"))
        await session.commit()


async def block_the_caller(operator: Operator) -> None:
    """Block *operator*'s login in its tenant, as a tenant admin does."""
    admin = Operator(
        sub="admin-person",
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=operator.tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )
    await block_principal(admin, operator.sub, "left the team")
    reset_block_cache()


def block_after_login(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    name: str,
    operator: Operator,
    *,
    on_open: int = 1,
) -> list[int]:
    """Block *operator* right after the Vault login of open number *on_open*.

    Returns the list that counts the opens.
    """
    real = getattr(module, name)
    opens: list[int] = []

    @asynccontextmanager
    async def _open(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async with real(*args, **kwargs) as client:
            opens.append(len(opens) + 1)
            if len(opens) == on_open:
                await block_the_caller(operator)
            yield client

    monkeypatch.setattr(module, name, _open)
    return opens
