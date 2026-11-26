# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An inventory read stops when the block list refuses one of its steps.

The real inventory handlers run through the real dispatcher. The steps they
read are test operations. The person is blocked (or the block list becomes
unreadable) while the inventory runs. The operation then answers with the
refusal and stores no inventory. Covered:

* ``linux.migration.inventory``: the change comes after its first step;
* ``hyperv.migration.inventory``: the change comes while it reads several
  virtual machines at the same time.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import block_principal, reset_block_cache
from meho_backplane.connectors.inventory import adapter_kit
from meho_backplane.connectors.inventory.source_hosts import (
    LINUX_CONNECTOR_ID,
    LINUX_INVENTORY_OP_ID,
)
from meho_backplane.connectors.inventory.source_hyperv import (
    HYPERV_CONNECTOR_ID,
    HYPERV_INVENTORY_OP_ID,
)
from meho_backplane.connectors.inventory.source_register import (
    SOURCE_ADAPTERS,
    register_source_adapters,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.settings import get_settings

from . import _composite_wait_audit_helpers as h

pytestmark = pytest.mark.asyncio

_FIRST_STEP = {
    LINUX_INVENTORY_OP_ID: "linux.about",
    HYPERV_INVENTORY_OP_ID: "hyperv.vms.config",
}
_STEPS = {
    LINUX_INVENTORY_OP_ID: ("linux.about", "linux.file.read"),
    HYPERV_INVENTORY_OP_ID: (
        "hyperv.vms.list",
        "hyperv.vms.guest.list",
        "hyperv.vms.config",
        "hyperv.disks.vm.list",
        "hyperv.checkpoints.list",
    ),
}
_CONNECTOR_IDS = {
    LINUX_INVENTORY_OP_ID: LINUX_CONNECTOR_ID,
    HYPERV_INVENTORY_OP_ID: HYPERV_CONNECTOR_ID,
}
_VM_ROWS = [{"Name": f"vm-{n}", "Id": f"00000000-0000-4000-8000-00000000000{n}"} for n in range(6)]

#: The steps that reached the target, in order.
sent: list[str] = []
#: What happens during the first step of a test, by step op id.
during_step: dict[str, Callable[[], Awaitable[None]]] = {}
#: The handles of the stored inventories.
stored: list[Any] = []
#: ``step`` is the step op id of ``during_step``. ``saved`` is set when its
#: hook has returned, so the block is saved. ``_env`` makes a new event for
#: each test: each test has its own event loop.
change = SimpleNamespace(step="", saved=asyncio.Event())


async def _note(op_id: str) -> None:
    sent.append(op_id)
    hook = during_step.pop(op_id, None)
    if hook is not None:
        try:
            await hook()
        finally:
            change.saved.set()
    elif op_id == change.step:
        # A parallel read of the same step returns only after the save, so
        # every later step of it checks the block list after the save.
        await change.saved.wait()


async def linux_about(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    await _note("linux.about")
    return {"hostname": "files-01", "os_pretty": "Example Linux 1"}


async def linux_file_read(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    await _note("linux.file.read")
    return {"content": "", "exists": True, "truncated": False}


async def hyperv_vms_list(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    await _note("hyperv.vms.list")
    return {"rows": _VM_ROWS, "total": len(_VM_ROWS)}


async def hyperv_vms_guest_list(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    await _note("hyperv.vms.guest.list")
    return {"rows": [], "total": 0}


async def hyperv_vms_config(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    await _note("hyperv.vms.config")
    return {"ProcessorCount": 1}


async def hyperv_disks_vm_list(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    await _note("hyperv.disks.vm.list")
    return {"rows": [], "total": 0}


async def hyperv_checkpoints_list(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    await _note("hyperv.checkpoints.list")
    return {"rows": [], "total": 0}


_HANDLERS = {
    "linux.about": linux_about,
    "linux.file.read": linux_file_read,
    "hyperv.vms.list": hyperv_vms_list,
    "hyperv.vms.guest.list": hyperv_vms_guest_list,
    "hyperv.vms.config": hyperv_vms_config,
    "hyperv.disks.vm.list": hyperv_disks_vm_list,
    "hyperv.checkpoints.list": hyperv_checkpoints_list,
}


class _Store:
    async def spill_document(self, **kwargs: Any) -> bool:
        stored.append(kwargs["handle_id"])
        return True


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(adapter_kit, "get_result_handle_store", _Store)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_block_cache()
    clear_registry()
    sent.clear()
    during_step.clear()
    change.step = ""
    change.saved = asyncio.Event()
    stored.clear()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_block_cache()
    clear_registry()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s_:
        s_.add(Tenant(id=h.TENANT_ID, slug="block-inventory", name="Block Inventory"))
        await s_.commit()
        yield s_


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = h.EMBEDDING
    service.encode.return_value = [h.EMBEDDING]
    service.dimension = 384
    return service


async def _register(op_id: str, embedding: AsyncMock) -> None:
    (adapter,) = (a for a in SOURCE_ADAPTERS if a.op_id == op_id)
    key = {"product": adapter.product, "version": adapter.version, "impl_id": adapter.impl_id}
    cls = type("_Connector", (h.NoOpVaultConnector,), key)
    register_connector_v2(**key, cls=cls)
    for step in _STEPS[op_id]:
        await register_typed_operation(
            **key,
            op_id=step,
            handler=_HANDLERS[step],
            summary="Read.",
            description="Read.",
            parameter_schema={"type": "object"},
            when_to_use=None,
            tags=["read-only"],
            safety_level="safe",
            embedding_service=embedding,
        )
    await register_source_adapters(embedding_service=embedding)


async def _run(op_id: str) -> Any:
    return await dispatch(
        operator=h.operator(),
        connector_id=_CONNECTOR_IDS[op_id],
        op_id=op_id,
        target=SimpleNamespace(
            id=uuid4(),
            tenant_id=h.TENANT_ID,
            name="host-01",
            host="192.0.2.10",
            port=22,
            product=op_id.split(".")[0],
            fingerprint=None,
            preferred_impl_id=None,
            auth_model="shared_service_account",
            secret_ref=None,
            extras={},
        ),
        params={},
    )


async def _block_person() -> None:
    admin = Operator(
        sub="admin-person",
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=h.TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )
    await block_principal(admin, h.operator().sub, "left the team")


def _make_unreadable(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[None]]:
    async def _broken(_sessionmaker: Any) -> frozenset[tuple[str, str]]:
        raise RuntimeError("database down")

    async def _break_now() -> None:
        monkeypatch.setattr("meho_backplane.auth.principal_block._load_entries", _broken)
        reset_block_cache()

    return _break_now


@pytest.mark.parametrize("op_id", [LINUX_INVENTORY_OP_ID, HYPERV_INVENTORY_OP_ID])
async def test_inventory_is_stored_for_a_person_who_is_not_blocked(
    session: AsyncSession, embedding: AsyncMock, op_id: str
) -> None:
    await _register(op_id, embedding)

    result = await _run(op_id)

    assert result.status == "ok", result
    assert len(stored) == 1


@pytest.mark.parametrize("op_id", [LINUX_INVENTORY_OP_ID, HYPERV_INVENTORY_OP_ID])
@pytest.mark.parametrize("case", ["blocked", "unreadable"])
async def test_inventory_stops_and_stores_nothing_when_a_step_is_refused(
    session: AsyncSession,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    case: str,
) -> None:
    await _register(op_id, embedding)
    change.step = _FIRST_STEP[op_id]
    during_step[change.step] = _block_person if case == "blocked" else _make_unreadable(monkeypatch)

    result = await _run(op_id)

    status, code = (
        ("denied", "principal_blocked")
        if case == "blocked"
        else ("error", "principal_block_check_unavailable")
    )
    assert (result.status, result.extras["error_code"]) == (status, code), result
    assert result.op_id == op_id
    assert result.extras["execution_outcome"] == "uncertain"
    assert stored == []
    assert "linux.file.read" not in sent
    assert "hyperv.checkpoints.list" not in sent
    steps = _STEPS[op_id]
    assert set(sent) <= set(steps[: steps.index(_FIRST_STEP[op_id]) + 1]), sent
