# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An operation whose mark does not fit its risk level is left out, and the start goes on.

The tests run the real start (:func:`run_typed_op_registrars`) with every
built-in registrar plus a stand-in connector package, with the connector
declaration check on (the test-only registrar snapshot is off). The
package ``probe-widgets`` registers three operations; in its "misfit"
version one of them sets ``hard_to_undo`` on a ``safe`` level.

After a start with the misfit version:

* MEHO writes and deletes no row for the left-out operation. A row that an
  earlier start, an import, a tenant or other code stored under that key
  is unchanged, field for field.
* The left-out operation is hidden: a call answers ``operation_left_out``
  (also when no row is stored), and search and the group list do not
  offer it. A row of other code under the same key is not hidden.
* A later start without the fault uses the stored row again, with the
  operator's settings on it.
* The other operations of the package, and every built-in row, are the
  same as after a start without the misfit mark.
* One ``operation_left_out`` log line names the operation, its connector
  and the reason.
* A connector whose test read is the left-out operation starts without
  its test read, and one more log line says so.
* Every stored built-in typed or multi-step row under a left-out key is
  hidden, unless a registrar wrote that key in the same start. A path that
  runs an operation without the dispatcher (the forward of a connector)
  hides it the same way.
* Every other check of a registration still runs for a left-out
  operation.

All package, connector and operation names are made up.
"""

from __future__ import annotations

import sys
import types
import uuid
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from meho_backplane import connector_packages
from meho_backplane.api.v1.connectors_target_form import router as target_form_router
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connector_packages import record_installed_connector_packages
from meho_backplane.connectors import Connector
from meho_backplane.connectors import registry as connector_registry
from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.connectors.target_form import ConnectorDeclarationError
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations import _left_out, dispatch, typed_register
from meho_backplane.operations._handler_resolve import import_handler
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.connector_declaration_check import registered_connector_classes
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.operations.meta_tools import list_operation_groups, search_operations
from meho_backplane.operations.typed_register import (
    register_composite_operation,
    register_typed_op_registrar,
    register_typed_operation,
    run_typed_op_registrars,
)
from meho_backplane.settings import get_settings

_WIDGETS_FOLDER, _WIDGETS = "probe_widgets_pkg", "probe-widgets"
_GADGETS_FOLDER, _GADGETS = "probe_gadgets_pkg", "probe-gadgets"
_WIDGETS_MODULE = f"meho_backplane.connectors.{_WIDGETS_FOLDER}"
_GADGETS_MODULE = f"meho_backplane.connectors.{_GADGETS_FOLDER}"
_PRODUCT, _VERSION, _IMPL = "probewid", "2.x", "probewid-api"
_CONNECTOR_ID = "probewid-api-2.x"

_LIST = "probewid.box.list"
_WIPE = "probewid.box.wipe"
_RENAME = "probewid.box.rename"
_NEVER = "probewid.box.never"
_WIPE_GROUP = "box-cleanup"

_K8S_PROBE = "k8s.probebox.list"

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000007138")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-000000007139")

#: Columns a start sets anew on every row it writes; every other column is compared.
_VOLATILE = frozenset({"id", "created_at", "updated_at", "group_id"})

_Registrar = Callable[..., Awaitable[None]]


async def widgets_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"from": "widgets"}


async def widgets_steps(
    *, operator: Any, target: Any, params: Any, dispatch_child: Any
) -> dict[str, Any]:
    return {"from": "widgets"}


async def gadgets_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"from": "gadgets"}


#: Every call that reached :func:`widgets_k8s_handler`.
_K8S_CALLS: list[dict[str, Any]] = []


async def widgets_k8s_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    _K8S_CALLS.append(params)
    return {"from": "widgets"}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Required settings, the declaration check on, and no left-out op of another test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS", "false")
    monkeypatch.setattr(_left_out, "_LEFT_OUT", frozenset())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stand_in_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put each package's handlers in a connector folder that the package installed."""
    for module_name, handlers in (
        (_WIDGETS_MODULE, (widgets_handler, widgets_steps, widgets_k8s_handler)),
        (_GADGETS_MODULE, (gadgets_handler,)),
    ):
        module = types.ModuleType(module_name)
        for handler in handlers:
            monkeypatch.setattr(handler, "__module__", module_name)
            setattr(module, handler.__name__, handler)
        monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setattr(connector_packages, "_installed_folders", {})
    _K8S_CALLS.clear()
    record_installed_connector_packages({_WIDGETS_FOLDER: _WIDGETS, _GADGETS_FOLDER: _GADGETS})
    _eager_import_connectors()


@pytest.fixture
def embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


# ---------------------------------------------------------------------------
# The stand-in packages
# ---------------------------------------------------------------------------


def _common(op_id: str, **extra: Any) -> dict[str, Any]:
    group = _WIPE_GROUP if op_id == _WIPE else "boxes"
    fields: dict[str, Any] = {
        "product": _PRODUCT,
        "version": _VERSION,
        "impl_id": _IMPL,
        "op_id": op_id,
        "summary": f"Probe box operation {op_id}.",
        "description": f"Works with one probe box ({op_id}).",
        "parameter_schema": {"type": "object", "properties": {}},
        "group_key": group,
        "when_to_use": f"Use the {group} group for probe boxes.",
    }
    fields.update(extra)
    return fields


async def _list_and_rename(embedding_service: Any) -> None:
    await register_typed_operation(
        handler=widgets_handler, embedding_service=embedding_service, **_common(_LIST)
    )
    await register_typed_operation(
        handler=widgets_handler,
        safety_level="caution",
        requires_approval=True,
        embedding_service=embedding_service,
        **_common(_RENAME),
    )


def _widgets(wipe: str, *, wipe_id: str = _WIPE, **wipe_extra: Any) -> _Registrar:
    """The registrar of ``probe-widgets``.

    *wipe* is ``fixed``, ``misfit``, ``multi_step`` (a misfit multi-step
    op) or ``multi_step_fixed``.
    """

    async def probe_widgets(*, embedding_service: Any = None) -> None:
        await register_typed_operation(
            handler=widgets_handler, embedding_service=embedding_service, **_common(_LIST)
        )
        fields = _common(wipe_id, **wipe_extra)
        if wipe in ("multi_step", "multi_step_fixed"):
            await register_composite_operation(
                handler=widgets_steps,
                safety_level="safe",
                requires_approval=False,
                hard_to_undo="stops" if wipe == "multi_step" else None,
                embedding_service=embedding_service,
                **fields,
            )
        else:
            await register_typed_operation(
                handler=widgets_handler,
                hard_to_undo="deletes" if wipe == "misfit" else None,
                embedding_service=embedding_service,
                **fields,
            )
        await register_typed_operation(
            handler=widgets_handler,
            safety_level="caution",
            requires_approval=True,
            embedding_service=embedding_service,
            **_common(_RENAME),
        )

    return probe_widgets


async def probe_gadgets(*, embedding_service: Any = None) -> None:
    await register_typed_operation(
        handler=gadgets_handler, embedding_service=embedding_service, **_common(_WIPE)
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _start(*registrars: _Registrar, embedding: AsyncMock) -> None:
    """One start of MEHO: every built-in registrar plus *registrars*."""
    saved = list(typed_register._TYPED_OP_REGISTRARS)
    for registrar in registrars:
        register_typed_op_registrar(registrar)
    try:
        await run_typed_op_registrars(embedding_service=embedding)
    finally:
        typed_register._TYPED_OP_REGISTRARS[:] = saved


def _fields(row: EndpointDescriptor) -> dict[str, Any]:
    return {col.name: getattr(row, col.name) for col in row.__table__.columns}


async def _rows() -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(EndpointDescriptor))).scalars().all()
    return [_fields(row) for row in rows]


async def _row(op_id: str, *, tenant_id: uuid.UUID | None = None) -> dict[str, Any] | None:
    found = [r for r in await _rows() if r["op_id"] == op_id and r["tenant_id"] == tenant_id]
    assert len(found) <= 1
    return found[0] if found else None


def _comparable(rows: list[dict[str, Any]], *, leave_out: str | None = None) -> set[Any]:
    """Every row of every connector, without the columns a start sets anew."""
    return {
        tuple(sorted((k, repr(v)) for k, v in row.items() if k not in _VOLATILE))
        for row in rows
        if row["op_id"] != leave_out
    }


async def _wipe_database() -> None:
    from sqlalchemy import delete

    from meho_backplane.db.models import OperationGroup

    async with get_sessionmaker()() as session, session.begin():
        await session.execute(delete(EndpointDescriptor))
        await session.execute(delete(OperationGroup))


def _operator(tenant_id: uuid.UUID = _TENANT) -> Operator:
    return Operator(
        sub="op-left-out",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _call(op_id: str, *, tenant_id: uuid.UUID = _TENANT) -> Any:
    return await dispatch(
        operator=_operator(tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=None,
        params={},
    )


async def _offered(*, tenant_id: uuid.UUID = _TENANT) -> tuple[set[str], set[str], set[str]]:
    """The op ids search finds, the op ids of the group list, and its group keys."""
    found = await search_operations(
        _operator(tenant_id), {"connector_id": _CONNECTOR_ID, "query": "probe box", "limit": 50}
    )
    groups = await list_operation_groups(
        _operator(tenant_id), {"connector_id": _CONNECTOR_ID}, list_operations=True
    )
    group_ops = {op["op_id"] for group in groups["groups"] for op in group.get("operations") or []}
    return (
        {hit["op_id"] for hit in found["hits"]},
        group_ops,
        {group["group_key"] for group in groups["groups"]},
    )


def _assert_refused_as_left_out(result: Any, op_id: str = _WIPE) -> None:
    assert result.status == "error"
    assert result.extras["error_code"] == "operation_left_out"
    assert repr(op_id) in result.error
    assert "left out at the start: its mark does not fit its risk level" in result.error


def _left_out_lines(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "operation_left_out"]


async def _baseline(embedding: AsyncMock) -> set[Any]:
    """The rows after a start with the fixed package on an empty database, then empty it."""
    await _start(_widgets("fixed"), embedding=embedding)
    rows = _comparable(await _rows(), leave_out=_WIPE)
    await _wipe_database()
    return rows


# ---------------------------------------------------------------------------
# Point 1: a left-out op is hidden, and nothing is written or deleted for it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case5_operator_settings_stay_through_a_misfit_start(embedding: AsyncMock) -> None:
    expected = await _baseline(embedding)
    await _start(_widgets("fixed"), embedding=embedding)
    admin = Operator(
        sub="op-admin",
        name="Platform Admin",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )
    await ReviewService(admin).edit_op(
        _CONNECTOR_ID, _WIPE, tenant_id=None, is_enabled=False, custom_description="Operator text."
    )
    before = await _row(_WIPE)
    assert before is not None
    assert (before["is_enabled"], before["custom_description"]) == (False, "Operator text.")

    with structlog.testing.capture_logs() as logs:
        await _start(_widgets("misfit"), embedding=embedding)

    assert await _row(_WIPE) == before
    assert _comparable(await _rows(), leave_out=_WIPE) == expected
    assert len(_left_out_lines(logs)) == 1
    _assert_refused_as_left_out(await _call(_WIPE))
    searched, listed, group_keys = await _offered()
    assert _WIPE not in searched
    assert _WIPE not in listed
    assert _WIPE_GROUP not in group_keys
    assert _LIST in searched

    await _start(_widgets("fixed"), embedding=embedding)

    after = await _row(_WIPE)
    assert after is not None
    assert (after["is_enabled"], after["custom_description"]) == (False, "Operator text.")
    assert after["id"] == before["id"]
    assert (await _call(_WIPE)).extras.get("error_code") != "operation_left_out"


@pytest.mark.asyncio
async def test_case6_a_fresh_database_gets_no_row_and_the_call_is_refused(
    embedding: AsyncMock,
) -> None:
    expected = await _baseline(embedding)
    with structlog.testing.capture_logs() as logs:
        await _start(_widgets("misfit"), embedding=embedding)

    assert await _row(_WIPE) is None
    assert _comparable(await _rows()) == expected
    [line] = _left_out_lines(logs)
    assert {k: line[k] for k in ("op_id", "product", "version", "impl_id")} == {
        "op_id": _WIPE,
        "product": _PRODUCT,
        "version": _VERSION,
        "impl_id": _IMPL,
    }
    assert line["connector_package"] == _WIDGETS
    assert "cannot be hard to undo" in line["reason"]
    _assert_refused_as_left_out(await _call(_WIPE))
    assert (await _call(_NEVER)).extras["error_code"] == "unknown_op"
    assert (await _call(_LIST)).status == "ok"
    searched, listed, group_keys = await _offered()
    assert _WIPE not in searched | listed
    assert _WIPE_GROUP not in group_keys


@pytest.mark.asyncio
async def test_case6_a_multi_step_op_is_left_out_the_same_way(embedding: AsyncMock) -> None:
    expected = await _baseline(embedding)
    with structlog.testing.capture_logs() as logs:
        await _start(_widgets("multi_step"), embedding=embedding)

    assert await _row(_WIPE) is None
    assert _comparable(await _rows()) == expected
    [line] = _left_out_lines(logs)
    assert line["op_id"] == _WIPE
    _assert_refused_as_left_out(await _call(_WIPE))


def _built_in_op_that_is_no_test_read(rows: list[dict[str, Any]]) -> dict[str, Any]:
    test_reads = {
        (cls.product, cls.version, cls.impl_id, cls.test_operation)
        for cls in registered_connector_classes()
    }
    candidates = sorted(
        (
            row
            for row in rows
            if row["tenant_id"] is None
            and row["owner_package"] is None
            and row["source_kind"] == "typed"
            and row["is_enabled"]
            and row["safety_level"] == "safe"
            and (row["product"], row["version"], row["impl_id"], row["op_id"]) not in test_reads
        ),
        key=lambda row: (row["product"], row["version"], row["impl_id"], row["op_id"]),
    )
    return candidates[0]


@pytest.mark.parametrize("shared_handler", [False, True], ids=["own_handler", "shared_handler"])
@pytest.mark.asyncio
async def test_case7_a_package_cannot_hide_a_built_in_op(
    embedding: AsyncMock, shared_handler: bool
) -> None:
    await _start(embedding=embedding)
    built_in = _built_in_op_that_is_no_test_read(await _rows())
    # With a shared handler, the package's op names no package either.
    handler = import_handler(built_in["handler_ref"]) if shared_handler else widgets_handler
    connector_id = f"{built_in['impl_id']}-{built_in['version']}"
    operator = _operator()

    async def _observed() -> tuple[Any, ...]:
        call = await dispatch(
            operator=operator,
            connector_id=connector_id,
            op_id=built_in["op_id"],
            target=None,
            params={},
        )
        hit = await search_operations(
            operator, {"connector_id": connector_id, "op_id": built_in["op_id"]}
        )
        groups = await list_operation_groups(
            operator, {"connector_id": connector_id}, list_operations=True
        )
        listed = {op["op_id"] for group in groups["groups"] for op in group.get("operations") or []}
        return (
            call.status,
            call.extras.get("error_code"),
            hit.get("operation", {}).get("op_id"),
            built_in["op_id"] in listed,
        )

    without_package = await _observed()
    stored = await _row(built_in["op_id"])

    async def misfit_on_a_built_in_key(*, embedding_service: Any = None) -> None:
        await register_typed_operation(
            product=built_in["product"],
            version=built_in["version"],
            impl_id=built_in["impl_id"],
            op_id=built_in["op_id"],
            handler=handler,
            summary="Not the built-in operation.",
            description="A package op under a built-in key.",
            parameter_schema={"type": "object"},
            when_to_use=None,
            hard_to_undo="deletes",
            embedding_service=embedding_service,
        )

    with structlog.testing.capture_logs() as logs:
        await _start(misfit_on_a_built_in_key, embedding=embedding)

    assert len(_left_out_lines(logs)) == 1
    stored_after = await _row(built_in["op_id"])
    assert stored_after is not None and stored is not None
    assert {k: v for k, v in stored_after.items() if k != "updated_at"} == {
        k: v for k, v in stored.items() if k != "updated_at"
    }
    assert stored_after["owner_package"] is None
    assert await _observed() == without_package
    assert without_package[2] == built_in["op_id"]
    assert without_package[1] != "operation_left_out"


async def _store_copy_of_wipe(embedding: AsyncMock, **changes: Any) -> dict[str, Any]:
    """Store a row with the key of the wipe op that the misfit start must not touch."""
    await _start(_widgets("fixed"), embedding=embedding)
    template = await _row(_WIPE)
    assert template is not None
    await _wipe_database()
    row = {k: v for k, v in template.items() if k not in ("id", "group_id")}
    row.update(changes)
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(insert(EndpointDescriptor), [row])
    stored = await _row(_WIPE, tenant_id=row["tenant_id"])
    assert stored is not None
    return stored


@pytest.mark.asyncio
async def test_case8_a_tenant_row_with_the_key_is_unchanged_and_decides_the_call(
    embedding: AsyncMock,
) -> None:
    stored = await _store_copy_of_wipe(
        embedding, tenant_id=_OTHER_TENANT, owner_package=None, summary="Tenant copy."
    )
    await _start(_widgets("fixed"), embedding=embedding)
    without_fault = await _call(_WIPE, tenant_id=_OTHER_TENANT)

    with structlog.testing.capture_logs() as logs:
        await _start(_widgets("misfit"), embedding=embedding)

    assert len(_left_out_lines(logs)) == 1
    assert await _row(_WIPE, tenant_id=_OTHER_TENANT) == stored
    tenant_call = await _call(_WIPE, tenant_id=_OTHER_TENANT)
    assert tenant_call.status == without_fault.status == "ok"
    assert tenant_call.result == without_fault.result
    searched, _listed, _groups = await _offered(tenant_id=_OTHER_TENANT)
    assert _WIPE in searched
    _assert_refused_as_left_out(await _call(_WIPE))


@pytest.mark.asyncio
async def test_case9_an_imported_row_with_the_key_is_unchanged(embedding: AsyncMock) -> None:
    stored = await _store_copy_of_wipe(
        embedding,
        tenant_id=None,
        owner_package=None,
        source_kind="ingested",
        handler_ref=None,
        summary="Imported from an API description.",
    )

    with structlog.testing.capture_logs() as logs:
        await _start(_widgets("misfit"), embedding=embedding)

    assert len(_left_out_lines(logs)) == 1
    assert await _row(_WIPE) == stored
    searched, _listed, _groups = await _offered()
    assert _WIPE in searched
    assert (await _call(_WIPE)).extras.get("error_code") != "operation_left_out"


@pytest.mark.asyncio
async def test_case10_a_row_of_another_package_with_the_key_is_unchanged(
    embedding: AsyncMock,
) -> None:
    await _start(probe_gadgets, embedding=embedding)
    stored = await _row(_WIPE)
    assert stored is not None and stored["owner_package"] == _GADGETS
    without_widgets = (await _call(_WIPE)).result
    assert without_widgets == {"from": "gadgets"}
    searched_without, listed_without, _ = await _offered()

    with structlog.testing.capture_logs() as logs:
        await _start(_widgets("misfit"), probe_gadgets, embedding=embedding)

    assert len(_left_out_lines(logs)) == 1
    after = await _row(_WIPE)
    assert after is not None
    assert {k: v for k, v in after.items() if k != "updated_at"} == {
        k: v for k, v in stored.items() if k != "updated_at"
    }
    call = await _call(_WIPE)
    assert call.status == "ok"
    assert call.result == without_widgets
    searched, listed, _ = await _offered()
    assert _WIPE in searched and _WIPE in searched_without
    assert (_WIPE in listed) == (_WIPE in listed_without)


# ---------------------------------------------------------------------------
# Point 4: a connector whose test read is left out starts without it
# ---------------------------------------------------------------------------


class _ProbeBase(Connector):
    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError


def _probe_class(monkeypatch: pytest.MonkeyPatch, test_operation: str) -> type[Connector]:
    cls = type("ProbeWidgetsConnector", (_ProbeBase,), {"test_operation": test_operation})
    monkeypatch.setitem(connector_registry._REGISTRY_V2, (_PRODUCT, _VERSION, _IMPL), cls)
    return cls


def _route_test_operation() -> Any:
    app = FastAPI()
    app.include_router(target_form_router)
    app.dependency_overrides[verify_jwt_and_bind] = lambda: _operator()
    resp = TestClient(app).get("/api/v1/connectors/target-form", params={"product": _PRODUCT})
    assert resp.status_code == 200, resp.text
    (answer,) = resp.json()["classes"]
    return answer["test_operation"]


@pytest.mark.parametrize("earlier_row", [False, True], ids=["fresh", "after_unmarked_start"])
@pytest.mark.asyncio
async def test_case4_a_left_out_test_read_leaves_the_connector_without_one(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock, earlier_row: bool
) -> None:
    expected = await _baseline(embedding)
    _probe_class(monkeypatch, _WIPE)
    if earlier_row:
        await _start(_widgets("fixed"), embedding=embedding)
        assert _route_test_operation() == _WIPE

    with structlog.testing.capture_logs() as logs:
        await _start(_widgets("misfit"), embedding=embedding)

    assert _comparable(await _rows(), leave_out=_WIPE) == expected
    [line] = _left_out_lines(logs)
    assert line["op_id"] == _WIPE
    [test_read_line] = [e for e in logs if e["event"] == "connector_test_read_left_out"]
    assert {
        k: test_read_line[k]
        for k in ("connector", "product", "version", "impl_id", "connector_package", "op_id")
    } == {
        "connector": "ProbeWidgetsConnector",
        "product": _PRODUCT,
        "version": _VERSION,
        "impl_id": _IMPL,
        "connector_package": _WIDGETS,
        "op_id": _WIPE,
    }
    assert "no test read" in test_read_line["reason"]
    assert (await _row(_WIPE) is not None) == earlier_row
    assert _route_test_operation() is None


@pytest.mark.asyncio
async def test_case4_a_test_read_no_registrar_writes_still_stops_the_start(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    _probe_class(monkeypatch, _NEVER)
    with pytest.raises(ConnectorDeclarationError, match=r"probewid\.box\.never.*no registrar"):
        await _start(_widgets("misfit"), embedding=embedding)


# ---------------------------------------------------------------------------
# Point 5: every other check of a registration runs for a left-out op too
# ---------------------------------------------------------------------------


_UNKNOWN_SWITCH_SCHEMA = {
    "type": "object",
    "properties": {"box": {"type": "string", "x-meho-switch": "made_up_switch"}},
}


@pytest.mark.parametrize("wipe", ["misfit", "multi_step"])
@pytest.mark.asyncio
async def test_a_left_out_op_with_an_unknown_switch_stops_the_start(
    embedding: AsyncMock, wipe: str
) -> None:
    with pytest.raises(ValueError, match="made_up_switch") as left_out:
        await _start(_widgets(wipe, parameter_schema=_UNKNOWN_SWITCH_SCHEMA), embedding=embedding)
    with pytest.raises(ValueError, match="made_up_switch") as good:
        await _start(
            _widgets("fixed", wipe_id=_LIST, parameter_schema=_UNKNOWN_SWITCH_SCHEMA),
            embedding=embedding,
        )
    assert str(left_out.value).replace(_WIPE, "<op>") == str(good.value).replace(_LIST, "<op>")
    assert await _rows() == []


# ---------------------------------------------------------------------------
# A second check in _MARK_FIT_CHECKS leaves its op out the same way
# ---------------------------------------------------------------------------


def _test_only_check(marks: Any) -> str | None:
    if marks.changes_rights and marks.safety_level == "safe":
        return "test-only check: changes_rights on a 'safe' operation"
    return None


@pytest.mark.asyncio
async def test_a_second_check_leaves_its_op_out_the_same_way(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    expected = await _baseline(embedding)
    monkeypatch.setattr(
        typed_register,
        "_MARK_FIT_CHECKS",
        (_test_only_check, *typed_register._MARK_FIT_CHECKS),
    )
    with structlog.testing.capture_logs() as logs:
        await _start(_widgets("fixed", changes_rights=True), embedding=embedding)

    assert await _row(_WIPE) is None
    assert _comparable(await _rows()) == expected
    [line] = _left_out_lines(logs)
    assert "test-only check" in line["reason"]
    _assert_refused_as_left_out(await _call(_WIPE))


# ---------------------------------------------------------------------------
# Every stored built-in row under a left-out key is hidden, in both kinds
# ---------------------------------------------------------------------------


async def _assert_stored_row_hidden(stored: dict[str, Any]) -> None:
    """*stored* is unchanged, hidden from every lookup, and no test read."""
    assert await _row(_WIPE) == stored
    found = await lookup_descriptor(
        tenant_id=_TENANT, product=_PRODUCT, version=_VERSION, impl_id=_IMPL, op_id=_WIPE
    )
    assert found is None
    searched, listed, _group_keys = await _offered()
    assert _WIPE not in searched | listed
    _assert_refused_as_left_out(await _call(_WIPE))
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.id == stored["id"])
            )
        ).scalar_one()
    assert _left_out.row_is_left_out(row)
    assert _route_test_operation() is None


@pytest.mark.asyncio
async def test_a_built_in_row_with_no_package_under_a_left_out_key_is_hidden(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    stored = await _store_copy_of_wipe(embedding, tenant_id=None, owner_package=None)
    _probe_class(monkeypatch, _WIPE)

    with structlog.testing.capture_logs() as logs:
        await _start(_widgets("misfit"), embedding=embedding)

    assert len(_left_out_lines(logs)) == 1
    await _assert_stored_row_hidden(stored)


@pytest.mark.parametrize(
    ("stored_version", "misfit_version", "stored_kind"),
    [("multi_step_fixed", "misfit", "composite"), ("fixed", "multi_step", "typed")],
    ids=["stored_multi_step_now_typed", "stored_typed_now_multi_step"],
)
@pytest.mark.asyncio
async def test_a_row_of_the_other_kind_under_a_left_out_key_is_hidden(
    monkeypatch: pytest.MonkeyPatch,
    embedding: AsyncMock,
    stored_version: str,
    misfit_version: str,
    stored_kind: str,
) -> None:
    await _start(_widgets(stored_version), embedding=embedding)
    stored = await _row(_WIPE)
    assert stored is not None
    assert (stored["source_kind"], stored["owner_package"]) == (stored_kind, _WIDGETS)
    _probe_class(monkeypatch, _WIPE)

    with structlog.testing.capture_logs() as logs:
        await _start(_widgets(misfit_version), embedding=embedding)

    assert len(_left_out_lines(logs)) == 1
    await _assert_stored_row_hidden(stored)


# ---------------------------------------------------------------------------
# The target form offers no test read that the start left out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_imported_row_under_a_left_out_test_read_is_no_test_read(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    stored = await _store_copy_of_wipe(
        embedding,
        tenant_id=None,
        owner_package=None,
        source_kind="ingested",
        handler_ref=None,
        safety_level="safe",
        is_enabled=True,
        summary="Imported from an API description.",
    )
    _probe_class(monkeypatch, _WIPE)

    with structlog.testing.capture_logs() as logs:
        await _start(_widgets("misfit"), embedding=embedding)

    assert [e["op_id"] for e in logs if e["event"] == "connector_test_read_left_out"] == [_WIPE]
    assert _route_test_operation() is None
    assert await _row(_WIPE) == stored
    found = await lookup_descriptor(
        tenant_id=_TENANT, product=_PRODUCT, version=_VERSION, impl_id=_IMPL, op_id=_WIPE
    )
    assert found is not None and found.id == stored["id"]
    assert (await _call(_WIPE)).extras.get("error_code") != "operation_left_out"


# ---------------------------------------------------------------------------
# A forwarded call hides a left-out operation as the dispatcher does
# ---------------------------------------------------------------------------


def _k8s_probe(hard_to_undo: str | None) -> _Registrar:
    """A ``probe-widgets`` registrar of one op under the Kubernetes connector."""

    async def probe_widgets_k8s(*, embedding_service: Any = None) -> None:
        await register_typed_operation(
            product="k8s",
            version="1.x",
            impl_id="k8s",
            op_id=_K8S_PROBE,
            handler=widgets_k8s_handler,
            summary="Lists the probe boxes of a cluster.",
            description="Lists the probe boxes of one cluster.",
            parameter_schema={"type": "object", "properties": {}},
            when_to_use=None,
            hard_to_undo=hard_to_undo,
            embedding_service=embedding_service,
        )

    return probe_widgets_k8s


@pytest.mark.asyncio
async def test_a_forwarded_call_of_a_left_out_op_does_not_reach_its_handler(
    embedding: AsyncMock,
) -> None:
    connector = KubernetesConnector()
    target = SimpleNamespace(name="probe-cluster")
    await _start(_k8s_probe(None), embedding=embedding)
    assert (await connector.execute(target, _K8S_PROBE, {})).status == "ok"
    assert len(_K8S_CALLS) == 1

    await _start(_k8s_probe("deletes"), embedding=embedding)

    assert await _row(_K8S_PROBE) is not None
    _assert_refused_as_left_out(await connector.execute(target, _K8S_PROBE, {}), _K8S_PROBE)
    shared = await dispatch(
        operator=_operator(), connector_id="k8s-1.x", op_id=_K8S_PROBE, target=None, params={}
    )
    _assert_refused_as_left_out(shared, _K8S_PROBE)
    assert len(_K8S_CALLS) == 1

    await _start(_k8s_probe(None), embedding=embedding)

    assert (await connector.execute(target, _K8S_PROBE, {})).status == "ok"
    assert len(_K8S_CALLS) == 2


# ---------------------------------------------------------------------------
# The left-out keys are current before anything can call an operation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_left_out_keys_are_current_inside_the_catalog_write(
    embedding: AsyncMock,
) -> None:
    await _start(_widgets("fixed"), embedding=embedding)
    assert await _row(_WIPE) is not None
    saved = list(typed_register._TYPED_OP_REGISTRARS)
    register_typed_op_registrar(_widgets("misfit"))
    try:
        async with typed_register.operation_catalog_write(embedding_service=embedding):
            inside = _left_out.left_out_ops()
    finally:
        typed_register._TYPED_OP_REGISTRARS[:] = saved

    assert {(op.op_id, op.owner_package) for op in inside} == {(_WIPE, _WIDGETS)}
