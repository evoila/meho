# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An operation whose mark does not fit its risk level is left out, and the start goes on.

The tests run the real start (:func:`run_typed_op_registrars`) with every
built-in registrar plus a stand-in connector package. The package
registers three operations; one of them does not fit. After the start:

* the bad operation has no row, and a call answers as for an operation
  that does not exist;
* the two other operations of the package have the same rows as after a
  start of the package without the bad operation;
* every built-in operation row is stored;
* one ``operation_left_out`` log line names the operation, its connector
  and the reason.

This holds for a typed and for a multi-step operation, and for any check
in ``_MARK_FIT_CHECKS`` (a test-only check proves it). All package,
connector and operation names are made up.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import structlog
from sqlalchemy import delete, select

from meho_backplane import connector_packages
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connector_packages import record_installed_connector_packages
from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import dispatch, typed_register
from meho_backplane.operations.typed_register import (
    register_composite_operation,
    register_typed_op_registrar,
    register_typed_operation,
    run_typed_op_registrars,
)
from meho_backplane.settings import get_settings

_FOLDER = "example_gizmo_pkg"
_MODULE = f"meho_backplane.connectors.{_FOLDER}"
_PACKAGE = "example-gizmos"
_PRODUCT, _VERSION, _IMPL = "gizmo", "3.x", "gizmo-api"
_CONNECTOR_ID = "gizmo-api-3.x"

_GOOD_READ = "gizmo.item.list"
_GOOD_WRITE = "gizmo.item.rename"
_BAD = "gizmo.item.wipe"
_NEVER = "gizmo.item.never_registered"

_Registrar = Callable[..., Awaitable[None]]


async def gizmo_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


async def gizmo_steps(
    *, operator: Any, target: Any, params: Any, dispatch_child: Any
) -> dict[str, Any]:
    return {"ok": True}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stand_in_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the package's handlers in a connector folder that the package installed."""
    module = types.ModuleType(_MODULE)
    for handler in (gizmo_handler, gizmo_steps):
        monkeypatch.setattr(handler, "__module__", _MODULE)
        setattr(module, handler.__name__, handler)
    monkeypatch.setitem(sys.modules, _MODULE, module)
    monkeypatch.setattr(connector_packages, "_installed_folders", {})
    record_installed_connector_packages({_FOLDER: _PACKAGE})
    # Keep the test-only row cache of other tests as it was.
    monkeypatch.setattr(typed_register, "_TYPED_OP_SNAPSHOT", typed_register._TYPED_OP_SNAPSHOT)
    _eager_import_connectors()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _common() -> dict[str, Any]:
    return {
        "product": _PRODUCT,
        "version": _VERSION,
        "impl_id": _IMPL,
        "summary": "Work with a gizmo.",
        "description": "Works with one gizmo of a made-up product.",
        "parameter_schema": {"type": "object", "properties": {}},
        "when_to_use": None,
    }


async def good_package(*, embedding_service: Any = None) -> None:
    await register_typed_operation(
        op_id=_GOOD_READ, handler=gizmo_handler, embedding_service=embedding_service, **_common()
    )
    await register_typed_operation(
        op_id=_GOOD_WRITE,
        handler=gizmo_handler,
        safety_level="caution",
        requires_approval=True,
        embedding_service=embedding_service,
        **_common(),
    )


async def package_with_bad_typed_op(*, embedding_service: Any = None) -> None:
    await register_typed_operation(
        op_id=_GOOD_READ, handler=gizmo_handler, embedding_service=embedding_service, **_common()
    )
    await register_typed_operation(
        op_id=_BAD,
        handler=gizmo_handler,
        hard_to_undo="deletes",
        embedding_service=embedding_service,
        **_common(),
    )
    await register_typed_operation(
        op_id=_GOOD_WRITE,
        handler=gizmo_handler,
        safety_level="caution",
        requires_approval=True,
        embedding_service=embedding_service,
        **_common(),
    )


async def package_with_bad_multi_step_op(*, embedding_service: Any = None) -> None:
    await register_typed_operation(
        op_id=_GOOD_READ, handler=gizmo_handler, embedding_service=embedding_service, **_common()
    )
    await register_composite_operation(
        op_id=_BAD,
        handler=gizmo_steps,
        safety_level="safe",
        requires_approval=False,
        hard_to_undo="stops",
        embedding_service=embedding_service,
        **_common(),
    )
    await register_typed_operation(
        op_id=_GOOD_WRITE,
        handler=gizmo_handler,
        safety_level="caution",
        requires_approval=True,
        embedding_service=embedding_service,
        **_common(),
    )


async def package_with_test_only_misfit(*, embedding_service: Any = None) -> None:
    await register_typed_operation(
        op_id=_GOOD_READ, handler=gizmo_handler, embedding_service=embedding_service, **_common()
    )
    await register_typed_operation(
        op_id=_BAD,
        handler=gizmo_handler,
        changes_rights=True,
        embedding_service=embedding_service,
        **_common(),
    )
    await register_typed_operation(
        op_id=_GOOD_WRITE,
        handler=gizmo_handler,
        safety_level="caution",
        requires_approval=True,
        embedding_service=embedding_service,
        **_common(),
    )


def _row_key(row: EndpointDescriptor) -> tuple[Any, ...]:
    return (
        row.product,
        row.version,
        row.impl_id,
        row.op_id,
        row.source_kind,
        row.handler_ref,
        row.owner_package,
        row.safety_level,
        row.requires_approval,
        row.hard_to_undo,
        row.changes_rights,
        row.is_enabled,
    )


async def _all_rows() -> set[tuple[Any, ...]]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(EndpointDescriptor))).scalars().all()
    return {_row_key(row) for row in rows}


async def _clear_rows() -> None:
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(delete(EndpointDescriptor))
        await session.execute(delete(OperationGroup))


async def _start_with(registrar: _Registrar, embedding: AsyncMock) -> None:
    """One start of MEHO: every built-in registrar plus *registrar*."""
    saved = list(typed_register._TYPED_OP_REGISTRARS)
    register_typed_op_registrar(registrar)
    try:
        await run_typed_op_registrars(embedding_service=embedding)
    finally:
        typed_register._TYPED_OP_REGISTRARS[:] = saved


async def _call(op_id: str) -> Any:
    operator = Operator(
        sub="op-left-out",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-000000007137"),
        tenant_role=TenantRole.OPERATOR,
    )
    return await dispatch(
        operator=operator, connector_id=_CONNECTOR_ID, op_id=op_id, target=None, params={}
    )


async def _assert_left_out_alone(
    bad_package: _Registrar, embedding: AsyncMock, reason_part: str
) -> None:
    await _start_with(good_package, embedding)
    expected = await _all_rows()
    built_in = {row for row in expected if row[0] != _PRODUCT}
    assert built_in, "the start stored no built-in operation row"
    assert {row[3] for row in expected if row[0] == _PRODUCT} == {_GOOD_READ, _GOOD_WRITE}

    await _clear_rows()
    with structlog.testing.capture_logs() as logs:
        await _start_with(bad_package, embedding)

    assert await _all_rows() == expected
    left_out = [e for e in logs if e["event"] == "operation_left_out"]
    assert len(left_out) == 1
    assert {k: left_out[0][k] for k in ("op_id", "product", "version", "impl_id")} == {
        "op_id": _BAD,
        "product": _PRODUCT,
        "version": _VERSION,
        "impl_id": _IMPL,
    }
    assert left_out[0]["connector_package"] == _PACKAGE
    assert reason_part in left_out[0]["reason"]

    bad, never = await _call(_BAD), await _call(_NEVER)
    assert bad.status == never.status == "error"
    assert bad.extras["error_code"] == never.extras["error_code"] == "unknown_op"
    assert (await _call(_GOOD_READ)).status == "ok"


@pytest.mark.asyncio
async def test_a_typed_op_that_does_not_fit_is_left_out_alone(embedding: AsyncMock) -> None:
    await _assert_left_out_alone(package_with_bad_typed_op, embedding, "cannot be hard to undo")


@pytest.mark.asyncio
async def test_a_multi_step_op_that_does_not_fit_is_left_out_alone(embedding: AsyncMock) -> None:
    await _assert_left_out_alone(
        package_with_bad_multi_step_op, embedding, "cannot be hard to undo"
    )


def _test_only_check(marks: Any) -> str | None:
    if marks.changes_rights and marks.safety_level == "safe":
        return "test-only check: changes_rights on a 'safe' operation"
    return None


@pytest.mark.asyncio
async def test_a_second_check_leaves_its_op_out_the_same_way(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    monkeypatch.setattr(
        typed_register,
        "_MARK_FIT_CHECKS",
        (*typed_register._MARK_FIT_CHECKS, _test_only_check),
    )
    await _assert_left_out_alone(package_with_test_only_misfit, embedding, "test-only check")
