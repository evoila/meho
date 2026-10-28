# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A failed start changes no operation row (#5019).

Every start writes the built-in operation rows (which code runs, the
parameter rules, the risk level, the approval flag). Pods of an older
release may still serve those rows during an update. So the write must be
all or nothing:

* A start that fails at a later start-up check changes no row.
* A start that fails while it starts its background loops changes no
  row, and the loops it already started are stopped.
* A registrar that fails in the middle of the run changes no row, also
  not the rows earlier registrars wrote.
* A check that fails after every registrar wrote (the invariant sweep)
  changes no row, and the retirement sweep's delete is undone too.
* After a good start, the stored release names the release that wrote
  the rows, and a pod of another release sees that it differs.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationCatalogState
from meho_backplane.operations import typed_register
from meho_backplane.operations.catalog_release import catalog_release_status
from meho_backplane.operations.dead_handler_sweep import RETIRED_OPS
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings

_PRODUCT = "catalogtest"
_VERSION = "1"
_IMPL_ID = "catalogtest-1"
_OP_ID = "catalogtest.widget.read"

#: What the test registrar writes on its next run. Tests change it
#: between two runs to stand in for a newer release.
_NEXT: dict[str, Any] = {}


async def handler_old(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Handler of the "old release"."""
    return {}


async def handler_new(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Handler of the "new release"."""
    return {}


async def widget_registrar(*, embedding_service: Any = None) -> None:
    """Register one op with the values in :data:`_NEXT`."""
    await register_typed_operation(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=_OP_ID,
        handler=_NEXT["handler"],
        summary="Read a widget",
        description="Reads one widget.",
        parameter_schema=_NEXT["parameter_schema"],
        when_to_use=None,
        safety_level=_NEXT["safety_level"],
        requires_approval=_NEXT["requires_approval"],
        embedding_service=embedding_service,
    )


async def failing_registrar(*, embedding_service: Any = None) -> None:
    """A registrar that fails, like a connector's start-up step with a bug."""
    raise RuntimeError("registrar failed on purpose")


def _set_old_release() -> None:
    _NEXT.update(
        handler=handler_old,
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=False,
    )


def _set_new_release() -> None:
    _NEXT.update(
        handler=handler_new,
        parameter_schema={"type": "object", "required": ["name"]},
        safety_level="dangerous",
        requires_approval=True,
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Required settings, a real registrar pass (no snapshot replay), a fixed release."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS", "false")
    monkeypatch.setenv("GIT_SHA", "old0000000000000000000000000000000000000")
    _set_old_release()
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


def _use_registrars(monkeypatch: pytest.MonkeyPatch, *registrars: Any) -> None:
    monkeypatch.setattr(typed_register, "_TYPED_OP_REGISTRARS", list(registrars))


async def _built_in_rows() -> dict[str, dict[str, Any]]:
    """Every built-in typed/composite row, keyed by op_id, as plain values."""
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.tenant_id.is_(None),
                        EndpointDescriptor.source_kind.in_(("typed", "composite")),
                    )
                )
            )
            .scalars()
            .all()
        )
    return {
        row.op_id: {
            "handler_ref": row.handler_ref,
            "parameter_schema": row.parameter_schema,
            "safety_level": row.safety_level,
            "requires_approval": row.requires_approval,
            "is_enabled": row.is_enabled,
            "updated_at": row.updated_at,
        }
        for row in rows
    }


async def _stored_release() -> tuple[str, datetime] | None:
    async with get_sessionmaker()() as session:
        row = await session.get(OperationCatalogState, 1)
    return None if row is None else (row.written_by_release, row.written_at)


async def _seed_retired_row() -> str:
    """Seed a row from the retired list; a successful sweep deletes it."""
    retired = sorted(RETIRED_OPS)[0]
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product=retired.product,
                version=retired.version,
                impl_id=retired.impl_id,
                op_id=retired.op_id,
                source_kind="composite",
                method=None,
                path=None,
                handler_ref="meho_backplane.connectors.gone.handler",
                summary="retired row",
                description="retired row",
                tags=[],
                parameter_schema={"type": "object"},
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=[0.1] * 384,
                created_at=now,
                updated_at=now,
            )
        )
    return retired.op_id


@pytest.mark.asyncio
async def test_registrar_failing_mid_run_changes_no_row(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
) -> None:
    """One registrar fails after another wrote new values: nothing changed."""
    _use_registrars(monkeypatch, widget_registrar)
    await typed_register.run_typed_op_registrars(embedding_service=stub_embedding_service)
    before = await _built_in_rows()
    stored_before = await _stored_release()
    assert before[_OP_ID]["safety_level"] == "safe"
    assert before[_OP_ID]["handler_ref"].endswith(".handler_old")

    # A "new release" whose second registrar fails.
    monkeypatch.setenv("GIT_SHA", "new0000000000000000000000000000000000000")
    _set_new_release()
    _use_registrars(monkeypatch, widget_registrar, failing_registrar)
    with pytest.raises(RuntimeError, match="registrar failed on purpose"):
        await typed_register.run_typed_op_registrars(embedding_service=stub_embedding_service)

    assert await _built_in_rows() == before
    assert await _stored_release() == stored_before


@pytest.mark.asyncio
async def test_check_failing_after_all_writes_changes_no_row(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
) -> None:
    """The last check fails after every registrar and the sweep ran: nothing changed."""
    _use_registrars(monkeypatch, widget_registrar)
    await typed_register.run_typed_op_registrars(embedding_service=stub_embedding_service)
    retired_op_id = await _seed_retired_row()
    before = await _built_in_rows()
    stored_before = await _stored_release()
    assert retired_op_id in before

    monkeypatch.setenv("GIT_SHA", "new0000000000000000000000000000000000000")
    _set_new_release()
    monkeypatch.setattr(
        typed_register,
        "assert_registered_composites_have_no_ingested_dispatch",
        AsyncMock(side_effect=RuntimeError("invariant failed on purpose")),
    )
    with pytest.raises(RuntimeError, match="invariant failed on purpose"):
        await typed_register.run_typed_op_registrars(embedding_service=stub_embedding_service)

    after = await _built_in_rows()
    assert after == before
    assert retired_op_id in after
    assert await _stored_release() == stored_before


@pytest.mark.asyncio
async def test_failed_registration_logs_that_no_row_changed(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
) -> None:
    """The operator sees in the log that the rows were left as they were."""
    _use_registrars(monkeypatch, widget_registrar, failing_registrar)
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(RuntimeError),
    ):
        await typed_register.run_typed_op_registrars(embedding_service=stub_embedding_service)
    assert any(entry["event"] == "operation_catalog_unchanged" for entry in logs)


@pytest.mark.asyncio
async def test_good_start_records_the_release_and_another_release_notices(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
) -> None:
    """After a good start the stored release is this one; a pod of another release says so."""
    status = await catalog_release_status()
    assert status.state == "not_recorded"

    _use_registrars(monkeypatch, widget_registrar)
    await typed_register.run_typed_op_registrars(embedding_service=stub_embedding_service)

    status = await catalog_release_status()
    assert status.state == "same_release"
    assert status.written_by_release == "old0000000000000000000000000000000000000"
    assert status.written_at is not None

    monkeypatch.setenv("GIT_SHA", "new0000000000000000000000000000000000000")
    with structlog.testing.capture_logs() as logs:
        status = await catalog_release_status()
    assert status.state == "other_release"
    assert status.this_release == "new0000000000000000000000000000000000000"
    assert status.written_by_release == "old0000000000000000000000000000000000000"
    assert [entry["event"] for entry in logs] == ["operation_catalog_written_by_other_release"]

    # A second good start of the new release takes the rows over.
    await typed_register.run_typed_op_registrars(embedding_service=stub_embedding_service)
    assert (await catalog_release_status()).state == "same_release"


def test_start_that_fails_after_registration_step_changes_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The issue's scenario: a new release with changed settings fails to start.

    First start: the "old release" writes its rows. Second start: a "new
    release" with a changed risk level, approval flag, parameter rules
    and handler, and no MCP address set, so a start-up check refuses the
    start. The rows still equal the copy taken after the first start.
    """
    _use_registrars(monkeypatch, widget_registrar)
    from meho_backplane.main import app

    with TestClient(app):
        pass
    before = asyncio.run(_built_in_rows())
    stored_before = asyncio.run(_stored_release())
    assert before[_OP_ID]["safety_level"] == "safe"
    assert stored_before is not None

    monkeypatch.setenv("GIT_SHA", "new0000000000000000000000000000000000000")
    monkeypatch.setenv("BACKPLANE_URL", "")
    monkeypatch.setenv("MCP_RESOURCE_URI", "")
    get_settings.cache_clear()
    _set_new_release()
    with pytest.raises(RuntimeError, match="audience_not_configured"), TestClient(app):
        pass

    assert asyncio.run(_built_in_rows()) == before
    assert asyncio.run(_stored_release()) == stored_before


def test_start_that_fails_while_starting_background_loops_changes_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rows were written, then the start of a background loop fails: nothing changed.

    The background loops start after the operation rows are written. So
    the rows must commit only when every loop started. The first loop
    starts, the last one fails: the rows and the release stamp still
    equal the copy from the first start, and the first loop is stopped
    again.
    """
    from meho_backplane import main

    _use_registrars(monkeypatch, widget_registrar)
    with TestClient(main.app):
        pass
    before = asyncio.run(_built_in_rows())
    stored_before = asyncio.run(_stored_release())
    assert before[_OP_ID]["safety_level"] == "safe"
    assert stored_before is not None

    started: list[asyncio.Task[None]] = []
    real_start_first_loop = main.start_topology_refresh_scheduler

    def start_first_loop() -> asyncio.Task[None]:
        task = real_start_first_loop()
        started.append(task)
        return task

    def start_last_loop_fails() -> asyncio.Task[None]:
        raise RuntimeError("background loop failed on purpose")

    monkeypatch.setenv("GIT_SHA", "new0000000000000000000000000000000000000")
    get_settings.cache_clear()
    _set_new_release()
    monkeypatch.setattr(main, "start_topology_refresh_scheduler", start_first_loop)
    monkeypatch.setattr(main, "start_gateway_unreported_mint_sweeper", start_last_loop_fails)
    with (
        pytest.raises(RuntimeError, match="background loop failed on purpose"),
        TestClient(main.app),
    ):
        pass

    assert asyncio.run(_built_in_rows()) == before
    assert asyncio.run(_stored_release()) == stored_before
    assert len(started) == 1
    assert started[0].cancelled()


@pytest.mark.asyncio
async def test_every_shipped_registrar_writes_through_the_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
) -> None:
    """The real registrar set, then a failing check: no built-in row exists.

    Guards the all-or-nothing rule against a connector whose start-up step
    opens its own session and commits: its rows would survive the rollback.
    """
    from meho_backplane.db.models import OperationGroup

    assert typed_register._TYPED_OP_REGISTRARS, "connector registrars were not imported"
    real_check = typed_register.assert_registered_composites_have_no_ingested_dispatch
    monkeypatch.setattr(
        typed_register,
        "assert_registered_composites_have_no_ingested_dispatch",
        AsyncMock(side_effect=RuntimeError("invariant failed on purpose")),
    )
    with pytest.raises(RuntimeError, match="invariant failed on purpose"):
        await typed_register.run_typed_op_registrars(embedding_service=stub_embedding_service)

    assert await _built_in_rows() == {}
    async with get_sessionmaker()() as session:
        built_in_groups = select(OperationGroup).where(OperationGroup.tenant_id.is_(None))
        groups = (await session.execute(built_in_groups)).scalars().all()
    assert groups == []
    assert await _stored_release() is None

    # The same set without the failing check does write rows, so the
    # empty result above is the rollback and not an idle registrar set.
    monkeypatch.setattr(
        typed_register, "assert_registered_composites_have_no_ingested_dispatch", real_check
    )
    await typed_register.run_typed_op_registrars(embedding_service=stub_embedding_service)
    assert len(await _built_in_rows()) > 50


@pytest.mark.asyncio
async def test_health_says_unknown_when_the_database_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database error does not break the health answer; the state is ``unknown``."""
    from meho_backplane.api.v1 import health

    monkeypatch.setattr(
        health, "catalog_release_status", AsyncMock(side_effect=OSError("database down"))
    )
    status = await health._operation_catalog_status()
    assert status.state == "unknown"
    assert status.this_release == "old0000000000000000000000000000000000000"
    assert status.written_by_release is None
