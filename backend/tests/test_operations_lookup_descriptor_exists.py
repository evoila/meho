# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""DB-backed tests for the ``is_enabled``-agnostic descriptor presence probe (#1601).

:func:`~meho_backplane.operations._lookup.descriptor_exists_any_state` is
the sibling of :func:`~meho_backplane.operations._lookup.lookup_descriptor`
that ignores ``is_enabled``. It exists only to let the composite pre-flight
tell *present-but-disabled* (a row with ``is_enabled = false``) apart from
*truly absent* (no row), so the dispatch error can choose between
``composite_l2_disabled`` (re-enable remediation) and ``composite_l2_missing``
(re-ingest remediation).

These tests insert real ``endpoint_descriptor`` rows against the autouse-
migrated SQLite engine and assert the probe's three load-bearing behaviours:

* a **disabled** row (``is_enabled = false``) is invisible to
  ``lookup_descriptor`` but **visible** to ``descriptor_exists_any_state``;
* an **enabled** row is visible to both;
* a **truly-absent** op_id is invisible to both.

The tenant-then-global visibility scope is checked too: a disabled row owned
by another tenant must not register as present for the calling tenant (no
cross-tenant presence oracle), while a global (``tenant_id IS NULL``) disabled
row must.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._lookup import (
    descriptor_exists_any_state,
    lookup_descriptor,
)
from meho_backplane.settings import get_settings

_TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
_TENANT_B = UUID("00000000-0000-0000-0000-00000000000b")

_PRODUCT = "vmware"
_VERSION = "9.0"
_IMPL_ID = "vmware-rest"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


async def _insert_descriptor(
    session: AsyncSession,
    *,
    op_id: str,
    is_enabled: bool,
    tenant_id: UUID | None,
) -> None:
    """Insert a minimal ingested-shape ``endpoint_descriptor`` row."""
    method, path = op_id.split(":", 1)
    session.add(
        EndpointDescriptor(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=op_id,
            source_kind="ingested",
            method=method,
            path=path,
            handler_ref=None,
            summary="probe fixture",
            description="probe fixture",
            group_id=None,
            tags=[],
            parameter_schema={"type": "object"},
            response_schema=None,
            llm_instructions=None,
            safety_level="safe",
            requires_approval=False,
            is_enabled=is_enabled,
            embedding=None,
            custom_description=None,
            custom_notes=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_disabled_row_invisible_to_lookup_but_present_to_probe(
    session: AsyncSession,
) -> None:
    """A disabled row: ``lookup_descriptor`` -> None, probe -> True.

    This is the exact gap #1601 closes: the disabled state was
    indistinguishable from absence because both resolve to ``None``
    through ``lookup_descriptor``'s ``is_enabled = TRUE`` filter.
    """
    op_id = "GET:/vcenter/datastore"
    await _insert_descriptor(session, op_id=op_id, is_enabled=False, tenant_id=None)

    resolved = await lookup_descriptor(
        tenant_id=_TENANT_A,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=op_id,
    )
    assert resolved is None, "disabled row is invisible to the dispatch lookup"

    present = await descriptor_exists_any_state(
        tenant_id=_TENANT_A,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=op_id,
    )
    assert present is True, "disabled row is present to the is_enabled-agnostic probe"


@pytest.mark.asyncio
async def test_enabled_row_present_to_both(session: AsyncSession) -> None:
    """An enabled row resolves through both helpers."""
    op_id = "GET:/vcenter/vm"
    await _insert_descriptor(session, op_id=op_id, is_enabled=True, tenant_id=None)

    resolved = await lookup_descriptor(
        tenant_id=_TENANT_A,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=op_id,
    )
    assert resolved is not None

    present = await descriptor_exists_any_state(
        tenant_id=_TENANT_A,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=op_id,
    )
    assert present is True


@pytest.mark.asyncio
async def test_absent_op_id_invisible_to_both(session: AsyncSession) -> None:
    """An op_id with no row in any state is absent to both helpers."""
    op_id = "GET:/vcenter/never-ingested"

    resolved = await lookup_descriptor(
        tenant_id=_TENANT_A,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=op_id,
    )
    assert resolved is None

    present = await descriptor_exists_any_state(
        tenant_id=_TENANT_A,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=op_id,
    )
    assert present is False


@pytest.mark.asyncio
async def test_probe_does_not_leak_other_tenants_disabled_rows(
    session: AsyncSession,
) -> None:
    """A disabled row owned by tenant B is not 'present' for tenant A.

    The probe mirrors ``lookup_descriptor``'s tenant-then-global
    visibility: a tenant-scoped row registers only for its owner (or
    when global). Otherwise the probe would be a cross-tenant presence
    oracle.
    """
    op_id = "GET:/vcenter/cluster/{cluster}"
    await _insert_descriptor(session, op_id=op_id, is_enabled=False, tenant_id=_TENANT_B)

    present_for_a = await descriptor_exists_any_state(
        tenant_id=_TENANT_A,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=op_id,
    )
    assert present_for_a is False, "tenant B's disabled row must not be visible to tenant A"

    present_for_b = await descriptor_exists_any_state(
        tenant_id=_TENANT_B,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=op_id,
    )
    assert present_for_b is True, "tenant B sees its own disabled row"
