# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""DB-backed registration test for the ``installer.composite.sddc.bringup`` composite.

Runs :func:`register_installer_composite_operations` against the autouse-migrated
SQLite engine (a deterministic embedding stub replaces the ONNX pipeline) and
asserts the ``endpoint_descriptor`` row lands with the governed-write posture:
``source_kind="composite"``, ``safety_level="dangerous"``, ``requires_approval=True``,
a NULL tenant/method/path, and a resolvable handler ref. This catches a
registration-time failure (bad schema, op_id convention, when_to_use pairing) in
the fast unit lane instead of at integration-boot. Mirrors
``test_connectors_vmware_rest_composites_register``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.vcf_installer import (
    INSTALLER_BRINGUP_OP_ID,
    register_installer_composite_operations,
)
from meho_backplane.connectors.vcf_installer import bringup as _bringup
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.typed_register import (
    _TYPED_OP_REGISTRARS,
    derive_handler_ref,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Snapshot + restore the process-global registrar list and reset caches."""
    saved_registrars = list(_TYPED_OP_REGISTRARS)
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    _TYPED_OP_REGISTRARS[:] = saved_registrars


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so the upsert doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


async def _fetch_bringup_row() -> EndpointDescriptor | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        return (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id == INSTALLER_BRINGUP_OP_ID
                    )
                )
            )
            .scalars()
            .one_or_none()
        )


@pytest.mark.asyncio
async def test_registrar_lands_the_bringup_composite_row(
    stub_embedding_service: AsyncMock,
) -> None:
    """The registrar upserts exactly the bring-up composite, embedded once."""
    await register_installer_composite_operations(embedding_service=stub_embedding_service)
    row = await _fetch_bringup_row()
    assert row is not None
    assert row.op_id == INSTALLER_BRINGUP_OP_ID
    assert stub_embedding_service.encode_one.call_count == 1


@pytest.mark.asyncio
async def test_bringup_row_carries_governed_write_posture(
    stub_embedding_service: AsyncMock,
) -> None:
    """The row routes the composite branch and pops the approval queue."""
    await register_installer_composite_operations(embedding_service=stub_embedding_service)
    row = await _fetch_bringup_row()
    assert row is not None
    assert row.source_kind == "composite"
    assert row.safety_level == "dangerous"
    assert row.requires_approval is True
    assert row.tenant_id is None
    assert row.is_enabled is True
    # A composite carries no wire method/path — it dispatches its own sub-calls.
    assert row.method is None
    assert row.path is None


@pytest.mark.asyncio
async def test_bringup_row_handler_ref_resolves_to_the_handler(
    stub_embedding_service: AsyncMock,
) -> None:
    """The persisted handler ref points at the module-level composite handler."""
    await register_installer_composite_operations(embedding_service=stub_embedding_service)
    row = await _fetch_bringup_row()
    assert row is not None
    assert row.handler_ref == derive_handler_ref(_bringup.installer_sddc_bringup_composite)
