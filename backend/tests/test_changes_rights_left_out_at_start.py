# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An operation that sets ``changes_rights`` at the risk level ``safe`` is left out.

The tests run the real start (:func:`run_typed_op_registrars`) with every
built-in registrar plus a stand-in connector package ``cowkit``, with the
switch ``CUSTOM_POLICY`` off and on. Each test reads the stored operation
rows and the log after the start.

* The one operation that sets ``changes_rights`` at ``safe`` has no row,
  one ``operation_left_out`` log line names it and the reason, and a call
  of it answers ``operation_left_out``. This holds for a typed and for a
  multi-step operation.
* Every other operation of the package, and every built-in operation, has
  the row it has after a start with a package without that operation. An
  operation at ``caution`` with ``changes_rights`` stores the value.
* The operations of a generic connector, made from an API spec, set
  neither field and keep their rows.

All package, connector and operation names are made up.
"""

from __future__ import annotations

import sys
import types
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from sqlalchemy import select

from meho_backplane import connector_packages
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connector_packages import record_installed_connector_packages
from meho_backplane.connectors.registry import _eager_import_connectors, deregister_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import _left_out, dispatch
from meho_backplane.operations.ingest.api_schemas import SpecSource
from meho_backplane.operations.ingest.llm_groups import GroupingResult
from meho_backplane.operations.ingest.pipeline import IngestionPipelineService
from meho_backplane.operations.typed_register import (
    register_composite_operation,
    register_typed_operation,
)
from meho_backplane.settings import get_settings
from tests.test_operation_left_out_at_start import _left_out_lines, _start

_FOLDER, _PACKAGE = "cowkit_pkg", "cowkit"
_MODULE = f"meho_backplane.connectors.{_FOLDER}"
_PRODUCT, _VERSION, _IMPL = "cowkit", "1.x", "cowkit-api"
_CONNECTOR_ID = "cowkit-api-1.x"

_LIST = "cowkit.box.list"
_PEEK = "cowkit.box.peek"
_GRANT = "cowkit.box.grant"
_COMPOSITE_PEEK = "cowkit.composite.peek"

_SPEC_PRODUCT, _SPEC_VERSION, _SPEC_IMPL = "cowspec", "1.0", "cowspec-rest"
_SPEC_YAML = """
openapi: "3.1.0"
info:
  title: Cowspec fixture
  version: "1.0.0"
paths:
  /pens:
    get:
      operationId: listPens
      summary: List pens
      responses:
        "200":
          description: Pens.
    post:
      operationId: addPen
      summary: Add a pen
      responses:
        "201":
          description: Added.
"""

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c0ce")

#: Columns a start sets anew on every row it writes; every other column is compared.
_VOLATILE = frozenset({"id", "created_at", "updated_at", "group_id"})

_SWITCH = pytest.mark.parametrize("switch_on", [False, True], ids=["switch_off", "switch_on"])


async def cowkit_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"from": "cowkit"}


async def cowkit_steps(
    *, operator: Any, target: Any, params: Any, dispatch_child: Any
) -> dict[str, Any]:
    return {"from": "cowkit"}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS", "false")
    monkeypatch.setattr(_left_out, "_LEFT_OUT", frozenset())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stand_in_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the package's handlers in a connector folder that the package installed."""
    module = types.ModuleType(_MODULE)
    for handler in (cowkit_handler, cowkit_steps):
        monkeypatch.setattr(handler, "__module__", _MODULE)
        setattr(module, handler.__name__, handler)
    monkeypatch.setitem(sys.modules, _MODULE, module)
    monkeypatch.setattr(connector_packages, "_installed_folders", {})
    record_installed_connector_packages({_FOLDER: _PACKAGE})
    _eager_import_connectors()


@pytest.fixture
def embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", "true" if on else "false")
    get_settings.cache_clear()


def _common(op_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "product": _PRODUCT,
        "version": _VERSION,
        "impl_id": _IMPL,
        "op_id": op_id,
        "summary": f"Cow box operation {op_id}.",
        "description": f"Works with one cow box ({op_id}).",
        "parameter_schema": {"type": "object", "properties": {}},
        "group_key": "boxes",
        "when_to_use": "Use the boxes group for cow boxes.",
        **extra,
    }


def _cowkit(*, peek: bool, composite_peek: bool = False) -> Any:
    """The registrar of ``cowkit``; *peek* and *composite_peek* add the misfit ops."""

    async def cowkit(*, embedding_service: Any = None) -> None:
        await register_typed_operation(
            handler=cowkit_handler, embedding_service=embedding_service, **_common(_LIST)
        )
        if peek:
            await register_typed_operation(
                handler=cowkit_handler,
                changes_rights=True,
                embedding_service=embedding_service,
                **_common(_PEEK),
            )
        await register_typed_operation(
            handler=cowkit_handler,
            safety_level="caution",
            requires_approval=True,
            changes_rights=True,
            embedding_service=embedding_service,
            **_common(_GRANT),
        )
        if composite_peek:
            await register_composite_operation(
                handler=cowkit_steps,
                safety_level="safe",
                requires_approval=False,
                changes_rights=True,
                embedding_service=embedding_service,
                **_common(_COMPOSITE_PEEK),
            )

    return cowkit


async def _rows() -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(EndpointDescriptor))).scalars().all()
    return [{col.name: getattr(row, col.name) for col in row.__table__.columns} for row in rows]


def _comparable(rows: list[dict[str, Any]]) -> set[Any]:
    return {
        tuple(sorted((k, repr(v)) for k, v in row.items() if k not in _VOLATILE)) for row in rows
    }


def _by_op(rows: list[dict[str, Any]], product: str = _PRODUCT) -> dict[str, dict[str, Any]]:
    return {r["op_id"]: r for r in rows if r["product"] == product}


async def _wipe_database() -> None:
    from sqlalchemy import delete

    async with get_sessionmaker()() as session, session.begin():
        await session.execute(delete(EndpointDescriptor))
        await session.execute(delete(OperationGroup))


def _operator() -> Operator:
    return Operator(
        sub="op-cowkit",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _call(op_id: str) -> Any:
    return await dispatch(
        operator=_operator(), connector_id=_CONNECTOR_ID, op_id=op_id, target=None, params={}
    )


def _assert_line(line: dict[str, Any], op_id: str) -> None:
    assert {k: line[k] for k in ("op_id", "product", "version", "impl_id")} == {
        "op_id": op_id,
        "product": _PRODUCT,
        "version": _VERSION,
        "impl_id": _IMPL,
    }
    assert line["connector_package"] == _PACKAGE
    assert "changes_rights=True on a 'safe' operation" in line["reason"]
    assert "cannot change rights" in line["reason"]


async def _start_and_check(embedding: AsyncMock, *, composite_peek: bool) -> None:
    await _start(_cowkit(peek=False), embedding=embedding)
    expected = _comparable(await _rows())
    await _wipe_database()

    with structlog.testing.capture_logs() as logs:
        await _start(_cowkit(peek=True, composite_peek=composite_peek), embedding=embedding)

    rows = await _rows()
    assert _comparable(rows) == expected
    cowkit = _by_op(rows)
    assert set(cowkit) == {_LIST, _GRANT}
    assert cowkit[_GRANT]["changes_rights"] is True
    left_out = [_PEEK, _COMPOSITE_PEEK] if composite_peek else [_PEEK]
    lines = _left_out_lines(logs)
    assert [line["op_id"] for line in lines] == left_out
    for line, op_id in zip(lines, left_out, strict=True):
        _assert_line(line, op_id)
        result = await _call(op_id)
        assert result.status == "error"
        assert result.extras["error_code"] == "operation_left_out"
    assert (await _call(_LIST)).status == "ok"


@_SWITCH
@pytest.mark.asyncio
async def test_case20_21_a_safe_typed_op_with_changes_rights_is_left_out(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock, switch_on: bool
) -> None:
    _switch(monkeypatch, on=switch_on)
    await _start_and_check(embedding, composite_peek=False)


@_SWITCH
@pytest.mark.asyncio
async def test_case22_23_a_safe_multi_step_op_with_changes_rights_is_left_out(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock, switch_on: bool
) -> None:
    _switch(monkeypatch, on=switch_on)
    await _start_and_check(embedding, composite_peek=True)


@pytest.fixture
def _spec_shim() -> Iterator[None]:
    yield
    deregister_connector_v2(product=_SPEC_PRODUCT, version=_SPEC_VERSION, impl_id=_SPEC_IMPL)


async def _ingest_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = Operator(
        sub="op-cowspec-admin",
        name="Platform Admin",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )
    embedding_service = AsyncMock()
    embedding_service.encode_one.return_value = [0.2] * 384
    embedding_service.encode.return_value = [[0.2] * 384, [0.2] * 384]
    embedding_service.dimension = 384
    service = IngestionPipelineService(
        admin, sessionmaker=get_sessionmaker(), embedding_service=embedding_service
    )

    async def _no_grouping(**_kwargs: Any) -> GroupingResult:
        return GroupingResult(
            connector_id=f"{_SPEC_IMPL}-{_SPEC_VERSION}",
            groups_created=0,
            operations_assigned=0,
            operations_unassigned=2,
            llm_call_count=0,
            llm_duration_ms=0.0,
        )

    monkeypatch.setattr(service, "_run_grouping_phase", _no_grouping)
    await service.ingest(
        product=_SPEC_PRODUCT,
        version=_SPEC_VERSION,
        impl_id=_SPEC_IMPL,
        specs=[SpecSource(uri="fixture://cowspec/spec.yaml", content=_SPEC_YAML)],
        tenant_id=None,
        dry_run=False,
    )


@_SWITCH
@pytest.mark.asyncio
@pytest.mark.usefixtures("_spec_shim")
async def test_case24_a_generic_package_from_an_api_spec_keeps_its_rows(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock, switch_on: bool
) -> None:
    _switch(monkeypatch, on=switch_on)
    await _ingest_spec(monkeypatch)
    before = _by_op(await _rows(), _SPEC_PRODUCT)
    assert len(before) == 2

    with structlog.testing.capture_logs() as logs:
        await _start(embedding=embedding)

    after = _by_op(await _rows(), _SPEC_PRODUCT)
    assert after == before
    for row in after.values():
        assert row["source_kind"] == "ingested"
        assert (row["hard_to_undo"], row["changes_rights"]) == (None, None)
    assert _left_out_lines(logs) == []
