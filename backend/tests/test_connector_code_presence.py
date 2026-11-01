# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector whose code is not in this MEHO is not usable (#5585).

Its typed rows have no package name (``owner_package`` is ``NULL``), none
of their handlers imports, and no connector class resolves. Then:

* the connector list marks it ``code_not_installed``, not dispatchable,
  with zero counts and a next step;
* search and groups offer none of its operations, and search answers
  ``code_not_installed``;
* a call and a preview answer ``connector_code_not_installed``;
* nothing is deleted, so it all works again when the code is back.

A family with no class whose handlers import stays usable. Rows with a
package name keep ``package_not_installed``. The names are made up.
"""

from __future__ import annotations

import sys
import types
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane import connector_packages
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connector_packages import record_installed_connector_packages
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.mcp.tools.connector_admin import list_connector_rows
from meho_backplane.operations import dispatch
from meho_backplane.operations._code_presence import handler_imports
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.ingest.api_schemas import ConnectorListItem
from meho_backplane.operations.ingest.list_connectors import list_ingested_connectors
from meho_backplane.operations.meta_tools import list_operation_groups, search_operations
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-000000005585")

_PRODUCT = "gizmo"
_VERSION = "2"
_IMPL_ID = "gizmo-api"
_CONNECTOR_ID = "gizmo-api-2"
_GONE_MODULE = "tests.fixtures.gizmo_code_not_here"

_OP_ON = "gizmo.health.read"
_OP_OFF = "gizmo.health.reset"

_LIVE_REF = "tests.fixtures.composites.handlers.typed_sub_op_handler"
_PACKAGE = "example-connectors"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_process() -> Iterator[None]:
    """Each test starts like a new MEHO process: no import answer is kept."""
    handler_imports.cache_clear()
    yield
    handler_imports.cache_clear()


@pytest.fixture(autouse=True)
def _installed_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with no package installed; restore the real state after."""
    monkeypatch.setattr(connector_packages, "_installed_folders", {})


@pytest.fixture(autouse=True)
def _stub_embedding_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``search_operations`` off the real embedding model."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)


def _operator() -> Operator:
    return Operator(
        sub="op-5585",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


def _gone_ref(op_id: str) -> str:
    return f"{_GONE_MODULE}.{op_id.replace('.', '_')}"


async def _seed(
    *,
    product: str = _PRODUCT,
    version: str = _VERSION,
    impl_id: str = _IMPL_ID,
    refs: dict[str, str],
    owner: str | None = None,
) -> None:
    """One enabled group; one row per op. The operator turned off ``_OP_OFF``
    and made ``_OP_ON`` stricter, with a note."""
    now = datetime.now(UTC)
    group_id = uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=None,
                product=product,
                version=version,
                impl_id=impl_id,
                group_key="health",
                name="health",
                when_to_use="health ops.",
                review_status="enabled",
            )
        )
        for op_id, ref in refs.items():
            session.add(
                EndpointDescriptor(
                    id=uuid.uuid4(),
                    tenant_id=None,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                    op_id=op_id,
                    source_kind="typed",
                    method=None,
                    path=None,
                    handler_ref=ref,
                    summary=f"gizmo op {op_id}",
                    description="gizmo health op",
                    group_id=group_id,
                    tags=[],
                    parameter_schema={"type": "object"},
                    safety_level="caution" if op_id == _OP_ON else "safe",
                    requires_approval=False,
                    is_enabled=op_id != _OP_OFF,
                    custom_notes="operator note" if op_id == _OP_ON else None,
                    owner_package=owner,
                    created_at=now,
                    updated_at=now,
                )
            )


async def _seed_missing_code() -> None:
    await _seed(refs={_OP_ON: _gone_ref(_OP_ON), _OP_OFF: _gone_ref(_OP_OFF)})


async def _list_row(connector_id: str = _CONNECTOR_ID) -> ConnectorListItem:
    items = await list_ingested_connectors(operator=_operator())
    rows = [item for item in items if item.connector_id == connector_id]
    assert len(rows) == 1
    return rows[0]


async def _search(connector_id: str = _CONNECTOR_ID, **extra: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {"connector_id": connector_id, "query": "gizmo health", **extra}
    return await search_operations(_operator(), arguments)


async def _groups(connector_id: str = _CONNECTOR_ID) -> list[str]:
    result = await list_operation_groups(_operator(), {"connector_id": connector_id})
    return [g["group_key"] for g in result["groups"]]


async def _call(op_id: str, connector_id: str = _CONNECTOR_ID) -> Any:
    return await dispatch(
        operator=_operator(), connector_id=connector_id, op_id=op_id, target=None, params={}
    )


async def _rows() -> dict[str, EndpointDescriptor]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(EndpointDescriptor))).scalars().all()
    return {row.op_id: row for row in rows}


def _bring_code_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the handler module importable, as in a MEHO image that has the code."""

    async def gizmo_health_read(target: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {"healthy": True}

    async def gizmo_health_reset(target: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {"reset": True}

    module = types.ModuleType(_GONE_MODULE)
    module.gizmo_health_read = gizmo_health_read  # type: ignore[attr-defined]
    module.gizmo_health_reset = gizmo_health_reset  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _GONE_MODULE, module)
    # A new process: nothing is kept from the run without the code.
    handler_imports.cache_clear()


@pytest.mark.asyncio
async def test_list_marks_a_connector_whose_code_is_missing() -> None:
    await _seed_missing_code()

    row = await _list_row()
    assert row.state == "code_not_installed"
    assert row.dispatchable is False
    assert (row.group_count, row.enabled_group_count) == (0, 0)
    assert (row.operation_count, row.enabled_operation_count) == (0, 0)
    assert row.next_step is not None
    assert "code of this connector is not in this MEHO" in row.next_step.rationale

    mcp = await list_connector_rows(_operator(), {})
    short = [r for r in mcp["connectors"] if r["connector_id"] == _CONNECTOR_ID]
    assert [(r["status"], r["dispatchable"]) for r in short] == [("code_not_installed", False)]

    # Only the "all" view shows it: there is nothing to review or use.
    enabled = await list_ingested_connectors(operator=_operator(), status="enabled")
    assert _CONNECTOR_ID not in {item.connector_id for item in enabled}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra", [{}, {"group": "health"}, {"op_id": _OP_ON}], ids=["ranked", "group", "exact-op"]
)
async def test_search_and_groups_offer_nothing(extra: dict[str, Any]) -> None:
    await _seed_missing_code()

    result = await _search(**extra)
    assert result["hits"] == []
    assert result["outcome"] == "code_not_installed"
    assert "code of this connector is not in this MEHO" in result["advisory"]
    assert "enable" not in result["advisory"]
    assert await _groups() == []


@pytest.mark.asyncio
async def test_call_and_preview_name_the_missing_code() -> None:
    await _seed_missing_code()

    for op_id in (_OP_ON, _OP_OFF, "gizmo.nothing.here"):
        result = await _call(op_id)
        assert result.status == "error"
        assert result.extras["error_code"] == "connector_code_not_installed"
        assert result.extras["connector_id"] == _CONNECTOR_ID
        assert result.error.startswith("connector_code_not_installed:")
        assert _CONNECTOR_ID in result.error

    preview = await preview_dispatch(
        operator=_operator(), connector_id=_CONNECTOR_ID, op_id=_OP_ON, target=None, params={}
    )
    assert preview["status"] == "error"
    assert preview["extras"]["error_code"] == "connector_code_not_installed"


@pytest.mark.asyncio
async def test_code_back_makes_the_same_rows_usable_with_operator_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_missing_code()
    assert (await _list_row()).state == "code_not_installed"
    before = {op_id: (row.id, row.updated_at) for op_id, row in (await _rows()).items()}

    _bring_code_back(monkeypatch)

    row = await _list_row()
    assert row.state == "ingested"
    assert (row.kind, row.dispatchable) == ("typed", True)
    assert (row.operation_count, row.enabled_operation_count) == (2, 1)
    assert await _groups() == ["health"]
    result = await _search()
    assert {hit["op_id"] for hit in result["hits"]} == {_OP_ON}

    rows = await _rows()
    assert {op_id: (r.id, r.updated_at) for op_id, r in rows.items()} == before
    assert rows[_OP_ON].custom_notes == "operator note"
    assert rows[_OP_ON].safety_level == "caution"
    assert rows[_OP_OFF].is_enabled is False

    called = await _call(_OP_ON)
    assert called.status == "ok", called.error
    off = await _call(_OP_OFF)
    assert off.extras["error_code"] == "unknown_op"


@pytest.mark.asyncio
async def test_family_with_no_class_and_live_handlers_stays_usable() -> None:
    await _seed(product="probe", version="1.x", impl_id="probe", refs={_OP_ON: _LIVE_REF})

    row = await _list_row("probe-1.x")
    assert row.state == "ingested"
    assert (row.kind, row.dispatchable) == ("typed", True)
    assert row.next_step is None
    assert await _groups("probe-1.x") == ["health"]
    result = await _search("probe-1.x")
    assert result["outcome"] != "code_not_installed"
    assert {hit["op_id"] for hit in result["hits"]} == {_OP_ON}


@pytest.mark.asyncio
async def test_rows_with_no_handler_ref_say_nothing_about_code() -> None:
    """A typed row with no ``handler_ref`` points at no code, so it is not hit."""
    await _seed(refs={_OP_ON: _gone_ref(_OP_ON)})
    async with get_sessionmaker()() as session, session.begin():
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP_ON)
            )
        ).scalar_one()
        row.handler_ref = None

    assert (await _list_row()).state == "ingested"
    assert (await _search())["outcome"] != "code_not_installed"


@pytest.mark.asyncio
async def test_one_live_handler_keeps_the_connector_usable() -> None:
    """Two releases share one database: a newer op whose handler is not here
    does not hide the connector; only that call fails."""
    await _seed(refs={_OP_ON: _LIVE_REF, "gizmo.health.newer": _gone_ref("gizmo.health.newer")})

    row = await _list_row()
    assert row.state == "ingested"
    assert row.dispatchable is True
    newer = await _call("gizmo.health.newer")
    assert newer.extras["error_code"] == "handler_unreachable"


@pytest.mark.asyncio
async def test_rows_with_a_package_name_keep_package_not_installed() -> None:
    await _seed(refs={_OP_ON: _gone_ref(_OP_ON)}, owner=_PACKAGE)

    row = await _list_row()
    assert row.state == "package_not_installed"
    assert _PACKAGE in (row.next_step.rationale if row.next_step else "")
    result = await _call(_OP_ON)
    assert result.extras["error_code"] == "connector_package_not_installed"
    assert (await _search())["outcome"] == "package_not_installed"

    # With the package installed the package rule decides, as before.
    record_installed_connector_packages({"gizmo_pkg": _PACKAGE})
    assert (await _list_row()).state == "ingested"
