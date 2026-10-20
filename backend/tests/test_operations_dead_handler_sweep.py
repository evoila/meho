# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for retiring typed/composite rows whose handler is gone (#3866).

An older release can leave a built-in ``typed`` / ``composite`` row in
``endpoint_descriptor`` for an op the current code no longer ships. The
two ``vmware-rest-9.0`` composites re-shipped as typed ops are the known
case: discovery still listed them and every call failed with
``handler_unreachable``.

Covered here:

* :func:`retire_dead_handler_rows` deletes only built-in typed/composite
  rows listed in :data:`RETIRED_OPS`. Live rows keep their operator
  edits; ingested and tenant-scoped rows are not touched.
* An older server sharing the database with a newer release keeps the
  rows it does not know about, even though their handlers do not import
  on it, and a row an operator disabled stays disabled.
* The boot path (:func:`run_typed_op_registrars`) runs the sweep, so a
  stale row seeded before boot is gone after it.
* Conformance: every row the real registrars write has a handler the
  dispatcher can import, and no registered op is on the retired list.
  This fails CI if a registrar ever points at a missing handler.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import typed_register
from meho_backplane.operations._handler_resolve import import_handler
from meho_backplane.operations.dead_handler_sweep import (
    RETIRED_OPS,
    RetiredOp,
    handler_ref_resolves,
    retire_dead_handler_rows,
)
from meho_backplane.settings import get_settings

_LIVE_REF = "tests.fixtures.composites.handlers.composite_module_level_handler"
_DEAD_UPLINKS_REF = (
    "meho_backplane.connectors.vmware_rest.composites._read.host_network_uplinks_composite"
)
_DEAD_VSAN_REF = "meho_backplane.connectors.vmware_rest.composites._read.host_vsan_health_composite"
_NEWER_ONLY_REF = "meho_backplane.connectors.future_release.typed_ops.newer_only_handler"
_RETIRED_COMPOSITES = frozenset(
    {"vmware.composite.host.network_uplinks", "vmware.composite.host.vsan_health"}
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so the registrar pass doesn't load a model."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _seed(
    *,
    op_id: str,
    source_kind: str,
    handler_ref: str | None,
    tenant_id: uuid.UUID | None = None,
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
    custom_description: str | None = None,
    is_enabled: bool = True,
) -> None:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
                op_id=op_id,
                source_kind=source_kind,
                method="GET" if source_kind == "ingested" else None,
                path="/widgets" if source_kind == "ingested" else None,
                handler_ref=handler_ref,
                summary=f"row for {op_id}",
                description=f"row for {op_id}",
                tags=[],
                parameter_schema={"type": "object"},
                safety_level="safe",
                requires_approval=False,
                is_enabled=is_enabled,
                custom_description=custom_description,
                created_at=now,
                updated_at=now,
            )
        )


async def _rows() -> dict[tuple[uuid.UUID | None, str], EndpointDescriptor]:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(EndpointDescriptor))
        return {(r.tenant_id, r.op_id): r for r in result.scalars().all()}


def test_handler_ref_resolves() -> None:
    assert handler_ref_resolves(_LIVE_REF)
    assert not handler_ref_resolves(_DEAD_UPLINKS_REF)
    assert not handler_ref_resolves(_NEWER_ONLY_REF)
    assert not handler_ref_resolves(None)
    assert not handler_ref_resolves("")


def test_retired_list_names_the_two_vmware_composites() -> None:
    assert {op.op_id for op in RETIRED_OPS} == _RETIRED_COMPOSITES
    assert {(op.product, op.version, op.impl_id) for op in RETIRED_OPS} == {
        ("vmware", "9.0", "vmware-rest")
    }


@pytest.mark.asyncio
async def test_retire_deletes_only_listed_built_in_rows() -> None:
    tenant = uuid.uuid4()
    await _seed(
        op_id="vmware.composite.host.network_uplinks",
        source_kind="composite",
        handler_ref=_DEAD_UPLINKS_REF,
    )
    await _seed(
        op_id="vmware.composite.host.vsan_health",
        source_kind="composite",
        handler_ref=_DEAD_VSAN_REF,
    )
    await _seed(
        op_id="vmware.composite.host.vsan_health",
        source_kind="composite",
        handler_ref=_DEAD_VSAN_REF,
        product="other",
    )
    await _seed(
        op_id="vmware.typed.unlisted_dead",
        source_kind="typed",
        handler_ref="meho_backplane.no_such_module.handler",
    )
    await _seed(
        op_id="synth.composite.live",
        source_kind="composite",
        handler_ref=_LIVE_REF,
        custom_description="operator wording",
    )
    await _seed(op_id="GET:/widgets", source_kind="ingested", handler_ref=None)
    await _seed(
        op_id="tenant.composite.dead",
        source_kind="composite",
        handler_ref=_DEAD_UPLINKS_REF,
        tenant_id=tenant,
    )

    retired = await retire_dead_handler_rows()

    assert retired == [
        "vmware.composite.host.network_uplinks",
        "vmware.composite.host.vsan_health",
    ]
    rows = await _rows()
    assert set(rows) == {
        (None, "vmware.composite.host.vsan_health"),
        (None, "vmware.typed.unlisted_dead"),
        (None, "synth.composite.live"),
        (None, "GET:/widgets"),
        (tenant, "tenant.composite.dead"),
    }
    assert rows[(None, "vmware.composite.host.vsan_health")].product == "other"
    live = rows[(None, "synth.composite.live")]
    assert live.custom_description == "operator wording"
    assert live.is_enabled is True


@pytest.mark.asyncio
async def test_retire_is_a_no_op_when_every_handler_resolves() -> None:
    await _seed(op_id="synth.composite.live", source_kind="composite", handler_ref=_LIVE_REF)

    assert await retire_dead_handler_rows() == []
    assert set(await _rows()) == {(None, "synth.composite.live")}


@pytest.mark.asyncio
async def test_older_server_keeps_disabled_op_of_newer_release() -> None:
    """An older server restarting next to a newer release deletes nothing.

    This process stands in for the older server: the newer release's
    handler does not import here and the op is not on this release's
    retired list. The row stays, and the operator's ``is_enabled=False``
    and wording stay with it.
    """
    await _seed(
        op_id="vmware.typed.newer_only",
        source_kind="typed",
        handler_ref=_NEWER_ONLY_REF,
        custom_description="disabled by operator",
        is_enabled=False,
    )

    assert await retire_dead_handler_rows() == []

    rows = await _rows()
    kept = rows[(None, "vmware.typed.newer_only")]
    assert kept.is_enabled is False
    assert kept.custom_description == "disabled by operator"
    assert kept.handler_ref == _NEWER_ONLY_REF


@pytest.mark.asyncio
async def test_boot_registrar_run_removes_retired_vmware_composites(
    stub_embedding_service: AsyncMock,
) -> None:
    """An upgraded install's stale composite rows are gone after boot.

    The first registrar run stands in for the earlier boot. Seeding the
    stale rows only after it also keeps them out of the per-process
    registrar snapshot the test suite reuses between boots.
    """
    await typed_register.run_typed_op_registrars(embedding_service=stub_embedding_service)
    await _seed(
        op_id="vmware.composite.host.network_uplinks",
        source_kind="composite",
        handler_ref=_DEAD_UPLINKS_REF,
    )
    await _seed(
        op_id="vmware.composite.host.vsan_health",
        source_kind="composite",
        handler_ref=_DEAD_VSAN_REF,
    )
    await _seed(
        op_id="vmware.typed.newer_only",
        source_kind="typed",
        handler_ref=_NEWER_ONLY_REF,
        is_enabled=False,
    )

    await typed_register.run_typed_op_registrars(embedding_service=stub_embedding_service)

    rows = await _rows()
    op_ids = {op_id for _tenant, op_id in rows}
    assert op_ids.isdisjoint(_RETIRED_COMPOSITES)
    assert {"vmware.host.network_uplinks", "vmware.host.vsan_health"} <= op_ids
    assert rows[(None, "vmware.typed.newer_only")].is_enabled is False


@pytest.mark.asyncio
async def test_every_registered_typed_and_composite_op_has_an_importable_handler(
    stub_embedding_service: AsyncMock,
) -> None:
    """Conformance: no registrar writes a row the dispatcher cannot run.

    Runs the registrars without the retirement sweep, so a row pointing at
    a missing handler would still be here and fail this test.
    """
    await typed_register._run_typed_op_registrars(embedding_service=stub_embedding_service)

    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(
                EndpointDescriptor.product,
                EndpointDescriptor.version,
                EndpointDescriptor.impl_id,
                EndpointDescriptor.op_id,
                EndpointDescriptor.handler_ref,
            ).where(EndpointDescriptor.source_kind.in_(("typed", "composite")))
        )
        rows = result.all()

    assert rows, "no typed/composite rows registered; the check would pass vacuously"
    unreachable: list[str] = []
    for _product, _version, _impl_id, op_id, handler_ref in rows:
        try:
            import_handler(handler_ref)
        except (ImportError, TypeError) as exc:
            unreachable.append(f"{op_id} -> {handler_ref}: {exc}")
    assert not unreachable, "ops advertised with no importable handler:\n" + "\n".join(
        sorted(unreachable)
    )
    registered = {RetiredOp(r.product, r.version, r.impl_id, r.op_id) for r in rows}
    assert registered.isdisjoint(RETIRED_OPS), "a registrar still writes a retired op"
