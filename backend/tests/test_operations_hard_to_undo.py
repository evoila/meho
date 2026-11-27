# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``hard_to_undo`` and ``changes_rights`` on an operation's own definition.

The rules: registration leaves out an operation that sets ``hard_to_undo`` on
a ``safe`` level, and writes and deletes nothing for it; it refuses any word
other than ``deletes``, ``stops`` and ``overwrites``; the stored row carries
both values, for a typed and a multi-step operation; and each start writes
them from the code again. An operation that sets ``changes_rights`` on a
``safe`` level is left out the same way
(``test_changes_rights_left_out_at_start.py``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import _left_out
from meho_backplane.operations.typed_register import (
    register_composite_operation,
    register_typed_operation,
)
from meho_backplane.settings import get_settings
from tests.fixtures.composites.handlers import composite_dispatch_child_handler

_PRODUCT, _VERSION, _IMPL = "undo-demo", "1.x", "undo-demo"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(_left_out, "_LEFT_OUT", frozenset())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


async def widget_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


async def _register(session: AsyncSession, embedding: AsyncMock, op_id: str, **policy: Any) -> None:
    await register_typed_operation(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL,
        op_id=op_id,
        handler=widget_handler,
        summary="Change a widget.",
        description="Changes one widget of a made-up product.",
        parameter_schema={"type": "object", "properties": {}},
        when_to_use=None,
        session=session,
        embedding_service=embedding,
        **policy,
    )
    await session.commit()


async def _row(session: AsyncSession, op_id: str) -> EndpointDescriptor | None:
    session.expire_all()
    result = await session.execute(
        select(EndpointDescriptor).where(
            EndpointDescriptor.product == _PRODUCT, EndpointDescriptor.op_id == op_id
        )
    )
    return result.scalar_one_or_none()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["deletes", "stops", "overwrites"])
async def test_the_stored_row_carries_both_fields(
    session: AsyncSession, embedding: AsyncMock, kind: str
) -> None:
    op_id = f"undo-demo.widget.{kind}"
    await _register(
        session,
        embedding,
        op_id,
        safety_level="caution",
        requires_approval=True,
        hard_to_undo=kind,
        changes_rights=True,
    )
    row = await _row(session, op_id)
    assert row is not None
    assert (row.hard_to_undo, row.changes_rights) == (kind, True)


@pytest.mark.asyncio
async def test_an_op_that_sets_neither_stores_no_and_no(
    session: AsyncSession, embedding: AsyncMock
) -> None:
    await _register(session, embedding, "undo-demo.widget.read")
    row = await _row(session, "undo-demo.widget.read")
    assert row is not None
    assert row.hard_to_undo is None
    assert not row.changes_rights


@pytest.mark.asyncio
async def test_registration_leaves_out_hard_to_undo_on_a_safe_op(
    session: AsyncSession, embedding: AsyncMock
) -> None:
    with structlog.testing.capture_logs() as logs:
        await _register(session, embedding, "undo-demo.widget.safe", hard_to_undo="deletes")
    assert await _row(session, "undo-demo.widget.safe") is None
    [line] = [e for e in logs if e["event"] == "operation_left_out"]
    assert line["op_id"] == "undo-demo.widget.safe"
    assert "cannot be hard to undo" in line["reason"]


@pytest.mark.asyncio
async def test_a_left_out_op_keeps_the_row_an_earlier_start_wrote(
    session: AsyncSession, embedding: AsyncMock
) -> None:
    op_id = "undo-demo.widget.purge"
    await _register(session, embedding, op_id)
    before = await _row(session, op_id)
    assert before is not None
    stored = {c.name: getattr(before, c.name) for c in before.__table__.columns}
    session.expunge_all()

    await _register(session, embedding, op_id, hard_to_undo="deletes")

    after = await _row(session, op_id)
    assert after is not None
    assert {c.name: getattr(after, c.name) for c in after.__table__.columns} == stored
    assert [op.op_id for op in _left_out.left_out_ops()] == [op_id]

    await _register(session, embedding, op_id)
    assert _left_out.left_out_ops() == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["removes", "DELETES", ""])
async def test_registration_refuses_an_unknown_word(
    session: AsyncSession, embedding: AsyncMock, word: str
) -> None:
    with pytest.raises(ValueError, match=r"undo-demo\.widget\.odd.*hard_to_undo must be one of"):
        await _register(
            session,
            embedding,
            "undo-demo.widget.odd",
            safety_level="dangerous",
            requires_approval=True,
            hard_to_undo=word,
        )
    assert await _row(session, "undo-demo.widget.odd") is None


@pytest.mark.asyncio
async def test_each_start_writes_both_fields_from_the_code_again(
    session: AsyncSession, embedding: AsyncMock
) -> None:
    op_id = "undo-demo.widget.reset"
    policy: dict[str, Any] = {"safety_level": "dangerous", "requires_approval": True}
    await _register(session, embedding, op_id, **policy, hard_to_undo="overwrites")
    await _register(session, embedding, op_id, **policy, changes_rights=True)
    row = await _row(session, op_id)
    assert row is not None
    assert (row.hard_to_undo, row.changes_rights) == (None, True)
    assert (row.safety_level, row.requires_approval) == ("dangerous", True)


@pytest.mark.asyncio
async def test_a_multi_step_op_carries_both_fields_and_leaves_out_a_safe_one(
    session: AsyncSession, embedding: AsyncMock
) -> None:
    common: dict[str, Any] = {
        "product": _PRODUCT,
        "version": _VERSION,
        "impl_id": _IMPL,
        "handler": composite_dispatch_child_handler,
        "summary": "Rebuild a widget.",
        "description": "Rebuilds one widget in two steps.",
        "parameter_schema": {"type": "object", "properties": {}},
        "when_to_use": None,
        "session": session,
        "embedding_service": embedding,
    }
    await register_composite_operation(
        op_id="undo-demo.composite.rebuild", hard_to_undo="stops", changes_rights=True, **common
    )
    await session.commit()
    row = await _row(session, "undo-demo.composite.rebuild")
    assert row is not None
    assert (row.source_kind, row.hard_to_undo, row.changes_rights) == ("composite", "stops", True)

    await register_composite_operation(
        op_id="undo-demo.composite.view",
        safety_level="safe",
        requires_approval=False,
        hard_to_undo="deletes",
        **common,
    )
    await session.commit()
    assert await _row(session, "undo-demo.composite.view") is None


@pytest.mark.asyncio
async def test_a_start_without_changes_rights_clears_a_stored_true(
    session: AsyncSession, embedding: AsyncMock
) -> None:
    op_id = "undo-demo.widget.grant"
    policy: dict[str, Any] = {"safety_level": "caution", "requires_approval": True}
    await _register(session, embedding, op_id, **policy, changes_rights=True)
    await _register(session, embedding, op_id, **policy)
    row = await _row(session, op_id)
    assert row is not None
    assert row.changes_rights is False


def _composite(op_id: str, **policy: Any) -> dict[str, Any]:
    return {
        "product": _PRODUCT,
        "version": _VERSION,
        "impl_id": _IMPL,
        "op_id": op_id,
        "handler": composite_dispatch_child_handler,
        "summary": "Rebuild a widget.",
        "description": "Rebuilds one widget in two steps.",
        "parameter_schema": {"type": "object", "properties": {}},
        "when_to_use": None,
        **policy,
    }


@pytest.mark.asyncio
async def test_a_multi_step_op_without_a_session_stores_both_fields(
    session: AsyncSession, embedding: AsyncMock
) -> None:
    op_id = "undo-demo.composite.wipe"
    await register_composite_operation(
        **_composite(op_id, hard_to_undo="deletes", changes_rights=True),
        embedding_service=embedding,
    )
    row = await _row(session, op_id)
    assert row is not None
    assert (row.source_kind, row.hard_to_undo, row.changes_rights) == ("composite", "deletes", True)


@pytest.mark.asyncio
async def test_a_multi_step_op_with_an_unknown_word_is_refused_and_writes_no_row(
    session: AsyncSession, embedding: AsyncMock
) -> None:
    op_id = "undo-demo.composite.odd"
    with pytest.raises(ValueError, match=r"undo-demo\.composite\.odd.*hard_to_undo must be one of"):
        await register_composite_operation(
            **_composite(op_id, hard_to_undo="removes"),
            session=session,
            embedding_service=embedding,
        )
    assert await _row(session, op_id) is None
