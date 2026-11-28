# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An import of an API spec never writes over the row of a typed operation.

A typed operation can have an id of the form ``METHOD:/path``, the same form
an import gives its operations. When an imported operation has the same key
as a typed row, the import writes nothing for it and names it in its answer.
The other operations of the spec are imported as usual. The grouping step
of an import sets a group only on rows that an import made.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect, select

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.ingest import (
    parse_openapi,
    register_ingested_operations,
    run_llm_grouping,
)
from meho_backplane.operations.ingest.register_ingested import RefusedOp
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings
from tests.fixtures.llm_groups import small_corpus

_CONNECTOR = {"product": "widgets", "version": "1.0", "impl_id": "widgets-rest"}
_SPEC_SOURCE = "widgets.yaml"
_COLUMNS = [attr.key for attr in inspect(EndpointDescriptor).column_attrs]

_SPEC = """
openapi: 3.0.3
info:
  title: Widgets
  version: "1.0"
paths:
  /widgets:
    get:
      operationId: listWidgets
      summary: List widgets from the spec
      responses:
        "200":
          description: The widgets.
    post:
      operationId: addWidget
      summary: Add a widget
      responses:
        "201":
          description: The new widget.
"""


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_registry()
    yield
    clear_registry()
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def typed_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _register_typed(op_id: str, embedding: AsyncMock) -> None:
    await register_typed_operation(
        **_CONNECTOR,
        op_id=op_id,
        handler=typed_handler,
        summary="List widgets from the typed code",
        description="Hand-written list of widgets.",
        parameter_schema={"type": "object", "properties": {}},
        when_to_use=None,
        tags=["typed"],
        safety_level="caution",
        requires_approval=True,
        hard_to_undo="stops",
        changes_rights=True,
        embedding_service=embedding,
    )


async def _row(op_id: str) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).filter_by(**_CONNECTOR, op_id=op_id, tenant_id=None)
            )
        ).scalar_one()
        return {name: getattr(row, name) for name in _COLUMNS}


async def _imported_ids() -> list[str]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(EndpointDescriptor.op_id)
            .filter_by(**_CONNECTOR, source_kind="ingested")
            .order_by(EndpointDescriptor.op_id)
        )
        return list(rows.scalars())


@pytest.mark.asyncio
async def test_an_import_refuses_an_operation_whose_key_is_a_typed_row(
    embedding: AsyncMock,
) -> None:
    await _register_typed("GET:/widgets", embedding)
    before = await _row("GET:/widgets")
    assert before["source_kind"] == "typed"

    operations = parse_openapi(_SPEC_SOURCE, spec_source=_SPEC_SOURCE, content=_SPEC)
    assert {op.op_id for op in operations} == {"GET:/widgets", "POST:/widgets"}

    for _ in range(2):
        result = await register_ingested_operations(
            **_CONNECTOR,
            spec_source=_SPEC_SOURCE,
            operations=operations,
            embedding_service=embedding,
        )
        assert result.refused_ops == (
            RefusedOp(
                op_id="GET:/widgets",
                reason=(
                    "The operation GET:/widgets was not imported: a typed operation "
                    "of this connector already has this id."
                ),
            ),
        )
        assert await _row("GET:/widgets") == before
        assert await _imported_ids() == ["POST:/widgets"]

    assert result.inserted_count + result.updated_count + result.skipped_count == 1


class _StubLlm:
    """Return canned answers in order and keep every prompt."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def generate_json(
        self, *, system_prompt: str, user_prompt: str, max_output_tokens: int
    ) -> str:
        self.prompts.append(user_prompt)
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_the_grouping_step_leaves_a_typed_rows_group_as_it_is(
    embedding: AsyncMock,
) -> None:
    # The stub answer puts this id into the "inventory" group.
    typed_id = "GET:/api/vcenter/vm"
    await _register_typed(typed_id, embedding)
    before = await _row(typed_id)
    assert before["group_id"] is None

    result = await register_ingested_operations(
        **_CONNECTOR,
        spec_source="vcenter.yaml",
        operations=small_corpus.PROTOS,
        embedding_service=embedding,
    )
    assert [refused.op_id for refused in result.refused_ops] == [typed_id]

    stub = _StubLlm([small_corpus.STUB_PROPOSE_RESPONSE, small_corpus.STUB_ASSIGNMENT_RESPONSE])
    grouping = await run_llm_grouping(
        llm_client=stub,
        operator_sub="op-test-typed-rows",
        operator_tenant_id=uuid.UUID("00000000-0000-0000-0000-000000007211"),
        **_CONNECTOR,
    )

    assert len(stub.prompts) == 2
    assert all(typed_id not in prompt for prompt in stub.prompts)
    assert grouping.operations_assigned == len(small_corpus.PROTOS) - 1
    assert await _row(typed_id) == before
