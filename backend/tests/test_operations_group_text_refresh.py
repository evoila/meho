# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Group texts written in code reach running installs (#4332).

:func:`~meho_backplane.operations.typed_register._resolve_or_create_group`
stores the hash of the code text it writes. At start-up, a row whose
text still matches that hash gets the new code text. A row an operator
edited keeps its text.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations.typed_register as typed_register
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationGroup
from meho_backplane.operations.typed_register import code_text_hash, register_typed_operation
from meho_backplane.settings import get_settings

_OLD_TEXT = "Use for KV reads. Old text from an earlier version."
_NEW_TEXT = "Use for KV reads and writes. New text from this version."
_OPERATOR_TEXT = "Our own text: only the team secrets live here."


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


async def _register(
    when_to_use: str, embedding_service: AsyncMock, *, op_id: str = "vault.kv.read"
) -> None:
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        handler=_handler,
        summary="Read a secret.",
        description="Read a secret.",
        parameter_schema={"type": "object"},
        group_key="kv",
        when_to_use=when_to_use,
        embedding_service=embedding_service,
    )


async def _group() -> OperationGroup:
    async with get_sessionmaker()() as fresh:
        return (
            await fresh.execute(
                select(OperationGroup).where(
                    OperationGroup.tenant_id.is_(None), OperationGroup.group_key == "kv"
                )
            )
        ).scalar_one()


async def _set_row(session: AsyncSession, **values: str | None) -> None:
    await session.execute(
        update(OperationGroup).where(OperationGroup.group_key == "kv").values(**values)
    )
    await session.commit()


@pytest.mark.asyncio
async def test_fresh_install_writes_code_text_and_hash(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(_NEW_TEXT, stub_embedding_service)

    group = await _group()
    assert group.when_to_use == _NEW_TEXT
    assert group.code_text_hash == code_text_hash(_NEW_TEXT)


@pytest.mark.asyncio
async def test_old_code_text_gets_new_code_text_at_start_up(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(_OLD_TEXT, stub_embedding_service)

    await _register(_NEW_TEXT, stub_embedding_service)

    group = await _group()
    assert group.when_to_use == _NEW_TEXT
    assert group.code_text_hash == code_text_hash(_NEW_TEXT)


@pytest.mark.asyncio
async def test_operator_edited_text_is_kept(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    await _register(_OLD_TEXT, stub_embedding_service)
    # What ``meho connector edit-group`` does: it changes the text only.
    await _set_row(session, when_to_use=_OPERATOR_TEXT)

    await _register(_NEW_TEXT, stub_embedding_service)
    await _register(_OLD_TEXT, stub_embedding_service)

    group = await _group()
    assert group.when_to_use == _OPERATOR_TEXT
    assert group.code_text_hash == code_text_hash(_OLD_TEXT)


@pytest.mark.asyncio
async def test_row_without_hash_and_unknown_text_is_kept(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """A row MEHO cannot prove it wrote (hash NULL) is never overwritten."""
    await _register(_OLD_TEXT, stub_embedding_service)
    await _set_row(session, when_to_use=_OPERATOR_TEXT, code_text_hash=None)

    await _register(_NEW_TEXT, stub_embedding_service)

    group = await _group()
    assert group.when_to_use == _OPERATOR_TEXT
    assert group.code_text_hash is None


@pytest.mark.asyncio
async def test_row_without_hash_holding_the_code_text_starts_to_refresh(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """A row that already holds the current code text gets its hash, then refreshes."""
    await _register(_OLD_TEXT, stub_embedding_service)
    await _set_row(session, code_text_hash=None)

    await _register(_OLD_TEXT, stub_embedding_service)
    assert (await _group()).code_text_hash == code_text_hash(_OLD_TEXT)

    await _register(_NEW_TEXT, stub_embedding_service)
    assert (await _group()).when_to_use == _NEW_TEXT


@pytest.mark.asyncio
async def test_first_text_of_a_registrar_pass_wins(
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ops that hand one group different texts do not flip the row at each start-up.

    The first text of the pass is the one a fresh install writes, so an
    upgraded install shows the same text.
    """

    async def registrar(*, embedding_service: Any = None) -> None:
        await _register(_NEW_TEXT, stub_embedding_service, op_id="vault.kv.read")
        await _register(_OLD_TEXT, stub_embedding_service, op_id="vault.kv.write")

    monkeypatch.setattr(typed_register, "_TYPED_OP_REGISTRARS", [registrar])

    await typed_register._run_registrars(embedding_service=stub_embedding_service)
    assert (await _group()).when_to_use == _NEW_TEXT

    await typed_register._run_registrars(embedding_service=stub_embedding_service)
    group = await _group()
    assert group.when_to_use == _NEW_TEXT
    assert group.code_text_hash == code_text_hash(_NEW_TEXT)
