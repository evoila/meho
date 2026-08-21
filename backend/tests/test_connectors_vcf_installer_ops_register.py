# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""DB-backed registration test for the vcf-installer typed primitives (#3078).

Runs :func:`register_installer_typed_operations` against the autouse-migrated
SQLite engine (a deterministic embedding stub replaces the ONNX pipeline) and
asserts the four ``endpoint_descriptor`` rows land with the governed
submit/poll posture the issue contract pins:

* ``installer.sddc.spec.validate`` — ``caution``, no approval (writes to the
  installer appliance, mutates no estate);
* ``installer.sddc.validation.status`` / ``installer.sddc.bringup.status`` —
  ``safe``, no approval;
* ``installer.sddc.bringup.start`` — ``dangerous`` + ``requires_approval``.

This catches a registration-time failure (bad schema, op_id convention,
when_to_use pairing, safety drift) in the fast unit lane instead of at
integration-boot. Mirrors ``test_connectors_vcf_installer_bringup_register``.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.vcf_installer import (
    INSTALLER_TYPED_OPS,
    register_installer_typed_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.typed_register import _TYPED_OP_REGISTRARS
from meho_backplane.settings import get_settings

_OP_IDS = tuple(op.op_id for op in INSTALLER_TYPED_OPS)

#: The issue-contract posture per op_id: (safety_level, requires_approval).
_EXPECTED_POSTURE: dict[str, tuple[str, bool]] = {
    "installer.sddc.spec.validate": ("caution", False),
    "installer.sddc.validation.status": ("safe", False),
    "installer.sddc.bringup.start": ("dangerous", True),
    "installer.sddc.bringup.status": ("safe", False),
}


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


async def _fetch_installer_rows() -> dict[str, EndpointDescriptor]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id.like("installer.sddc.%")
                    )
                )
            )
            .scalars()
            .all()
        )
    return {row.op_id: row for row in rows}


@pytest.mark.asyncio
async def test_registrar_lands_exactly_the_four_primitive_rows(
    stub_embedding_service: AsyncMock,
) -> None:
    """The registrar upserts the issue-contract op set, each embedded once."""
    await register_installer_typed_operations(embedding_service=stub_embedding_service)
    rows = await _fetch_installer_rows()
    assert sorted(rows) == sorted(_EXPECTED_POSTURE)
    assert sorted(_OP_IDS) == sorted(_EXPECTED_POSTURE)
    assert stub_embedding_service.encode_one.call_count == len(_EXPECTED_POSTURE)


@pytest.mark.asyncio
async def test_rows_carry_the_contract_safety_posture(
    stub_embedding_service: AsyncMock,
) -> None:
    """bringup.start parks for approval; the other three dispatch per safety level."""
    await register_installer_typed_operations(embedding_service=stub_embedding_service)
    rows = await _fetch_installer_rows()
    for op_id, (safety_level, requires_approval) in _EXPECTED_POSTURE.items():
        row = rows[op_id]
        assert row.source_kind == "typed", f"{op_id} source_kind={row.source_kind!r}"
        assert row.safety_level == safety_level, f"{op_id} safety={row.safety_level!r}"
        assert row.requires_approval is requires_approval, op_id
        assert row.is_enabled is True
        assert row.tenant_id is None


@pytest.mark.asyncio
async def test_rows_resolve_their_bound_method_handlers(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each persisted handler ref names the connector shim the op declares."""
    await register_installer_typed_operations(embedding_service=stub_embedding_service)
    rows = await _fetch_installer_rows()
    for op in INSTALLER_TYPED_OPS:
        row = rows[op.op_id]
        assert row.handler_ref and op.handler_attr in row.handler_ref, row.handler_ref


@pytest.mark.asyncio
async def test_rows_persist_the_result_scalars_reduction_hint(
    stub_embedding_service: AsyncMock,
) -> None:
    """Every persisted row carries the #3084 ``result_scalars`` hint with ``id``.

    The dispatcher lifts ``llm_instructions["result_scalars"]`` off the
    *persisted* descriptor row (``_result_scalars_from_descriptor``), so the
    hint must survive the DB round-trip — otherwise a reduce of the vendor
    ``Validation`` / ``SddcTask`` swallows the poll ``id`` again and the
    submit → poll loop breaks exactly as observed live.
    """
    await register_installer_typed_operations(embedding_service=stub_embedding_service)
    rows = await _fetch_installer_rows()
    for op in INSTALLER_TYPED_OPS:
        row = rows[op.op_id]
        assert isinstance(row.llm_instructions, dict), op.op_id
        hint = row.llm_instructions.get("result_scalars")
        assert isinstance(hint, dict), f"{op.op_id} must persist a result_scalars hint"
        assert op.llm_instructions is not None
        assert hint == op.llm_instructions["result_scalars"], op.op_id
        assert "id" in hint["keys"], f"{op.op_id}: the poll id must be a preserved scalar"
