# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An operation from an imported API spec has no "hard to undo" and no "changes rights" mark.

Only the code of a typed operation sets ``hard_to_undo`` and
``changes_rights``. An import of an API spec writes both as NULL: on a fresh
import, on a re-import (also over a value stored on the row before) and on
an import into one tenant. An import never changes a built-in typed row.

The tests run the real import: :func:`parse_openapi` reads a small OpenAPI
file and :func:`register_ingested_operations` writes the rows.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect, select, update

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import typed_register
from meho_backplane.operations.ingest import parse_openapi, register_ingested_operations
from meho_backplane.operations.service_grants import delete_grant_reason
from meho_backplane.operations.typed_register import clear_typed_op_snapshot
from meho_backplane.settings import get_settings
from tests.approval_table import register_all_descriptors

#: ``(product, version, impl_id, op_id)``.
RowKey = tuple[str, str, str, str]

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000007148")
_SPEC_SOURCE = "widgets.yaml"
_CONNECTOR = {"product": "widgets", "version": "1.0", "impl_id": "widgets-rest"}
_COLUMNS = [attr.key for attr in inspect(EndpointDescriptor).column_attrs]


def _spec(get_id: str, post_id: str, delete_id: str) -> str:
    """A small OpenAPI file with a ``GET``, a ``POST`` and a ``DELETE`` path."""
    return f"""
openapi: 3.0.3
info:
  title: Widgets
  version: "1.0"
paths:
  /widgets:
    get:
      operationId: {get_id}
      summary: List widgets
      responses:
        "200":
          description: The widgets.
    post:
      operationId: {post_id}
      summary: Add a widget
      responses:
        "201":
          description: The new widget.
  /widgets/{{widget_id}}:
    delete:
      operationId: {delete_id}
      summary: Remove a widget
      parameters:
        - name: widget_id
          in: path
          required: true
          schema:
            type: string
      responses:
        "204":
          description: Removed.
"""


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("CUSTOM_POLICY", "true")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _import(
    text: str,
    embedding: AsyncMock,
    *,
    tenant_id: uuid.UUID | None,
    connector: dict[str, str],
) -> None:
    operations = parse_openapi(_SPEC_SOURCE, spec_source=_SPEC_SOURCE, content=text)
    assert {op.method for op in operations} == {"GET", "POST", "DELETE"}
    await register_ingested_operations(
        **connector,
        spec_source=_SPEC_SOURCE,
        operations=operations,
        tenant_id=tenant_id,
        embedding_service=embedding,
    )


async def _rows(**where: Any) -> list[EndpointDescriptor]:
    async with get_sessionmaker()() as session:
        stmt = select(EndpointDescriptor).filter_by(**where).order_by(EndpointDescriptor.op_id)
        rows = list((await session.execute(stmt)).scalars())
        session.expunge_all()
    return rows


def _marks(rows: list[EndpointDescriptor]) -> dict[str, tuple[str | None, bool | None]]:
    return {row.op_id: (row.hard_to_undo, row.changes_rights) for row in rows}


def _delete_signs(rows: list[EndpointDescriptor]) -> dict[str, str | None]:
    patterns = get_settings().service_grant_delete_shaped_patterns
    return {row.op_id: delete_grant_reason(row.op_id, patterns, row) for row in rows}


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_id", [None, _TENANT], ids=["built-in", "one-tenant"])
async def test_an_import_and_a_re_import_write_no_mark(
    tenant_id: uuid.UUID | None, embedding: AsyncMock
) -> None:
    text = _spec("listWidgets", "addWidget", "removeWidget")
    imported = {"source_kind": "ingested", "tenant_id": tenant_id}

    await _import(text, embedding, tenant_id=tenant_id, connector=_CONNECTOR)
    fresh = await _rows(**imported)
    assert [row.op_id for row in fresh] == [
        "DELETE:/widgets/{widget_id}",
        "GET:/widgets",
        "POST:/widgets",
    ]
    assert set(_marks(fresh).values()) == {(None, None)}
    fresh_signs = _delete_signs(fresh)

    # A value stored on the imported rows by hand.
    async with get_sessionmaker()() as session:
        changed = await session.execute(
            update(EndpointDescriptor)
            .filter_by(**imported)
            .values(hard_to_undo="overwrites", changes_rights=True)
        )
        await session.commit()
    assert changed.rowcount == 3
    marked = await _rows(**imported)
    assert set(_marks(marked).values()) == {("overwrites", True)}
    # With the switch on, the delete-sign check reads the stored value.
    assert _delete_signs(marked)["GET:/widgets"] is not None
    assert fresh_signs["GET:/widgets"] is None

    await _import(text, embedding, tenant_id=tenant_id, connector=_CONNECTOR)
    again = await _rows(**imported)
    assert [row.op_id for row in again] == [row.op_id for row in fresh]
    assert set(_marks(again).values()) == {(None, None)}
    assert _delete_signs(again) == fresh_signs


@pytest.fixture
def code_marks(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[RowKey, tuple[str | None, bool]]]:
    """Run every registrar for real and note the two values each register call writes.

    The suite replays the rows of its first registration pass; that is off
    here, so each register call runs and passes the values of its
    definition in code.
    """
    monkeypatch.delenv("MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS", raising=False)
    get_settings.cache_clear()
    clear_typed_op_snapshot()
    noted: dict[RowKey, tuple[str | None, bool]] = {}
    write = typed_register._register_in_session

    @functools.wraps(write)
    async def noted_write(*args: Any, **kwargs: Any) -> None:
        key = (kwargs["product"], kwargs["version"], kwargs["impl_id"], kwargs["op_id"])
        noted[key] = (kwargs["hard_to_undo"], kwargs["changes_rights"])
        await write(*args, **kwargs)

    monkeypatch.setattr(typed_register, "_register_in_session", noted_write)
    yield noted
    clear_typed_op_snapshot()
    get_settings.cache_clear()


def _key(row: EndpointDescriptor) -> RowKey:
    return (row.product, row.version, row.impl_id, row.op_id)


def _all_fields(rows: list[EndpointDescriptor]) -> dict[RowKey, dict[str, Any]]:
    return {_key(row): {name: getattr(row, name) for name in _COLUMNS} for row in rows}


@pytest.mark.asyncio
async def test_an_import_never_changes_a_built_in_row(
    code_marks: dict[RowKey, tuple[str | None, bool]], embedding: AsyncMock
) -> None:
    built_in = [row for row in await register_all_descriptors() if row.source_kind != "ingested"]
    assert len(built_in) > 500
    assert {_key(row) for row in built_in} == set(code_marks)
    before = _all_fields(built_in)

    # The spec's operation ids are the ids of three built-in operations of one
    # connector that set a mark, and the import goes into that connector.
    by_connector: dict[tuple[str, str, str], list[str]] = {}
    for row in sorted(built_in, key=_key):
        if row.hard_to_undo is not None:
            by_connector.setdefault(_key(row)[:3], []).append(row.op_id)
    triple, ids = next((k, v[:3]) for k, v in by_connector.items() if len(v) >= 3)
    connector = dict(zip(("product", "version", "impl_id"), triple, strict=True))
    text = _spec(*ids)

    for tenant_id in (None, _TENANT):
        await _import(text, embedding, tenant_id=tenant_id, connector=connector)
        await _import(text, embedding, tenant_id=tenant_id, connector=_CONNECTOR)
        await _import(text, embedding, tenant_id=tenant_id, connector=connector)

    imported = await _rows(source_kind="ingested")
    assert len(imported) == 12
    assert set(_marks(imported).values()) == {(None, None)}

    after = [row for row in await _rows() if row.source_kind != "ingested"]
    assert {_key(row): (row.hard_to_undo, row.changes_rights) for row in after} == code_marks
    assert _all_fields(after) == before
