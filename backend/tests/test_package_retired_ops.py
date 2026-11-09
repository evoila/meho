# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector package lists the ops it removed; MEHO deletes their rows at start.

A package built in another repository cannot add to ``RETIRED_OPS``. It
calls the kit's ``register_retired_operations`` instead. Covered here:

* a listed row that the package wrote is deleted once, with one
  ``dead_handler_op_retired`` line, and the next sweep logs nothing for it;
* the start (``run_typed_op_registrars``) runs it;
* a listed entry that names a built-in row or a row of another package
  deletes nothing, and neither does a listing from a folder that is not
  part of an installed package;
* an unlisted row whose handler does not import is still kept;
* the call checks its inputs, and both names are in the kit.

All package names are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import meho_connector_kit as kit
import pytest
import structlog
from sqlalchemy import select

from meho_backplane import connector_packages
from meho_backplane.connector_packages import record_installed_connector_packages
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dead_handler_sweep as sweep_mod
from meho_backplane.operations import typed_register
from meho_backplane.operations.dead_handler_sweep import (
    RetiredOp,
    register_retired_operations,
    retire_dead_handler_rows,
)
from meho_backplane.settings import get_settings

_FOLDER = "meho_backplane.connectors.widget_pkg"
_OTHER_FOLDER = "meho_backplane.connectors.gadget_pkg"
_PACKAGE = "example-widgets"
_OTHER_PACKAGE = "example-gadgets"
_GONE_REF = "meho_backplane.connectors.widget_pkg.ops.removed_handler"
_GONE = RetiredOp("widget", "2.x", "widget-api", "widget.item.legacy_export")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two made-up packages are installed; no package listed anything yet."""
    monkeypatch.setattr(connector_packages, "_installed_folders", {})
    monkeypatch.setattr(sweep_mod, "_PACKAGE_RETIRED_OPS", {})
    monkeypatch.setattr(sweep_mod, "_log", structlog.get_logger(sweep_mod.__name__))
    record_installed_connector_packages({"widget_pkg": _PACKAGE, "gadget_pkg": _OTHER_PACKAGE})


async def _seed(
    op: RetiredOp,
    *,
    owner_package: str | None,
    handler_ref: str = _GONE_REF,
) -> None:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product=op.product,
                version=op.version,
                impl_id=op.impl_id,
                op_id=op.op_id,
                source_kind="typed",
                handler_ref=handler_ref,
                owner_package=owner_package,
                summary=f"row for {op.op_id}",
                description=f"row for {op.op_id}",
                tags=[],
                parameter_schema={"type": "object"},
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                created_at=now,
                updated_at=now,
            )
        )


async def _rows() -> set[tuple[str, str, str | None]]:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(EndpointDescriptor))
        return {(r.product, r.op_id, r.owner_package) for r in result.scalars().all()}


def _events_for(logs: list[dict[str, object]], op_id: str) -> list[object]:
    return [e["event"] for e in logs if e.get("op_id") == op_id]


@pytest.mark.asyncio
async def test_listed_package_row_is_deleted_once() -> None:
    await _seed(_GONE, owner_package=_PACKAGE)
    register_retired_operations(_FOLDER, [_GONE])

    with structlog.testing.capture_logs() as first:
        assert await retire_dead_handler_rows() == [_GONE.op_id]
    assert await _rows() == set()
    retired = [e for e in first if e.get("op_id") == _GONE.op_id]
    assert [e["event"] for e in retired] == ["dead_handler_op_retired"]
    assert retired[0]["owner_package"] == _PACKAGE

    with structlog.testing.capture_logs() as second:
        assert await retire_dead_handler_rows() == []
    assert _events_for(second, _GONE.op_id) == []


@pytest.mark.asyncio
async def test_start_deletes_the_listed_package_row() -> None:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await typed_register.run_typed_op_registrars(embedding_service=service)
    await _seed(_GONE, owner_package=_PACKAGE)
    register_retired_operations(_FOLDER, [_GONE])

    await typed_register.run_typed_op_registrars(embedding_service=service)

    assert not {row for row in await _rows() if row[1] == _GONE.op_id}


@pytest.mark.asyncio
async def test_listing_never_deletes_core_or_other_package_rows() -> None:
    core = RetiredOp("widget", "2.x", "widget-api", "widget.core.health")
    other = RetiredOp("gadget", "1.x", "gadget-api", "gadget.item.list")
    await _seed(core, owner_package=None)
    await _seed(other, owner_package=_OTHER_PACKAGE)
    register_retired_operations(_FOLDER, [core, other])

    with structlog.testing.capture_logs() as logs:
        assert await retire_dead_handler_rows() == []

    assert await _rows() == {
        ("widget", core.op_id, None),
        ("gadget", other.op_id, _OTHER_PACKAGE),
    }
    assert _events_for(logs, core.op_id) == ["dead_handler_op_kept"]
    assert _events_for(logs, other.op_id) == ["dead_handler_op_kept"]


@pytest.mark.asyncio
async def test_same_op_listed_by_two_packages_deletes_only_each_own_row() -> None:
    await _seed(_GONE, owner_package=_PACKAGE)
    register_retired_operations(_OTHER_FOLDER, [_GONE])

    assert await retire_dead_handler_rows() == []
    assert await _rows() == {("widget", _GONE.op_id, _PACKAGE)}


@pytest.mark.asyncio
async def test_listing_from_a_folder_without_a_package_deletes_nothing() -> None:
    await _seed(_GONE, owner_package=None)
    register_retired_operations("meho_backplane.connectors.builtin_widget", [_GONE])

    with structlog.testing.capture_logs() as logs:
        assert await retire_dead_handler_rows() == []

    assert await _rows() == {("widget", _GONE.op_id, None)}
    assert [e for e in logs if e["event"] == "package_retired_ops_ignored"]


@pytest.mark.asyncio
async def test_unlisted_dead_package_row_is_kept() -> None:
    await _seed(_GONE, owner_package=_PACKAGE)
    register_retired_operations(_FOLDER, [])

    with structlog.testing.capture_logs() as logs:
        assert await retire_dead_handler_rows() == []

    assert await _rows() == {("widget", _GONE.op_id, _PACKAGE)}
    assert _events_for(logs, _GONE.op_id) == ["dead_handler_op_kept"]


def test_register_checks_its_inputs() -> None:
    with pytest.raises(ValueError, match="not a connector folder"):
        register_retired_operations("example_widgets.ops", [_GONE])
    with pytest.raises(TypeError, match="not a RetiredOp"):
        register_retired_operations(_FOLDER, [("widget", "2.x", "widget-api", "widget.x")])
    with pytest.raises(TypeError, match="not a RetiredOp"):
        register_retired_operations(_FOLDER, [RetiredOp("widget", "2.x", "widget-api", 7)])
    assert sweep_mod._PACKAGE_RETIRED_OPS == {}


def test_calls_add_to_the_list() -> None:
    second = RetiredOp("widget", "2.x", "widget-api", "widget.item.old_report")
    register_retired_operations(_FOLDER, [_GONE])
    register_retired_operations(_FOLDER, [second])
    assert {_FOLDER: frozenset({_GONE, second})} == sweep_mod._PACKAGE_RETIRED_OPS


def test_kit_has_both_names() -> None:
    assert kit.RetiredOp is RetiredOp
    assert kit.register_retired_operations is register_retired_operations
