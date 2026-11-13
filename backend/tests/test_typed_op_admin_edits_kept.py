# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An admin's description and instructions of a typed operation stay after a start (#5321).

* an admin edit of ``custom_description`` or ``llm_instructions`` marks the field;
* typed registration at the next start leaves a marked field as it is,
  on the path that keeps the embedding and on the path that makes a new one;
* the embedding text holds the admin's description, so search finds the op by it;
* a change of the description in code still reaches the row;
* a field the admin did not edit keeps following the code.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings

_OP_ID = "widgets.item.read"
_CONNECTOR_ID = "widgets-1.x"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    return service


async def sample_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


def _admin() -> Operator:
    return Operator(
        sub="op-admin",
        name="Platform Admin",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )


async def _register(service: AsyncMock, **overrides: Any) -> None:
    kwargs: dict[str, Any] = {
        "product": "widgets",
        "version": "1.x",
        "impl_id": "widgets",
        "op_id": _OP_ID,
        "handler": sample_handler,
        "summary": "Read a widget.",
        "description": "Read one widget by id.",
        "parameter_schema": {"type": "object"},
        "group_key": "items",
        "when_to_use": "Use it to read widgets.",
        "tags": ["read-only"],
        "llm_instructions": {"notes": "code notes"},
        "embedding_service": service,
    }
    kwargs.update(overrides)
    await register_typed_operation(**kwargs)


async def _row() -> EndpointDescriptor:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP_ID)
        )
        return result.scalar_one()


async def _admin_edit(**fields: Any) -> None:
    await ReviewService(_admin()).edit_op(_CONNECTOR_ID, _OP_ID, tenant_id=None, **fields)


@pytest.mark.asyncio
async def test_edits_stay_when_the_code_is_unchanged(embedding_service: AsyncMock) -> None:
    await _register(embedding_service)
    await _admin_edit(custom_description="Admin words.", llm_instructions={"notes": "admin notes"})

    await _register(embedding_service)

    row = await _row()
    assert row.custom_description == "Admin words."
    assert row.llm_instructions == {"notes": "admin notes"}
    assert sorted(row.admin_edited_fields or []) == ["custom_description", "llm_instructions"]
    assert embedding_service.encode_one.call_count == 1


@pytest.mark.asyncio
async def test_edits_stay_and_search_text_keeps_them_when_the_code_text_changes(
    embedding_service: AsyncMock,
) -> None:
    await _register(embedding_service)
    await _admin_edit(custom_description="Admin words.", llm_instructions={"notes": "admin notes"})

    await _register(
        embedding_service,
        description="Read one widget by its id, new text.",
        llm_instructions={"notes": "new code notes"},
        custom_description="Code custom text.",
    )

    row = await _row()
    assert row.description == "Read one widget by its id, new text."
    assert row.custom_description == "Admin words."
    assert row.llm_instructions == {"notes": "admin notes"}
    assert embedding_service.encode_one.call_count == 2
    text_sent = embedding_service.encode_one.call_args_list[-1].args[0]
    assert "Admin words." in text_sent
    assert "Code custom text." not in text_sent
    assert "new text" in text_sent


@pytest.mark.asyncio
async def test_an_admin_clear_of_the_description_stays(embedding_service: AsyncMock) -> None:
    await _register(embedding_service, custom_description="Code custom text.")
    await _admin_edit(custom_description=None)

    await _register(embedding_service, custom_description="Code custom text.")

    row = await _row()
    assert row.custom_description is None
    assert row.admin_edited_fields == ["custom_description"]


@pytest.mark.asyncio
async def test_a_field_the_admin_did_not_edit_follows_the_code(
    embedding_service: AsyncMock,
) -> None:
    await _register(embedding_service)
    await _admin_edit(custom_description="Admin words.")

    await _register(embedding_service, llm_instructions={"notes": "new code notes"})

    row = await _row()
    assert row.custom_description == "Admin words."
    assert row.llm_instructions == {"notes": "new code notes"}
    assert row.admin_edited_fields == ["custom_description"]


@pytest.mark.asyncio
async def test_without_an_admin_edit_the_code_wins(embedding_service: AsyncMock) -> None:
    await _register(embedding_service, custom_description="Code custom text.")
    await _register(
        embedding_service,
        custom_description="New code custom text.",
        llm_instructions={"notes": "new code notes"},
    )

    row = await _row()
    assert row.custom_description == "New code custom text."
    assert row.llm_instructions == {"notes": "new code notes"}
    assert not row.admin_edited_fields
